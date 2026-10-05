#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retry bridge + round-robin engine between CC Switch's proxy and the relays.

Why this exists
---------------
CC Switch's proxy tries **each provider at most once per request** (its log says
"已尝试 10/10 个 Provider" even with max_retries=40), and it treats HTTP 400 as
a client error, so the relays' 400

    {"error":{"message":"Upstream request failed","type":"upstream_error"}}

is never retried and the turn dies. This bridge therefore owns the retry loop:

    1 rquest -> bridge -> relay A (fail) -> relay B (fail) -> ... -> relay J
                       -> relay A -> ...  up to BRIDGE_ATTEMPTS total attempts,
                       cycling the whole relay list.

Routing
-------
CC Switch calls  /p/<provider-id><prefix><endpoint>  . The bridge strips the
mount, keeps the endpoint, and for attempt N uses relay number
(start_index + N) % len(order), rebuilding the path as
<that relay's upstream><that relay's prefix><endpoint>. Each relay is called
with **its own API key** (taken from CC Switch's database by setup.py), so
rotating across relays with different keys works.

A response that is not retryable (auth, malformed request, ...) is passed
through untouched. When every attempt is used up the bridge answers with a
status CC Switch does NOT fail over on, so the budget stays bounded instead of
being multiplied by CC Switch's own 10-provider walk.

Config lives in routes.json (written by setup.py). Secrets in it -> file mode 600.
"""

import base64
import json
import os
import random
import socket
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTES_FILE = os.environ.get("BRIDGE_ROUTES", os.path.join(BASE_DIR, "routes.json"))
LOG_FILE = os.environ.get("BRIDGE_LOG", os.path.join(BASE_DIR, "bridge.log"))

DEFAULT_ATTEMPTS = int(os.environ.get("BRIDGE_ATTEMPTS", "100"))
MAX_SECONDS = float(os.environ.get("BRIDGE_MAX_SECONDS", "300"))   # 0 = no wall-clock cap
BACKOFF = float(os.environ.get("BRIDGE_BACKOFF", "0.15"))
# Backoff for connection/TLS-level failures. They almost always arrive in
# bursts (VPN/tunnel reloads, network switches) and hit *every* relay at once,
# so hammering the chain is pointless - backing off lets the request ride the
# outage out instead of burning the whole budget inside it.
NET_BACKOFF_MAX = float(os.environ.get("BRIDGE_NET_BACKOFF_MAX", "8"))
# Abort the whole request once this many consecutive connection-level failures
# have happened. Without it a total network outage keeps the request alive for
# BRIDGE_MAX_SECONDS, so the client looks frozen instead of failing fast.
NET_FAIL_LIMIT = int(os.environ.get("BRIDGE_NET_FAIL_LIMIT", "15"))
READ_TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "300"))
CONNECT_TIMEOUT = float(os.environ.get("BRIDGE_CONNECT_TIMEOUT", "20"))
# How long to wait for the *first* byte of a stream. Kept below CC Switch's
# streaming_first_byte_timeout (60s) so the bridge notices a stalled relay and
# rotates before CC Switch gives up and truncates the client's stream.
FIRST_BYTE_TIMEOUT = float(os.environ.get("BRIDGE_FIRST_BYTE_TIMEOUT", "50"))
# status returned when the whole budget is spent; 400 makes CC Switch stop
# instead of walking its own 10-provider chain all over again
EXHAUST_STATUS = int(os.environ.get("BRIDGE_EXHAUST_STATUS", "400"))
VERBOSE = os.environ.get("BRIDGE_VERBOSE", "1") != "0"

# worth another relay
RETRY_STATUS = {400, 401, 403, 404, 405, 408, 409, 425, 429,
                500, 502, 503, 504, 520, 521, 522, 523, 524, 529}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "authorization", "x-api-key",
}

_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if VERBOSE:
        sys.stdout.write(line)
        sys.stdout.flush()
    try:
        with _log_lock:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".1")
            with open(LOG_FILE, "a") as fh:
                fh.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------- routes
_state = {"mounts": {}, "order": [], "routes": {}, "attempts": DEFAULT_ATTEMPTS}
_state_mtime = 0.0
_state_lock = threading.Lock()


def load_routes(force=False):
    global _state_mtime
    try:
        mtime = os.path.getmtime(ROUTES_FILE)
    except OSError:
        return
    if not force and mtime == _state_mtime:
        return
    try:
        with open(ROUTES_FILE) as fh:
            data = json.load(fh)
    except Exception as exc:
        log("route reload failed: %s" % exc)
        return

    routes = {}
    for pid, entry in (data.get("routes") or {}).items():
        mount = (entry.get("mount") or "").rstrip("/")
        upstream = (entry.get("upstream") or "").rstrip("/")
        if not mount or not upstream:
            continue
        routes[pid] = {
            "mount": mount,
            "upstream": upstream,
            "prefix": entry.get("prefix") or "",
            "auth": entry.get("auth") or "",
            "auth_type": entry.get("auth_type") or "bearer",
            "auth_file": entry.get("auth_file") or "",
            "max_attempts": entry.get("max_attempts"),
            "name": entry.get("name") or pid,
        }
    order = [pid for pid in (data.get("order") or []) if pid in routes]
    order += [pid for pid in routes if pid not in order]
    with _state_lock:
        _state["routes"] = routes
        _state["order"] = order
        _state["mounts"] = {r["mount"]: pid for pid, r in routes.items()}
        _state["attempts"] = int(data.get("attempts") or DEFAULT_ATTEMPTS)
        _state_mtime = mtime
    log("routes loaded: %d relay(s), attempts=%d" % (len(routes), _state["attempts"]))


def resolve(path):
    """Match the request against the mount table (longest prefix wins)."""
    load_routes()
    with _state_lock:
        best_mount, best_pid = "", None
        for mount, pid in _state["mounts"].items():
            if path == mount or path.startswith(mount + "/"):
                if len(mount) > len(best_mount):
                    best_mount, best_pid = mount, pid
        if best_pid is None:
            return None
        return best_pid, path[len(best_mount):] or "/", list(_state["order"]), \
            _state["routes"][best_pid]["prefix"]


# ------------------------------------------------------------------- oauth auth
def load_oauth(path):
    """Read a Codex auth.json (ChatGPT subscription login).

    Returns (access_token, account_id, exp) or None. Read fresh on every attempt
    on purpose: Codex/CC Switch rotate the token in place, and a cached copy is
    exactly how you end up sending a revoked token.
    """
    if not path:
        return None
    path = os.path.expanduser(path)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return None
    if data.get("auth_mode") not in (None, "chatgpt"):
        return None
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        return None
    exp = 0
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        pass
    return token, tokens.get("account_id"), exp


def normalize_for_official(body):
    """Make a request acceptable to the ChatGPT Codex backend.

    Relays tolerate `input` being a bare string; the official backend rejects it
    with {"detail":"Input must be a list"}, which would silently break the
    account fallback for any client that sends the short form.
    """
    if not body:
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(data, dict):
        return body
    inp = data.get("input")
    if isinstance(inp, str):
        data["input"] = [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": inp}]}]
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return body


# ----------------------------------------------------------------------- upstream
def is_retryable_body(body):
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("type") == "upstream_error" or data.get("code") == "cc_switch_upstream_error":
        return True
    err = data.get("error")
    if isinstance(err, dict):
        if err.get("type") == "upstream_error":
            return True
        if "Upstream request failed" in str(err.get("message") or ""):
            return True
    return False


def open_upstream(method, url, headers, body):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    conn = HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT) if scheme == "https" \
        else HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
    conn.request(method, target, body=body, headers=headers)
    resp = conn.getresponse()
    try:
        resp.fp.raw._sock.settimeout(READ_TIMEOUT)
    except Exception:
        pass
    return conn, resp


def read_all(resp, limit=8 * 1024 * 1024):
    chunks, total = [], 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)


# ------------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ccswitch-retry-bridge/1.1"

    def log_message(self, *args):
        pass

    def _read_request_body(self):
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        return self.rfile.read(int(length)) if length else b""

    def _base_headers(self):
        out = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            if key.lower() == "accept-encoding":
                continue
            out[key] = value
        out["Accept-Encoding"] = "identity"
        return out

    def _send(self, status, headers, body):
        try:
            self.send_response(status)
            for key, value in headers:
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError as exc:
            self.close_connection = True
            log("CLIENT-GONE while sending %s: %s" % (status, exc))

    def _stream(self, status, headers, resp, head=b""):
        try:
            self.send_response(status)
            for key, value in headers:
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if head:
                self.wfile.write(b"%x\r\n" % len(head) + head + b"\r\n")
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except OSError as exc:
            self.close_connection = True
            log("CLIENT-GONE / upstream cut mid-stream: %s" % exc)

    def _handle(self, method):
        split = urlsplit(self.path)
        resolved = resolve(split.path)
        if resolved is None:
            body = json.dumps({"error": {
                "message": "retry bridge: no route for %s" % split.path,
                "type": "invalid_request_error"}}).encode()
            log("NO ROUTE %s %s" % (method, self.path))
            self._send(404, [("Content-Type", "application/json")], body)
            return

        start_pid, endpoint, order, start_prefix = resolved
        # endpoint still carries the *starting* relay's prefix; strip it so the
        # suffix can be re-attached to whichever relay attempt N uses
        suffix = endpoint
        if start_prefix and suffix.startswith(start_prefix):
            suffix = suffix[len(start_prefix):] or "/"
        if split.query:
            suffix += "?" + split.query
        request_body = self._read_request_body()
        base_headers = self._base_headers()
        base_headers["Content-Length"] = str(len(request_body))

        start_index = order.index(start_pid) if start_pid in order else 0
        with _state_lock:
            routes = dict(_state["routes"])
            attempts = _state["attempts"]

        start_time = time.time()
        last = {"status": None, "headers": [("Content-Type", "application/json")], "body": b""}

        counts = {}          # attempts spent per route (honours max_attempts)
        done = 0
        skipped = 0
        cursor = 0
        netfails = 0         # consecutive connection/TLS-level failures
        while done < attempts and skipped < len(order):
            pid = order[(start_index + cursor) % len(order)]
            cursor += 1
            route = routes[pid]

            cap = route.get("max_attempts")
            if cap is not None and counts.get(pid, 0) >= cap:
                skipped += 1                       # route budget spent
                continue

            headers = dict(base_headers)
            if route.get("auth_type") == "oauth":
                cred = load_oauth(route.get("auth_file") or "")
                if not cred:
                    skipped += 1
                    log("SKIP  %s (no oauth credentials in %s)"
                        % (route["name"], route.get("auth_file") or "?"))
                    continue
                token, account_id, exp = cred
                if exp and exp <= time.time():
                    skipped += 1
                    log("SKIP  %s (access token expired)" % route["name"])
                    continue
                headers["Authorization"] = "Bearer " + token
                if account_id:
                    headers["chatgpt-account-id"] = account_id
            elif route.get("auth"):
                headers["Authorization"] = "Bearer " + route["auth"]

            skipped = 0
            counts[pid] = counts.get(pid, 0) + 1
            done += 1
            url = route["upstream"] + route["prefix"] + suffix

            attempt_body = request_body
            if route.get("auth_type") == "oauth":
                attempt_body = normalize_for_official(request_body)
                if attempt_body is not request_body:
                    headers["Content-Length"] = str(len(attempt_body))

            conn = None
            try:
                conn, resp = open_upstream(method, url, headers, attempt_body)
                status = resp.status
                raw_headers = resp.getheaders()
                ctype = (resp.getheader("Content-Type") or "").lower()

                if status == 200 and "event-stream" in ctype:
                    # Do not commit to this relay until it actually produces a
                    # byte. A relay that accepts the request and then stalls (or
                    # closes immediately) is the classic cause of Codex's
                    # "stream disconnected before completion"; rotating to the
                    # next relay is far better than handing the client a
                    # truncated body.
                    head = b""
                    try:
                        resp.fp.raw._sock.settimeout(FIRST_BYTE_TIMEOUT)
                        head = resp.read(1)
                    except (socket.timeout, OSError) as exc:
                        log("RETRY %s %s -> no first byte via %s (attempt %d/%d): %s"
                            % (method, split.path, route["name"], done, attempts, exc))
                        head = b""
                    finally:
                        try:
                            resp.fp.raw._sock.settimeout(READ_TIMEOUT)
                        except Exception:
                            pass
                    if not head:
                        last = {"status": 200,
                                "headers": [("Content-Type", "application/json")],
                                "body": json.dumps({"error": {
                                    "message": "retry bridge: empty or stalled stream",
                                    "type": "upstream_error"}}).encode()}
                        netfails += 1
                        conn.close()
                        conn = None
                    else:
                        log("OK    %s %s -> %s via %s (attempt %d/%d)"
                            % (method, split.path, status, route["name"], done, attempts))
                        self._stream(status, raw_headers, resp, head)
                        conn.close()
                        return

                body = read_all(resp)
                conn.close()
                conn = None

                retryable = status in RETRY_STATUS or is_retryable_body(body)

                if not retryable:
                    log("PASS  %s %s -> %s via %s (attempt %d)"
                        % (method, split.path, status, route["name"], done))
                    self._send(status, raw_headers, body)
                    return

                last = {"status": status, "headers": raw_headers, "body": body}
                netfails = 0
                snippet = body[:120].decode("utf-8", "replace").replace("\n", " ")
                log("RETRY %s %s -> %s via %s (attempt %d/%d) %s"
                    % (method, split.path, status, route["name"], done, attempts, snippet))
            except (socket.timeout, OSError) as exc:
                last = {"status": None,
                        "headers": [("Content-Type", "application/json")],
                        "body": json.dumps({"error": {
                            "message": "retry bridge: unreachable: %s" % exc,
                            "type": "upstream_error"}}).encode()}
                netfails += 1
                log("RETRY %s %s -> network error via %s (attempt %d/%d, net-streak %d): %s"
                    % (method, split.path, route["name"], done, attempts, netfails, exc))
            except Exception as exc:                                   # pragma: no cover
                last = {"status": None,
                        "headers": [("Content-Type", "application/json")],
                        "body": json.dumps({"error": {
                            "message": "retry bridge: %s" % exc,
                            "type": "upstream_error"}}).encode()}
                netfails += 1
                log("RETRY %s %s -> error (attempt %d/%d): %r"
                    % (method, split.path, done, attempts, exc))
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

            if MAX_SECONDS and (time.time() - start_time) >= MAX_SECONDS:
                log("TIMEBOX %s %s stopped after %.0fs / %d attempt(s)"
                    % (method, split.path, time.time() - start_time, done))
                break
            if NET_FAIL_LIMIT and netfails >= NET_FAIL_LIMIT:
                log("NETOUTAGE %s %s aborted after %d consecutive network failures "
                    "in %.0fs - the network itself is down, not the relays"
                    % (method, split.path, netfails, time.time() - start_time))
                break
            if done < attempts:
                if netfails:
                    # whole-network outage: wait it out rather than churn
                    delay = min(1.0 * (2 ** (netfails - 1)), NET_BACKOFF_MAX)
                else:
                    delay = BACKOFF
                time.sleep(delay + (0.6 if done % 10 == 0 else 0.0)
                           + random.random() * BACKOFF)

        log("GIVEUP %s %s after %d attempt(s), last=%s"
            % (method, split.path, done, last["status"]))
        body = last["body"] or json.dumps({"error": {
            "message": "retry bridge: every relay failed",
            "type": "upstream_error"}}).encode()
        try:
            parsed = json.loads(body.decode("utf-8", "replace"))
            if not isinstance(parsed, dict) or "error" not in parsed:
                raise ValueError
        except Exception:
            parsed = {"error": {"message": body[:300].decode("utf-8", "replace"),
                                "type": "upstream_error"}}
            body = json.dumps(parsed).encode()
        self._send(EXHAUST_STATUS, [("Content-Type", "application/json")], body)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def do_HEAD(self):
        self._handle("HEAD")


class BridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a realistic listen backlog.

    The stock backlog of 5 drops connections as soon as Codex opens a few
    concurrent streams, which shows up upstream as
    "上游连接失败: error sending request".
    """
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = int(os.environ.get("BRIDGE_BACKLOG", "256"))


def main():
    load_routes(force=True)
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "15888"))
    server = BridgeServer((host, port), Handler)
    log("retry bridge listening on %s:%d (backlog=%d, first-byte timeout=%.0fs)"
        % (host, port, server.request_queue_size, FIRST_BYTE_TIMEOUT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
