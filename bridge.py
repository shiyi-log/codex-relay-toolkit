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
mount, keeps the endpoint, and rebuilds the path as
<relay upstream><relay prefix><endpoint>. Each relay is called with **its own
API key** (taken from CC Switch's database by setup.py), so rotating across
relays with different keys works.

Which relay is tried first is **not fixed**: every relay is scored from its own
billed price (`GET /v1/usage`, refreshed in the background) and from what this
machine actually measured - time to first byte and a decayed failure rate. The
cheapest+fastest relay that serves the requested model is tried first, and a
relay that is slow, stalling or expensive sinks. `BRIDGE_ORDER_MODE=fixed`
restores the old "start at the provider CC Switch picked, then round-robin"
behaviour. `GET /__bridge/status?text=1` prints the current ranking.

A response that is not retryable (auth, malformed request, ...) is passed
through untouched. When every attempt is used up the bridge answers with a
status CC Switch does NOT fail over on, so the budget stays bounded instead of
being multiplied by CC Switch's own 10-provider walk.

Config lives in routes.json (written by setup.py). Secrets in it -> file mode 600.
"""

import base64
import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTES_FILE = os.environ.get("BRIDGE_ROUTES", os.path.join(BASE_DIR, "routes.json"))
LOG_FILE = os.environ.get("BRIDGE_LOG", os.path.join(BASE_DIR, "bridge.log"))
# One JSON line per attempt: which provider CC Switch sent to, which relay the
# bridge actually used, model, attempt, status, first byte. CC Switch can only
# record the provider it *sent* to, so this file is the only per-request record
# of the real relay. Set to 0/off/empty to disable.
REQUEST_LOG = os.environ.get("BRIDGE_REQUEST_LOG",
                             os.path.join(BASE_DIR, "bridge-requests.jsonl"))
if REQUEST_LOG.strip().lower() in ("0", "off", "none", "no"):
    REQUEST_LOG = ""

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


def log_request(mount, relay, model, attempt, status, seconds, result,
                pid=None, tokens=None, stream_complete=None, response_id=None,
                mount_id=None, relay_id=None, error_kind=None, breaker=None):
    """Append one structured request line (see BRIDGE_REQUEST_LOG).

    `tokens` comes from the relay's own `usage` block; `est_cost_usd` prices it
    with **that relay's measured $/weighted-M-token**, i.e. what this relay
    really charges - not the shared blended table CC Switch uses.
    `stream_complete=False` means the client hung up mid-stream; then there is
    no usage to report at all (the relay never sent it).
    `response_id` + `relay_id` let relay_attrib.py point CC Switch's request-log
    row at the relay that really served it.
    """
    if not REQUEST_LOG:
        return
    price = None
    if pid and tokens:
        price, _exact = price_index(pid, model)
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": round(time.time(), 3),
             "mount": mount, "relay": relay, "model": model, "attempt": attempt,
             "status": status, "result": result,
             "first_byte_ms": round(seconds * 1000) if seconds is not None else None}
    if mount_id:
        entry["mount_id"] = mount_id
    if relay_id:
        entry["relay_id"] = relay_id
    if response_id:
        entry["response_id"] = response_id
    if error_kind:
        entry["error_kind"] = error_kind
    if breaker:
        entry["breaker"] = breaker
    if stream_complete is not None:
        entry["stream_complete"] = stream_complete
    if tokens:
        entry["tokens"] = tokens
        entry["price_per_m"] = round(price, 5) if price else None
        entry["est_cost_usd"] = (round(weighted_tokens({
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "cache_read_tokens": tokens.get("cache_read"),
            "cache_creation_tokens": tokens.get("cache_creation")}) * price, 8)
            if price else None)
    try:
        with _log_lock:
            if os.path.exists(REQUEST_LOG) and os.path.getsize(REQUEST_LOG) > 5 * 1024 * 1024:
                os.replace(REQUEST_LOG, REQUEST_LOG + ".1")
            with open(REQUEST_LOG, "a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


class UsageScanner:
    """Pull the `usage` block out of a streamed Responses API body.

    The bridge relays streams chunk by chunk and must not buffer a whole
    response, so this keeps only a bounded window and tries to decode the
    object after every `"usage":` marker. The last usable block wins (relays
    often send `"usage":{}` in earlier events and the real numbers in the final
    `response.completed`).
    """

    MARKER = '"usage"'

    def __init__(self, limit=256 * 1024):
        self.buf = ""
        self.limit = limit
        self.usage = None
        # the relay's response id: CC Switch names its request-log row
        # "session:codex:<provider>:<resp_id>", so this is what lets the
        # attributor rewrite that row's provider to the relay that really served
        self.response_id = None
        self._tail = ""

    def feed(self, chunk):
        if not chunk:
            return
        text = chunk.decode("utf-8", "replace")
        for m in re.finditer(r'"id":"(resp_[A-Za-z0-9]+)"', self._tail + text):
            self.response_id = m.group(1)
        self._tail = (self._tail + text)[-200:]
        self.buf += text
        if len(self.buf) > self.limit:
            self.buf = self.buf[-self.limit:]
        while True:
            i = self.buf.find(self.MARKER)
            if i < 0:
                self.buf = self.buf[-64:]      # marker may straddle two chunks
                return
            j = self.buf.find("{", i + len(self.MARKER))
            if j < 0:
                self.buf = self.buf[i:]
                return
            try:
                obj, end = json.JSONDecoder().raw_decode(self.buf[j:])
            except ValueError:
                if len(self.buf) >= self.limit:
                    self.buf = self.buf[j + 1:]   # malformed, stop retrying it
                else:
                    self.buf = self.buf[i:]       # truncated: wait for more
                return
            if isinstance(obj, dict) and obj:
                self.usage = obj
            self.buf = self.buf[j + end:]

    def tokens(self):
        return normalise_usage(self.usage)


def normalise_usage(usage):
    """Responses-API usage -> our token buckets.

    `input_tokens` already includes the cached ones, so uncached input is the
    difference; that matches how the relays' own `model_stats` count them.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details") or {}
    try:
        total_in = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cached = int(details.get("cached_tokens") or 0)
        written = int(details.get("cache_write_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if not (total_in or out or cached):
        return None
    return {"input": max(0, total_in - cached), "output": out,
            "cache_read": cached, "cache_creation": written,
            "total": int(usage.get("total_tokens") or (total_in + out))}


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
        model_map = entry.get("model_map")
        routes[pid] = {
            "mount": mount,
            "upstream": upstream,
            "prefix": entry.get("prefix") or "",
            "auth": entry.get("auth") or "",
            "auth_type": entry.get("auth_type") or "bearer",
            "auth_file": entry.get("auth_file") or "",
            "max_attempts": entry.get("max_attempts"),
            "name": entry.get("name") or pid,
            # a sidecar (e.g. codex-relay in front of a chat-completions-only
            # provider) needs the client's model name rewritten, and has no
            # /v1/usage to read a price from - so both can be declared per route
            "model_map": model_map if isinstance(model_map, dict) else None,
            "price_per_m": entry.get("price_per_m"),
        }
    order = [pid for pid in (data.get("order") or []) if pid in routes]
    order += [pid for pid in routes if pid not in order]
    with _state_lock:
        _state["routes"] = routes
        _state["order"] = order
        _state["mounts"] = {r["mount"]: pid for pid, r in routes.items()}
        _state["attempts"] = int(data.get("attempts") or DEFAULT_ATTEMPTS)
        _state_mtime = mtime
        seeded = 0
        for pid, route in routes.items():
            price = route.get("price_per_m")
            if not price:
                continue
            with _stats_lock:
                ent = _prices.get(pid) or {}
                if not ent.get("static"):
                    _prices[pid] = {"ts": time.time(), "per_model": {}, "overall": float(price),
                                    "trend": 1.0, "error": "", "static": True}
                    seeded += 1
    if seeded:
        _prices_ready.set()
    log("routes loaded: %d relay(s), attempts=%d%s"
        % (len(routes), _state["attempts"],
           ", %d fixed price(s)" % seeded if seeded else ""))


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
def read_auth(path):
    """Read an auth.json without losing anything we do not understand."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path)) as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def auth_fingerprint(data):
    tokens = (data or {}).get("tokens") or {}
    raw = (tokens.get("access_token") or tokens.get("refresh_token") or "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def jwt_exp(token):
    """Read `exp` from a JWT payload without trusting it (we only need a clock)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


def oauth_creds(data):
    """-> (access_token, account_id, exp, fingerprint) or None."""
    if not isinstance(data, dict) or data.get("auth_mode") not in (None, "chatgpt"):
        return None
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        return None
    return token, tokens.get("account_id"), jwt_exp(token), auth_fingerprint(data)


def load_oauth(path):
    """Read the account fresh on every attempt (a cached copy is exactly how you
    end up sending a revoked token). -> creds tuple or None."""
    return oauth_creds(read_auth(path))


def write_auth(path, data):
    """Atomically persist auth.json at 0600, preserving fields we do not know."""
    path = os.path.expanduser(path)
    tmp = "%s.bridge-%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


_oauth_lock = threading.Lock()


def oauth_refresh(path, tried_fingerprint=None, reason="expired"):
    """Refresh the subscription access token and persist the rotated one.

    Guards, in order: mode off -> no-op; in-process lock; cross-process flock;
    re-read the file and if another process (the Codex app) already replaced the
    token we use *theirs* instead of rotating again. The write is
    read-modify-write so unknown fields and concurrent edits survive.
    """
    if OAUTH_REFRESH_MODE == "off" or not path:
        return None
    path = os.path.expanduser(path)
    with _oauth_lock:
        lock_fd = None
        try:
            import fcntl
            lock_fd = os.open(path + ".bridge-lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except Exception:
            lock_fd = None
        try:
            data = read_auth(path)
            if not data:
                return None
            if tried_fingerprint and auth_fingerprint(data) != tried_fingerprint:
                log("OAUTH token already replaced by someone else (%s), using it" % reason)
                return oauth_creds(data)
            tokens = dict(data.get("tokens") or {})
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                return None
            body = json.dumps({"client_id": OAUTH_CLIENT_ID,
                               "grant_type": "refresh_token",
                               "refresh_token": refresh_token}).encode()
            req = urllib.request.Request(
                OAUTH_ISSUER.rstrip("/") + "/oauth/token", data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "ccswitch-retry-bridge/1.4"})
            with urllib.request.urlopen(req, timeout=OAUTH_TIMEOUT) as fh:
                payload = json.load(fh)
            access = payload.get("access_token")
            if not access:
                log("OAUTH refresh (%s) returned no access_token" % reason)
                return None
            fresh = read_auth(path) or data      # read-modify-write
            merged = dict(fresh.get("tokens") or {})
            merged["access_token"] = access
            if payload.get("refresh_token"):
                merged["refresh_token"] = payload["refresh_token"]
            if payload.get("id_token"):
                merged["id_token"] = payload["id_token"]
            fresh["tokens"] = merged
            fresh["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_auth(path, fresh)
            log("OAUTH refreshed (%s) for %s" % (reason, path))
            return oauth_creds(fresh)
        except Exception as exc:
            log("OAUTH refresh failed (%s): %s" % (reason, exc))
            return None
        finally:
            if lock_fd is not None:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                except Exception:
                    pass


def parse_quota_headers(headers):
    """Quota straight from the response headers - no extra API call.

    The ChatGPT backend reports x-codex-{primary,secondary}-used-percent,
    -window-minutes, -reset-at / -reset-after-seconds, plus plan type and
    credits balance.
    """
    head = {}
    for key, value in (headers or []):
        head[key.lower()] = value
    windows, worst, worst_reset = {}, None, None
    for slot in ("primary", "secondary"):
        pct = head.get("x-codex-%s-used-percent" % slot)
        if pct is None:
            continue
        try:
            used = float(pct)
        except (TypeError, ValueError):
            continue
        reset = None
        for key in ("x-codex-%s-reset-after-seconds" % slot,
                    "x-codex-%s-reset-at" % slot):
            raw = head.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            reset = max(0.0, value - time.time()) if "reset-at" in key else max(0.0, value)
            break
        minutes = head.get("x-codex-%s-window-minutes" % slot)
        if str(minutes).strip() in ("0", "0.0"):
            continue                     # this plan has no such window
        windows[slot] = {"used_percent": used, "window_minutes": minutes,
                         "reset_after_s": reset}
        if worst is None or used > worst:
            worst, worst_reset = used, reset
    if worst is None:
        return None
    return {"used_percent": worst, "reset_after_s": worst_reset,
            "plan_type": head.get("x-codex-plan-type"),
            "credits": head.get("x-codex-credits-balance"),
            "ts": time.time(), "windows": windows}


def note_quota(pid, headers):
    """Store the account quota snapshot and park the account when it is spent."""
    snap = parse_quota_headers(headers)
    if not snap:
        return None
    with _stats_lock:
        _bucket(pid)["quota"] = snap
    if snap["used_percent"] >= 100:
        breaker_charge(pid, "quota", reset=snap.get("reset_after_s"))
    else:
        with _stats_lock:
            b = _bucket(pid)
            still_parked = b.get("last_error") == "quota" and (b.get("open_until") or 0) > time.time()
        if still_parked:
            b["last_error"] = ""
            b["open_until"] = 0
            b["fails"] = 0
            log("QUOTA %s reports %.0f%% again - account usable" % (pid, snap["used_percent"]))
    return snap


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


# ------------------------------------------------------------- model adaptation
# Relays do not all carry the same model versions. Rather than failing a request
# with 404 on a relay that is one version behind, look up what that relay
# actually serves and swap in the newest model of the same family. The client
# keeps asking for whatever it wants; the bridge fits each relay.
_models_cache = {}
_models_lock = threading.Lock()
MODEL_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?-([A-Za-z0-9.]+)$")
MODELS_TTL = float(os.environ.get("BRIDGE_MODELS_TTL", "1800"))

# ----------------------------------------------------------- adaptive order
# routes.json's `order` is only the *starting* order. At runtime every relay
# gets a score from the two things the bridge can actually observe:
#
#   price   what the relay itself bills (`GET /v1/usage` -> actual_cost per
#           weighted million tokens), refreshed in the background
#   speed   time-to-first-byte measured on this machine (EWMA per relay) plus a
#           decayed failure rate: stalls, timeouts, 5xx
#
# Lowest score is tried first. A relay that would need the model adapted (or
# has none of that family) pays a penalty, so "cheap" never silently turns into
# "worse model", and a relay we have no data for sits mid-pack (exploration)
# rather than dead last.
ORDER_MODE = os.environ.get("BRIDGE_ORDER_MODE", "adaptive").lower()
RESPECT_START = os.environ.get("BRIDGE_RESPECT_START", "0") == "1"
W_PRICE = float(os.environ.get("BRIDGE_W_PRICE", "1.0"))
W_LATENCY = float(os.environ.get("BRIDGE_W_LATENCY", "1.0"))
W_FAIL = float(os.environ.get("BRIDGE_W_FAIL", "2.0"))
MODEL_ADAPT_PENALTY = float(os.environ.get("BRIDGE_MODEL_ADAPT_PENALTY", "0.4"))
MODEL_MISSING_PENALTY = float(os.environ.get("BRIDGE_MODEL_MISSING_PENALTY", "1.5"))
EWMA_ALPHA = float(os.environ.get("BRIDGE_EWMA_ALPHA", "0.3"))
PRICE_TTL = float(os.environ.get("BRIDGE_PRICE_TTL", "600"))
PRICE_TIMEOUT = float(os.environ.get("BRIDGE_PRICE_TIMEOUT", "8"))
# how long a request may wait at cold start for the first price refresh
PRICE_WAIT = float(os.environ.get("BRIDGE_PRICE_WAIT", "8"))
# Price and speed are both *variables*, so nothing here is a one-off calibration:
#
#   price   taken from the relay's last BRIDGE_PRICE_DAYS days of billing, not
#           from its all-time average (a relay that raised prices yesterday must
#           not look cheap today); the per-model figure is scaled by that recent
#           trend. Anything older than BRIDGE_PRICE_MAX_AGE is not trusted.
#   speed   EWMA overall *and* per hour of the day (peak hours differ), and a
#           figure older than BRIDGE_LATENCY_MAX_AGE is not trusted either.
#   stale   relays whose numbers went stale get re-measured on every
#           BRIDGE_EXPLORE_EVERY-th request, so a relay that got faster/cheaper
#           (or slower/pricier) can climb back without being hammered.
PRICE_DAYS = int(os.environ.get("BRIDGE_PRICE_DAYS", "3"))
PRICE_MAX_AGE = float(os.environ.get("BRIDGE_PRICE_MAX_AGE", "21600"))     # 6h
LATENCY_MAX_AGE = float(os.environ.get("BRIDGE_LATENCY_MAX_AGE", "1800"))  # 30min
HOUR_MIN_SAMPLES = int(os.environ.get("BRIDGE_HOUR_MIN_SAMPLES", "3"))
EXPLORE_EVERY = int(os.environ.get("BRIDGE_EXPLORE_EVERY", "50"))
# Exploration costs a real request, so only spend it on a relay that could
# actually win: background /v1/usage refresh already catches price changes, so
# what we lack for a far-more-expensive relay is nothing worth buying. 0 = no
# price gate.
EXPLORE_PRICE_FACTOR = float(os.environ.get("BRIDGE_EXPLORE_PRICE_FACTOR", "2.0"))
# A relay can only win on speed once it has been measured, and the cheapest one
# would otherwise soak up every request. So while a *reasonably priced* relay
# has never been measured, promote it once - bounded by the number of such
# relays, and never for relays pricier than BRIDGE_WARMUP_PRICE_FACTOR x the
# cheapest. 0 disables warm-up.
WARMUP_PRICE_FACTOR = float(os.environ.get("BRIDGE_WARMUP_PRICE_FACTOR", "2.0"))
# ------------------------------------------------------------ circuit breaker
# Measured price/latency is not enough: a relay that just returned 429, or whose
# key is dead, must sit out regardless of how cheap it looks. Consecutive
# failures open a breaker; the first real request after the cooldown is the
# half-open probe, and every failed probe doubles the cooldown. Classification
# follows model-hotel / LiteLLM: auth and quota are handled differently from a
# burst of 5xx, and Retry-After is honoured.
BREAKER_THRESHOLD = int(os.environ.get("BRIDGE_BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN = float(os.environ.get("BRIDGE_BREAKER_COOLDOWN", "60"))
BREAKER_COOLDOWN_MAX = float(os.environ.get("BRIDGE_BREAKER_COOLDOWN_MAX", "900"))
BREAKER_AUTH_COOLDOWN = float(os.environ.get("BRIDGE_BREAKER_AUTH_COOLDOWN", "3600"))
BREAKER_QUOTA_MIN = float(os.environ.get("BRIDGE_BREAKER_QUOTA_MIN", "600"))
BREAKER_QUOTA_MAX = float(os.environ.get("BRIDGE_BREAKER_QUOTA_MAX", "691200"))  # 8d
RETRY_AFTER_MAX = float(os.environ.get("BRIDGE_RETRY_AFTER_MAX", "60"))
# ------------------------------------------------- subscription account (OAuth)
# The ChatGPT-subscription fallback route reads ~/.codex/auth.json on every
# attempt. That file is shared with the Codex app, which refreshes the same
# rotating refresh token - so we refresh only when the token is actually dead,
# serialise across processes with flock, and re-read before writing (see
# codex-proxy's warning: two refreshers invalidate each other).
OAUTH_REFRESH_MODE = os.environ.get("BRIDGE_OAUTH_REFRESH", "reactive").lower()
OAUTH_ISSUER = os.environ.get("BRIDGE_OAUTH_ISSUER", "https://auth.openai.com")
OAUTH_CLIENT_ID = os.environ.get("BRIDGE_OAUTH_CLIENT_ID",
                                 "app_EMoamEEZ73f0CkXaXp7hrann")
OAUTH_SKEW = float(os.environ.get("BRIDGE_OAUTH_SKEW", "300"))
OAUTH_TIMEOUT = float(os.environ.get("BRIDGE_OAUTH_TIMEOUT", "30"))
# accounts are discovered from these dirs (colon separated): dir/auth.json plus
# one level of subdirectories that have their own auth.json
OAUTH_DIRS = os.environ.get("BRIDGE_OAUTH_DIRS", "~/.codex")
# ---------------------------------------------------------- balance + affinity
# Two things measured price/latency cannot see:
#   * a relay whose prepaid balance is nearly gone will fail soon - park it
#     before it burns an attempt (the /v1/usage refresh already carries it)
#   * a conversation that hops relays loses its prompt cache (and, on stateful
#     relays, its context) - give the relay that served the last turn a bonus
BRIDGE_MIN_BALANCE = float(os.environ.get("BRIDGE_MIN_BALANCE", "1.0"))
BRIDGE_BALANCE_HOLD = float(os.environ.get("BRIDGE_BALANCE_HOLD", "3600"))
BRIDGE_UNLIMITED_BALANCE = float(os.environ.get("BRIDGE_UNLIMITED_BALANCE", "1000000"))
AFFINITY_TTL = float(os.environ.get("BRIDGE_AFFINITY_TTL", "1800"))
AFFINITY_BONUS = float(os.environ.get("BRIDGE_AFFINITY_BONUS", "0.75"))
AFFINITY_MAX = int(os.environ.get("BRIDGE_AFFINITY_MAX", "2000"))
# ---------------------------------------------------------- streaming health
# A relay that opens a stream and then emits only bookkeeping events (or nothing
# at all) must not be committed to - the client would simply hang. We buffer
# until the first *content* frame, because before the first byte is the only
# point where rotating is still possible. Once committed we cannot rotate any
# more, so a stall is ended with a well-formed response.failed frame instead of
# a truncated body.
TTFT_MARKERS = (b'"response.output_text.delta"',
                b'"response.function_call_arguments.delta"',
                b'"response.reasoning_summary_text.delta"',
                b'"response.reasoning_text.delta"',
                b'"response.output_item.added"',
                b'"response.content_part.added"')
STREAM_END_MARKERS = (b'"response.completed"', b'"response.failed"',
                      b'"response.incomplete"', b'data: [DONE]')
TTFT_BUFFER_MAX = int(os.environ.get("BRIDGE_TTFT_BUFFER_MAX", str(512 * 1024)))
STREAM_STALL = float(os.environ.get("BRIDGE_STREAM_STALL", "45"))
STREAM_STALL_MULT = float(os.environ.get("BRIDGE_STREAM_STALL_MULT", "3"))
STREAM_STALL_AFTER = int(os.environ.get("BRIDGE_STREAM_STALL_AFTER", "50"))
HOUSEKEEPING_SECONDS = float(os.environ.get("BRIDGE_HOUSEKEEPING", "30"))
# NOT state.json - that one belongs to watchdog.py
STATE_FILE = os.environ.get("BRIDGE_STATE", os.path.join(BASE_DIR, "bridge-state.json"))
STATE_TTL = float(os.environ.get("BRIDGE_STATE_TTL", "86400"))
# Relative weights that turn a relay's token mix into "weighted million
# tokens". Output is ~4x input and cache reads ~0.1x on every OpenAI model, so
# this makes relays with different cache-hit ratios comparable.
TOKEN_WEIGHTS = {"input_tokens": 1.0, "output_tokens": 4.0,
                 "cache_read_tokens": 0.1, "cache_creation_tokens": 1.25}

_stats_lock = threading.Lock()
_stats = {}       # pid -> {"lat", "ok", "fail", "samples", "ts", "lat_ts", "byhour"}
_prices = {}      # pid -> {"ts", "per_model", "overall", "trend", "error"}
_last_plan = {"order": [], "signature": ""}
_explore = {"n": 0}
_affinity = {}     # conversation key -> (pid, last seen)   [session stickiness]
_affinity_lock = threading.Lock()
# set once the first price refresh has landed; requests arriving during a cold
# start wait for it for a moment instead of ordering on the fixed list
_prices_ready = threading.Event()


def _bucket(pid):
    return _stats.setdefault(
        pid, {"lat": None, "ok": 0.0, "fail": 0.0, "samples": 0,
              "ts": 0.0, "lat_ts": 0.0, "attempt_ts": 0.0, "byhour": {},
              "fails": 0, "open_until": 0.0, "cooldown": BREAKER_COOLDOWN,
              "last_error": ""})


def record_attempt(pid, seconds=None, failed=False):
    """Feed one attempt into the relay's score.

    Two axes, both time-varying: EWMA overall and EWMA for the current hour of
    the day, so peak-hour slowness is remembered for that hour instead of
    dragging the relay down all day.
    """
    with _stats_lock:
        b = _bucket(pid)
        now = time.time()
        if failed:
            b["fail"] = b["fail"] * (1 - EWMA_ALPHA) + EWMA_ALPHA
            b["ok"] *= 1 - EWMA_ALPHA
        else:
            b["ok"] = b["ok"] * (1 - EWMA_ALPHA) + EWMA_ALPHA
            b["fail"] *= 1 - EWMA_ALPHA
            if seconds is not None:
                b["lat"] = seconds if b["lat"] is None else (
                    b["lat"] * (1 - EWMA_ALPHA) + seconds * EWMA_ALPHA)
                b["samples"] += 1
                b["lat_ts"] = now
                hour = str(time.localtime(now).tm_hour)
                h = b["byhour"].setdefault(hour, {"lat": None, "samples": 0})
                h["lat"] = seconds if h["lat"] is None else (
                    h["lat"] * (1 - EWMA_ALPHA) + seconds * EWMA_ALPHA)
                h["samples"] += 1
        b["ts"] = now
        b["attempt_ts"] = now


# ------------------------------------------------------- failure classification
QUOTA_HINTS = ("usage_limit_reached", "insufficient_quota", "quota_exhausted",
               "exceeded your current quota", "credit balance is too low",
               "余额不足", "额度已用完", "欠费")
RESET_KEYS = ("resets_at", "resets_in_seconds", "reset_at", "reset_after_seconds",
              "reset_after", "reset_time", "retry_after")


def _body_json(body):
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _find_reset_seconds(obj, depth=0):
    """Pull a reset/retry hint out of an error body, whatever it is called.

    codex-proxy documents the field-name mismatch: `resets_at` on the 429 body
    vs `reset_at` on the usage endpoint - so all spellings are accepted here.
    """
    if depth > 4 or not isinstance(obj, (dict, list)):
        return None
    items = obj.items() if isinstance(obj, dict) else enumerate(obj)
    for key, value in items:
        if isinstance(key, str) and key.lower() in RESET_KEYS:
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)):
                if "at" in key.lower() and value > 1e9:      # absolute epoch
                    return max(0.0, float(value) - time.time())
                return max(0.0, float(value))
            elif isinstance(value, str):
                try:
                    return max(0.0, float(value))
                except ValueError:
                    pass
        found = _find_reset_seconds(value, depth + 1)
        if found is not None:
            return found
    return None


def classify_error(status, headers=None, body=None):
    """-> (kind, retry_after_seconds, reset_seconds).

    kinds: quota (park until the reset), auth (dead key: park long),
    rate_limit (short throttle), timeout, server, model_missing, bad_request,
    transport.
    """
    text = (body or b"").decode("utf-8", "replace").lower()
    retry_after = None
    for key, value in (headers or []):
        if key.lower() == "retry-after":
            try:
                retry_after = max(0.0, float(value))
            except (TypeError, ValueError):
                retry_after = None
    reset = _find_reset_seconds(_body_json(body))
    if status == 402 or (status == 429 and any(h in text for h in QUOTA_HINTS)):
        return "quota", retry_after, reset
    if status == 429:
        return "rate_limit", retry_after, reset
    if status in (401, 403):
        return "auth", retry_after, reset
    if status == 404:
        return "model_missing", retry_after, reset
    if status in (408, 425, 504):
        return "timeout", retry_after, reset
    if status is not None and status >= 500:
        return "server", retry_after, reset
    if status is not None and status >= 400:
        return "bad_request", retry_after, reset
    return "transport", retry_after, reset


def breaker_charge(pid, kind, retry_after=None, reset=None):
    """Charge one failure to a relay and return its new breaker state."""
    now = time.time()
    with _stats_lock:
        b = _bucket(pid)
        b["fails"] = b.get("fails", 0) + 1
        b["last_error"] = kind
        if kind == "auth":
            b["cooldown"] = BREAKER_AUTH_COOLDOWN
            b["open_until"] = now + BREAKER_AUTH_COOLDOWN
        elif kind == "balance":
            b["cooldown"] = BRIDGE_BALANCE_HOLD
            b["open_until"] = now + BRIDGE_BALANCE_HOLD
        elif kind == "quota":
            hold = reset if reset else (retry_after or BREAKER_QUOTA_MIN)
            hold = min(BREAKER_QUOTA_MAX, max(BREAKER_QUOTA_MIN, float(hold)))
            b["cooldown"] = hold
            # extend-only: a nearer reset must never shorten an existing hold
            b["open_until"] = max(b.get("open_until") or 0, now + hold)
        elif kind in ("rate_limit", "timeout", "server", "transport"):
            if b["fails"] >= BREAKER_THRESHOLD:
                cd = min(BREAKER_COOLDOWN_MAX,
                         max(BREAKER_COOLDOWN,
                             (b.get("cooldown") or BREAKER_COOLDOWN) * 2))
                b["cooldown"] = cd
                b["open_until"] = now + cd
            elif retry_after:
                b["open_until"] = max(b.get("open_until") or 0,
                                      now + min(RETRY_AFTER_MAX, retry_after))
        return _breaker_state_locked(b, now)


def _breaker_state_locked(b, now):
    if (b.get("open_until") or 0) > now:
        return "open"
    return "half-open" if b.get("fails") else "closed"


def breaker_state(pid, now=None):
    now = now or time.time()
    with _stats_lock:
        return _breaker_state_locked(_stats.get(pid) or {}, now)


def breaker_ok(pid):
    """One success closes the breaker - except a quota park: a spent account
    still answers 200 while its window is exhausted, so only fresh quota data
    (below 100%) may lift that hold."""
    with _stats_lock:
        b = _bucket(pid)
        if (b.get("last_error") in ("quota", "balance")
                and (b.get("open_until") or 0) > time.time()):
            return
        b["fails"] = 0
        b["cooldown"] = BREAKER_COOLDOWN
        b["open_until"] = 0
        b["last_error"] = ""


def breaker_open(pid, now=None):
    now = now or time.time()
    with _stats_lock:
        return (_stats.get(pid) or {}).get("open_until", 0) > now


def effective_latency(pid, now=None):
    """Latency used for scoring: this hour's figure when it has enough samples,
    otherwise the recent overall figure.

    Speed is a variable: a figure older than BRIDGE_LATENCY_MAX_AGE is dropped
    (None -> the relay scores as "no data") and exploration re-measures it.
    """
    now = now or time.time()
    b = _stats.get(pid) or {}
    if not b.get("lat") or now - (b.get("lat_ts") or 0) > LATENCY_MAX_AGE:
        return None
    hour = (b.get("byhour") or {}).get(str(time.localtime(now).tm_hour))
    if hour and hour.get("samples", 0) >= HOUR_MIN_SAMPLES and hour.get("lat"):
        return hour["lat"]
    return b["lat"]


def last_measured(pid):
    """When this relay last produced a latency sample (0 = never)."""
    b = _stats.get(pid) or {}
    return b.get("lat_ts") or 0


def last_attempt(pid):
    """When this relay was last *tried*, success or failure.

    Exploration must look at attempts, not at successful measurements: a relay
    that always fails (dead key, deleted group) never produces a latency sample,
    so "never measured" would stay true forever and it would be promoted on
    every exploration forever.
    """
    b = _stats.get(pid) or {}
    return b.get("attempt_ts") or b.get("lat_ts") or 0


def weighted_tokens(ms):
    return sum((ms.get(k) or 0) * w for k, w in TOKEN_WEIGHTS.items()) / 1e6


def parse_usage(data, days=None):
    """Turn one /v1/usage body into a price picture.

    per_model: all-time $ per weighted million tokens, per model
    trend:     recent (last `days` days) price / all-time price, >1 = got
               pricier, <1 = got cheaper - the per-model figures are scaled by
               it so a relay that changed its price does not keep yesterday's
               reputation
    """
    days = PRICE_DAYS if days is None else days
    per_model = {}
    total_cost = total_weighted = 0.0
    for ms in data.get("model_stats") or []:
        cost = ms.get("actual_cost")
        if cost is None:
            cost = ms.get("account_cost")
        if cost is None:
            cost = ms.get("cost")
        weighted = weighted_tokens(ms)
        if cost is None or weighted <= 0 or not ms.get("model"):
            continue
        per_model[ms["model"]] = float(cost) / weighted
        total_cost += float(cost)
        total_weighted += weighted
    alltime = (total_cost / total_weighted) if total_weighted > 0 else None

    daily = data.get("daily_usage") or []
    recent_cost = recent_weighted = 0.0
    for day in daily[-days:] if days > 0 else daily:
        cost = day.get("actual_cost")
        if cost is None:
            cost = day.get("cost")
        if cost is None:
            continue
        # daily rows call cache-creation "cache_write"
        weighted = weighted_tokens({"input_tokens": day.get("input_tokens"),
                                    "output_tokens": day.get("output_tokens"),
                                    "cache_read_tokens": day.get("cache_read_tokens"),
                                    "cache_creation_tokens": day.get("cache_write_tokens")})
        recent_cost += float(cost)
        recent_weighted += weighted
    recent = (recent_cost / recent_weighted) if recent_weighted > 0 else None
    trend = 1.0
    if recent and alltime and alltime > 0:
        trend = min(5.0, max(0.2, recent / alltime))
    scaled = {m: v * trend for m, v in per_model.items()}
    return {"per_model": scaled, "overall": (recent or alltime),
            "alltime": alltime, "trend": trend, "days": days,
            "daily_days": len(daily)}


def read_balance(data):
    """Remaining credit as the relay reports it (None when it does not).

    Relays hand out absurd values for prepaid/unlimited plans (one of ours
    reports 1.1e10), so anything above BRIDGE_UNLIMITED_BALANCE counts as
    "no limit" instead of a real number.
    """
    if not isinstance(data, dict):
        return None
    for key in ("remaining", "balance"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if value > BRIDGE_UNLIMITED_BALANCE:
            return None
        return max(0.0, value)
    return None


def note_balance(pid, balance):
    """Park a nearly-empty relay; lift the park once it is topped up again."""
    if balance is None:
        return None
    with _stats_lock:
        b = _bucket(pid)
        b["balance"] = balance
        parked = (b.get("last_error") == "balance"
                  and (b.get("open_until") or 0) > time.time())
    if balance < BRIDGE_MIN_BALANCE:
        if not parked:
            breaker_charge(pid, "balance")
            log("BALANCE %s reports $%.2f (< $%.2f) - parked for %.0fs"
                % (pid, balance, BRIDGE_MIN_BALANCE, BRIDGE_BALANCE_HOLD))
    elif parked:
        with _stats_lock:
            b = _bucket(pid)
            b["last_error"] = ""
            b["open_until"] = 0
            b["fails"] = 0
        log("BALANCE %s is back to $%.2f - usable again" % (pid, balance))
    return balance


def fetch_price(route):
    """Relay-reported price per weighted million tokens. Uses the relay's own
    billing (`actual_cost`), so it is what this relay really charges - not what
    its price list claims."""
    prefix = (route.get("prefix") or "").rstrip("/")
    err = "no usage data"
    for path in (prefix + "/usage", "/v1/usage"):
        try:
            req = urllib.request.Request(
                route["upstream"] + path,
                headers={"Authorization": "Bearer " + (route.get("auth") or ""),
                         "User-Agent": "curl/8.7.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=PRICE_TIMEOUT) as fh:
                data = json.load(fh)
        except Exception as exc:
            err = str(exc)[:80]
            continue
        parsed = parse_usage(data)
        if not parsed["per_model"]:
            err = "no billable usage yet"
            continue
        parsed["ts"] = time.time()
        parsed["error"] = ""
        parsed["balance"] = read_balance(data)
        return parsed
    return {"ts": time.time(), "per_model": {}, "overall": None, "alltime": None,
            "trend": 1.0, "days": PRICE_DAYS, "daily_days": 0, "error": err}


def refresh_prices(routes, force=False):
    """Refresh every relay's price in parallel; never blocks a request."""
    now = time.time()
    todo = []
    with _stats_lock:
        for pid, route in routes.items():
            if route.get("auth_type") == "oauth" or route.get("price_per_m"):
                continue                      # oauth has no /v1/usage, sidecars declare a price
            if not force and now - (_prices.get(pid) or {}).get("ts", 0) < PRICE_TTL:
                continue
            todo.append((pid, dict(route)))
    if not todo:
        return
    threads = []

    def work(pid, route):
        ent = fetch_price(route)
        with _stats_lock:
            _prices[pid] = ent

    for pid, route in todo:
        th = threading.Thread(target=work, args=(pid, route), daemon=True)
        th.start()
        threads.append(th)
    deadline = time.time() + PRICE_TIMEOUT + 10
    for th in threads:
        th.join(max(0.0, deadline - time.time()))
    for pid, _ in todo:
        ent = _prices.get(pid) or {}
        if "balance" in ent:
            note_balance(pid, ent["balance"])
    bad = [pid for pid, _ in todo if (_prices.get(pid) or {}).get("error")]
    # released even when nothing came back: the cold-start wait must cost at
    # most one request, never one wait per request
    _prices_ready.set()
    log("PRICE refreshed %d relay(s)%s"
        % (len(todo), "" if not bad else ", %d without data" % len(bad)))


def price_index(pid, model, now=None):
    """(price, exact_model_for_this_model) or (None, False) when we have
    nothing trustworthy. Price is a variable: a reading older than
    BRIDGE_PRICE_MAX_AGE is dropped rather than trusted, and the background
    refresh replaces it."""
    now = now or time.time()
    ent = _prices.get(pid) or {}
    if not ent.get("ts") or now - ent["ts"] > PRICE_MAX_AGE:
        return None, False
    per = ent.get("per_model") or {}
    if model and model in per:
        return per[model], True
    if ent.get("overall"):
        return ent["overall"], False
    return None, False


def model_penalty(pid, model):
    """How much this relay costs us in *model quality* for this request.

    Uses the cached /v1/models list only - scoring must never do network I/O.
    """
    if not model:
        return 0.0
    with _models_lock:
        ent = _models_cache.get(pid)
    ids = (ent or {}).get("ids") or set()
    if not ids or model in ids:
        return 0.0
    m = MODEL_RE.match(model)
    if m and any((p := MODEL_RE.match(i)) and p.group(3) == m.group(3)
                 for i in ids):
        return MODEL_ADAPT_PENALTY
    return MODEL_MISSING_PENALTY


def relay_scores(pids, routes, model, affinity_pid=None):
    """Score every relay: lower is better. Unknown data -> mid-pack.

    `affinity_pid` is the relay that served the last turn of this conversation:
    it gets a bonus, because hopping relays throws away the upstream prompt cache
    (and, on stateful relays, the conversation context) - but a clearly better
    relay still wins.
    """
    prices, lats = {}, {}
    for pid in pids:
        prices[pid], _exact = price_index(pid, model)
        lats[pid] = effective_latency(pid)
    known_p = sorted(v for v in prices.values() if v)
    known_l = sorted(v for v in lats.values() if v)
    min_p = known_p[0] if known_p else None
    med_p = known_p[len(known_p) // 2] if known_p else None
    min_l = known_l[0] if known_l else None
    med_l = known_l[len(known_l) // 2] if known_l else None

    out = {}
    for pid in pids:
        b = _stats.get(pid) or {}
        err = b.get("fail", 0.0)
        ok = b.get("ok", 0.0)
        fail_rate = err / (ok + err) if (ok + err) > 0 else 0.0
        price, exact = prices[pid], price_index(pid, model)[1]
        if price and min_p:
            p_norm = price / min_p
        elif med_p and min_p:
            p_norm = med_p / min_p
        else:
            p_norm = 1.0
        lat = lats[pid]
        if lat and min_l:
            l_norm = lat / min_l
        elif med_l and min_l:
            l_norm = med_l / min_l
        else:
            l_norm = 1.0
        pen = model_penalty(pid, model)
        bonus = AFFINITY_BONUS if (affinity_pid and pid == affinity_pid) else 0.0
        out[pid] = {
            "affinity": bool(bonus),
            "score": (W_PRICE * p_norm + W_LATENCY * l_norm + W_FAIL * fail_rate
                      + pen - bonus),
            "price_per_m": price, "price_norm": p_norm,
            "latency": lat, "latency_norm": l_norm,
            "fail_rate": fail_rate, "model_penalty": pen,
            "exact_model": exact,
            "samples": b.get("samples", 0),
            "error": (_prices.get(pid) or {}).get("error", ""),
        }
    return out


def session_key(headers, body_bytes, parsed=None):
    """A stable per-conversation key.

    Codex resends the whole conversation every turn, so the explicit hints come
    first (session-id / thread-id headers, prompt_cache_key) and the fallback is
    a hash of the parts that do not change between turns: the instructions and
    the first input item.
    """
    for name in ("session-id", "thread-id", "x-client-request-id"):
        value = (headers or {}).get(name)
        if value:
            return "h:" + str(value)[:80]
    data = parsed
    if data is None and body_bytes:
        try:
            data = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None
    cache_key = data.get("prompt_cache_key")
    if cache_key:
        return "c:" + str(cache_key)[:80]
    seed = json.dumps([data.get("instructions"),
                       (data.get("input") or [None])[0] if isinstance(data.get("input"), list)
                       else data.get("input")], ensure_ascii=False)[:4000]
    meaningful = seed.strip().strip("[]").replace("null", "").replace(",", "").strip()
    if not meaningful:
        return None
    return "p:" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def affinity_get(key, now=None):
    if not key:
        return None
    now = now or time.time()
    with _affinity_lock:
        ent = _affinity.get(key)
        if not ent:
            return None
        pid, seen = ent
        if now - seen > AFFINITY_TTL:
            _affinity.pop(key, None)
            return None
        return pid


def affinity_remember(key, pid, now=None):
    if not key or not pid:
        return
    now = now or time.time()
    with _affinity_lock:
        _affinity[key] = (pid, now)
        if len(_affinity) > AFFINITY_MAX:
            for stale, _ in sorted(_affinity.items(), key=lambda kv: kv[1][1])[:len(_affinity) - AFFINITY_MAX]:
                _affinity.pop(stale, None)


def pick_explore(pids, scores):
    """The relay most in need of a fresh try, among those that could win.

    Staleness is measured from the last *attempt* (so a permanently failing
    relay does not look forever-unexplored), and the price gate keeps the
    exploration budget away from relays that could never outrank the leaders
    however fast they turned out to be.
    """
    cands = [pid for pid in pids
             if EXPLORE_PRICE_FACTOR <= 0
             or scores.get(pid, {}).get("price_norm", 99) <= EXPLORE_PRICE_FACTOR]
    return min(cands, key=last_attempt) if cands else None


def pick_warmup(pids, scores):
    """A never-*tried* relay that is cheap enough to be worth trying.

    Returns the best-scoring such relay, or None. Each promotion tries one
    relay, so this self-limits to one extra request per untried relay.
    """
    if WARMUP_PRICE_FACTOR <= 0:
        return None
    cands = [pid for pid in pids
             if not last_attempt(pid)
             and scores.get(pid, {}).get("price_norm", 99) <= WARMUP_PRICE_FACTOR]
    return min(cands, key=lambda pid: scores[pid]["score"]) if cands else None


def plan_order(routes, base_order, start_pid, model, remember=True, explore=False,
               affinity_pid=None):
    """The attempt order for one request.

    adaptive (default): cheapest + fastest first, subscription account last,
    ties fall back to the previous plan (stable sort) so it does not thrash.
    fixed: the old behaviour - rotate from the provider CC Switch selected.
    explore: promote the relay whose numbers are stalest, because price and
    speed both drift; used once every BRIDGE_EXPLORE_EVERY requests.
    """
    pids = [p for p in base_order if p in routes]
    oauth = [p for p in pids if routes[p].get("auth_type") == "oauth"]
    normal = [p for p in pids if p not in oauth]
    if affinity_pid and (affinity_pid not in normal or breaker_open(affinity_pid)):
        affinity_pid = None            # the sticky relay is gone or parked
    scores = relay_scores(normal, routes, model, affinity_pid=affinity_pid)

    if ORDER_MODE == "fixed":
        if start_pid in normal:
            i = normal.index(start_pid)
            normal = normal[i:] + normal[:i]
        ordered = normal
    else:
        prev = [p for p in _last_plan["order"] if p in normal]
        seed = prev + [p for p in normal if p not in prev]
        if RESPECT_START:
            ordered = sorted([p for p in seed if p != start_pid],
                             key=lambda p: scores[p]["score"])
            if start_pid in scores:
                ordered.insert(0, start_pid)
        else:
            ordered = sorted(seed, key=lambda p: scores[p]["score"])
        if len(ordered) > 1 and not affinity_pid:
            # A conversation that already has its relay keeps it: warm-up and
            # exploration are for traffic that is not mid-conversation (Codex
            # sessions are long, and hijacking one costs its prompt cache).
            pid, why = pick_warmup(ordered, scores), "WARMUP"
            if pid is None and explore:
                pid, why = pick_explore(ordered, scores), "EXPLORE"
            if pid and pid != ordered[0]:
                tried = last_attempt(pid)
                note = ("never tried" if not tried
                        else "last tried %.0fs ago" % (time.time() - tried))
                ordered.remove(pid)
                ordered.insert(0, pid)
                log("%s promoting %s (%s)" % (why, routes[pid].get("name"), note))
    ordered = ordered + oauth
    if ORDER_MODE != "fixed":
        # a parked relay (breaker open) is tried last - including parked
        # subscription accounts in the pool
        now = time.time()
        with _stats_lock:
            broken = [p for p in ordered
                      if (_stats.get(p) or {}).get("open_until", 0) > now]
        if broken and len(broken) < len(ordered):
            ordered = [p for p in ordered if p not in broken] + broken
    if remember:
        _last_plan["order"] = ordered
    return ordered, scores


def _ago(seconds):
    if seconds is None:
        return "?"
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds // 60)
    return "%.1fh" % (seconds / 3600.0)


def log_plan(ordered, scores, model, sticky=None):
    """Log the ranking once per change, not once per request."""
    sig = "|".join(ordered) + "#" + (model or "")
    if sig == _last_plan["signature"]:
        return
    _last_plan["signature"] = sig
    parts = []
    for pid in ordered:
        s = scores.get(pid)
        if not s:
            parts.append(pid[:8])
            continue
        price = "%.2f/M" % s["price_per_m"] if s["price_per_m"] else "?/M"
        lat = "%.0fms" % (s["latency"] * 1000) if s["latency"] else "?"
        parts.append("%s[%s %s fail%.2f p%.1f]"
                     % (pid[:8], price, lat, s["fail_rate"], s["score"]))
    log("ORDER %s -> %s" % (model or "(no model)", " > ".join(parts)))


def status_snapshot(routes, base_order, model):
    ordered, scores = plan_order(routes, base_order, None, model, remember=False)
    now = time.time()
    # the ranking is conversation-agnostic, so report which relays are currently
    # pinned by live conversations instead of faking a per-conversation score
    with _affinity_lock:
        sticky = {}
        for key, (pid, seen) in list(_affinity.items()):
            if now - seen > AFFINITY_TTL:
                _affinity.pop(key, None)
                continue
            sticky[pid] = sticky.get(pid, 0) + 1
    rows = []
    for pid in ordered:
        route = routes[pid]
        s = scores.get(pid) or {}
        ent = _prices.get(pid) or {}
        b = _stats.get(pid) or {}
        price = s.get("price_per_m")
        rows.append({
            "id": pid, "name": route.get("name"), "upstream": route.get("upstream"),
            "auth_type": route.get("auth_type") or "bearer",
            "score": round(s["score"], 3) if s else None,
            "price_per_m": round(price, 4) if price else None,
            "price_norm": round(s["price_norm"], 3) if s else None,
            "price_trend": round(ent.get("trend", 1.0), 3) if ent else None,
            "price_age_s": int(now - ent["ts"]) if ent.get("ts") else None,
            "price_note": (s.get("error") or ent.get("error") or ""),
            "latency_ms": round(s["latency"] * 1000, 1) if s.get("latency") else None,
            "latency_age_s": (int(now - (b.get("lat_ts") or 0))
                              if b.get("lat_ts") else None),
            "latency_samples": b.get("samples", 0),
            "hour_samples": (b.get("byhour") or {}).get(
                str(time.localtime(now).tm_hour), {}).get("samples", 0),
            "fail_rate": round(s["fail_rate"], 3) if s else None,
            "model_penalty": s.get("model_penalty"),
            "balance": b.get("balance"),
            "fixed_price": bool(route.get("price_per_m")),
            "affinity": sticky.get(pid, 0) > 0,
            "affinity_sessions": sticky.get(pid, 0),
            "breaker": breaker_state(pid),
            "breaker_open_s": max(0, int((b.get("open_until") or 0) - now)),
            "consecutive_fails": b.get("fails", 0),
            "last_error": b.get("last_error", ""),
        })
        if route.get("auth_type") == "oauth":
            quota = b.get("quota") or {}
            creds = load_oauth(route.get("auth_file") or "")
            rows[-1]["quota"] = ({
                "used_percent": round(quota.get("used_percent", 0), 1),
                "plan_type": quota.get("plan_type"),
                "credits": quota.get("credits"),
                "reset_in_s": (int(quota["reset_after_s"])
                               if quota.get("reset_after_s") else None),
                "observed_age_s": int(now - quota.get("ts", now)) if quota else None,
            } if quota else None)
            rows[-1]["token_expires_in_s"] = (int(creds[2] - now) if creds and creds[2] else None)
    return {"mode": ORDER_MODE, "respect_start": RESPECT_START,
            "model": model, "order": ordered, "routes": rows,
            "price_days": PRICE_DAYS,
            "price_max_age_s": int(PRICE_MAX_AGE),
            "latency_max_age_s": int(LATENCY_MAX_AGE),
            "explore_every": EXPLORE_EVERY}


def save_state():
    with _stats_lock:
        snap = {"ts": time.time(), "stats": _stats, "prices": _prices}
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snap, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log("state save failed: %s" % exc)


def load_state():
    try:
        with open(STATE_FILE) as fh:
            snap = json.load(fh)
    except Exception:
        return
    age = time.time() - float(snap.get("ts") or 0)
    if age > STATE_TTL:
        log("state ignored (%.0fh old)" % (age / 3600))
        return
    with _stats_lock:
        _stats.update(snap.get("stats") or {})
        for pid, ent in (snap.get("prices") or {}).items():
            if ent.get("per_model") and age <= PRICE_TTL:
                _prices[pid] = ent
    if _prices:
        _prices_ready.set()
    log("state restored (%d relay stats, %d price sets)"
        % (len(_stats), len(_prices)))


def prune_state():
    """Forget relays that are no longer in routes.json (deleted in CC Switch)."""
    with _state_lock:
        live = set(_state["routes"])
    if not live:
        return
    with _stats_lock:
        removed = [pid for pid in _stats if pid not in live]
        removed += [pid for pid in _prices if pid not in live]
        for pid in set(removed):
            _stats.pop(pid, None)
            _prices.pop(pid, None)
    return len(set(removed))


def housekeeping():
    """Background price refresh + state save. Never touches request handling."""
    while True:
        try:
            with _state_lock:
                routes = dict(_state["routes"])
            if routes:
                refresh_prices(routes)
                prune_state()
            save_state()
        except Exception as exc:                                   # pragma: no cover
            log("housekeeping failed: %r" % exc)
        time.sleep(HOUSEKEEPING_SECONDS)


def route_models(pid, route):
    """Cached /v1/models for one relay. Empty set means 'unknown'."""
    if route.get("auth_type") == "oauth":
        return set()
    now = time.time()
    with _models_lock:
        ent = _models_cache.get(pid)
        if ent and now - ent["ts"] < MODELS_TTL:
            return ent["ids"]
    ids = set()
    try:
        req = urllib.request.Request(
            route["upstream"] + route["prefix"] + "/models",
            headers={"Authorization": "Bearer " + (route.get("auth") or ""),
                     # some relays answer 403 to the default python-urllib UA
                     "User-Agent": "curl/8.7.1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as fh:
            ids = {m.get("id") for m in (json.load(fh).get("data") or []) if m.get("id")}
    except Exception as exc:
        log("MODELS %s unavailable (%s) - skipping adaptation" % (route["name"], exc))
    with _models_lock:
        _models_cache[pid] = {"ids": ids, "ts": now}
    return ids


def apply_model_map(body, pid, route):
    """Rewrite the client's model to the name this route really serves.

    A sidecar in front of a chat-completions-only provider has no `gpt-*`
    models at all, so guessing from /v1/models cannot work: the route declares
    `model_map` (`{"gpt-6.1-sol": "deepseek-chat", "*": "deepseek-chat"}`).
    Explicit mapping wins; the caller skips further adaptation afterwards.
    """
    mapping = route.get("model_map") or {}
    if not body or not mapping:
        return body, None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return body, None
    if not isinstance(data, dict):
        return body, None
    wanted = data.get("model")
    if not isinstance(wanted, str) or not wanted:
        return body, None
    target = mapping.get(wanted) or mapping.get("*")
    if not target or target == wanted:
        return body, None
    data["model"] = target
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), target


def adapt_model(body, pid, route):
    """Swap the requested model for the newest one this relay does serve."""
    if not body or route.get("auth_type") == "oauth":
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(data, dict):
        return body
    wanted = data.get("model")
    if not isinstance(wanted, str) or not wanted:
        return body
    if route.get("model_map"):
        return body                       # explicit mapping handled by the caller

    ids = route_models(pid, route)
    if not ids or wanted in ids:
        return body                      # unknown list, or nothing to do

    m = MODEL_RE.match(wanted)
    if not m:
        return body
    family = m.group(3)
    cands = []
    for mid in ids:
        p = MODEL_RE.match(mid)
        if p and p.group(3) == family:
            cands.append((int(p.group(1)), int(p.group(2) or 0), mid))
    if not cands:
        return body                      # relay has none of this family at all
    cands.sort(reverse=True)
    best = cands[0][2]
    data["model"] = best
    log("ADAPT %s: %s -> %s (this relay does not serve %s)"
        % (route["name"], wanted, best, wanted))
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


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


def await_first_content(resp, timeout, limit=None):
    """Wait until the stream shows *real model output* before committing.

    A relay that accepts the request and then emits only bookkeeping events
    (`response.created`, `in_progress`, keep-alives) is the classic "Codex looks
    hung" failure: reading a single byte is not enough to rule it out. Before
    the first content frame we can still rotate to the next relay, so this
    buffers (bounded) until a content frame - or a terminal event, which counts
    as a complete, possibly empty answer.

    -> (ok, buffered_bytes, had_content)
    """
    limit = limit or TTFT_BUFFER_MAX
    buf = b""
    deadline = time.time() + timeout
    while len(buf) < limit:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            resp.fp.raw._sock.settimeout(remaining)
        except Exception:
            pass
        try:
            # read1() returns as soon as one chunk is available; read(n) blocks
            # until n bytes arrive, which would batch the stream in 8 KiB blocks
            chunk = (resp.read1(4096) if hasattr(resp, "read1")
                     else resp.read(4096))
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        buf += chunk
        if any(marker in buf for marker in TTFT_MARKERS):
            return True, buf, True
        if any(marker in buf for marker in STREAM_END_MARKERS):
            return True, buf, False
    return False, buf, False


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

    def _stream(self, status, headers, resp, head=b"", scanner=None):
        """Relay a stream.

        Returns "complete", "client-gone" or "stalled". Once any byte has gone
        out we can no longer rotate, so a stall is ended with a well-formed
        response.failed frame rather than a silently truncated body.
        """
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
                if scanner:
                    scanner.feed(head)
            chunks = 0
            stalled = False
            while True:
                window = STREAM_STALL * (STREAM_STALL_MULT
                                         if chunks >= STREAM_STALL_AFTER else 1.0)
                try:
                    resp.fp.raw._sock.settimeout(window)
                except Exception:
                    pass
                try:
                    # read1(): forward each upstream chunk immediately instead of
                    # waiting for 8 KiB (latency matters for SSE deltas)
                    chunk = (resp.read1(8192) if hasattr(resp, "read1")
                             else resp.read(8192))
                except (socket.timeout, OSError):
                    stalled = True
                    break
                if not chunk:
                    break
                chunks += 1
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                if scanner:
                    scanner.feed(chunk)
            if stalled:
                frame = (b"event: response.failed\n"
                         b'data: {"type":"response.failed","response":{"status":"failed",'
                         b'"error":{"code":"stream_stalled","type":"upstream_error",'
                         b'"message":"retry bridge: upstream stopped sending data"}}}\n\n')
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
                if scanner:
                    scanner.feed(frame)
                log("STALL %s: no data for %.0fs, ended the stream with response.failed"
                    % (self.path, window))
            self.wfile.write(b"0\r\n\r\n")
            return "stalled" if stalled else "complete"
        except OSError as exc:
            self.close_connection = True
            log("CLIENT-GONE / upstream cut mid-stream: %s" % exc)
            return "client-gone"

    def _status(self, split):
        """GET /__bridge/status[?model=...][&text=1] - the live ranking."""
        client = (self.client_address or ("",))[0]
        if client not in ("127.0.0.1", "::1"):
            self._send(403, [("Content-Type", "application/json")],
                       b'{"error":{"message":"local only"}}')
            return
        query = {}
        for part in split.query.split("&"):
            if "=" in part:
                key, _, value = part.partition("=")
                query[key] = unquote(value)
        model = query.get("model") or None
        with _state_lock:
            routes = dict(_state["routes"])
            base_order = list(_state["order"])
            attempts = _state["attempts"]
        snap = status_snapshot(routes, base_order, model)
        snap["attempts"] = attempts
        if "text" in query:
            lines = ["%-22s %7s %10s %6s %9s %5s %6s %5s  %s"
                     % ("中转", "score", "$/加权M", "价格趋势", "首字节", "样本",
                        "失败率", "模型", "备注")]
            for row in snap["routes"]:
                if row["auth_type"] == "oauth":
                    q = row.get("quota")
                    note = "oauth 账号，固定队尾"
                    if q:
                        note += "，已用 %.0f%%%s" % (q["used_percent"], (
                            "，%s后重置" % _ago(q["reset_in_s"]) if q.get("reset_in_s") else ""))
                        if q.get("plan_type"):
                            note += "（%s）" % q["plan_type"]
                    elif row.get("token_expires_in_s") is not None:
                        note += "，token %s后过期" % _ago(max(0, row["token_expires_in_s"]))
                    if row.get("breaker") == "open":
                        note += " · 熔断 %ds" % row.get("breaker_open_s", 0)
                    lines.append("%-22s %7s %10s %6s %9s %5s %6s %5s  %s"
                                 % (row["name"][:22], "-", "-", "-", "-", "-", "-",
                                    "-", note))
                    continue
                age = row["price_age_s"]
                if row.get("breaker") == "open":
                    row_note = "熔断 %ds" % row.get("breaker_open_s", 0)
                    if row["price_note"]:
                        row_note += " · " + row["price_note"]
                else:
                    row_note = row["price_note"] or ""
                note_extra = ""
                if row.get("fixed_price"):
                    note_extra = "固定单价"
                if row.get("balance") is not None:
                    note_extra = (note_extra + " · " if note_extra else "") + "余额 $%.2f" % row["balance"]
                if row.get("affinity"):
                    note_extra = ((note_extra + " · " if note_extra else "")
                                  + "会话粘住×%d" % row.get("affinity_sessions", 1))
                lines.append("%-22s %7s %10s %6s %9s %5s %6s %5s  %s"
                             % (row["name"][:22],
                                row["score"] if row["score"] is not None else "-",
                                ("%.3f" % row["price_per_m"]) if row["price_per_m"] else "-",
                                ("x%.2f" % row["price_trend"]) if row["price_trend"] else "-",
                                ("%.0fms" % row["latency_ms"]) if row["latency_ms"] else "-",
                                row["latency_samples"] or "-",
                                ("%.2f" % row["fail_rate"]) if row["fail_rate"] is not None else "-",
                                ("-%.1f" % row["model_penalty"]) if row["model_penalty"] else "ok",
                                " · ".join(x for x in (
                                    row_note,
                                    note_extra,
                                    ("价格 %s前" % _ago(age)) if (age is not None and not row_note) else "") if x)))
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self._send(200, [("Content-Type", "text/plain; charset=utf-8")], body)
            return
        body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
        self._send(200, [("Content-Type", "application/json")], body)

    def _handle(self, method):
        split = urlsplit(self.path)
        if split.path in ("/__bridge/status", "/__bridge/ranking"):
            self._status(split)
            return
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

        with _state_lock:
            routes = dict(_state["routes"])
            attempts = _state["attempts"]
            base_order = list(_state["order"])

        wanted = None
        parsed_body = None
        try:
            parsed_body = json.loads(request_body.decode("utf-8")) or {}
            wanted = parsed_body.get("model")
        except Exception:
            parsed_body = None
        conv_key = session_key(base_headers, request_body, parsed_body)
        _explore["n"] += 1
        explore = EXPLORE_EVERY > 0 and _explore["n"] % EXPLORE_EVERY == 0
        if ORDER_MODE != "fixed" and not _prices_ready.is_set():
            # cold start (no fresh bridge-state.json): wait a moment for the
            # first price refresh so the very first request is already ordered
            # by price, instead of falling back to the fixed list
            _prices_ready.wait(PRICE_WAIT)
        sticky = affinity_get(conv_key)
        ordered, scores = plan_order(routes, base_order, start_pid, wanted,
                                     explore=explore, affinity_pid=sticky)
        log_plan(ordered, scores, wanted, sticky=sticky)
        if sticky and ordered and ordered[0] != sticky:
            log("AFFINITY %s: leaving %s for %s"
                % ((conv_key or "?")[:24], routes[sticky]["name"],
                   routes[ordered[0]]["name"]))
        mount_name = (routes.get(start_pid) or {}).get("name") or start_pid

        start_time = time.time()
        last = {"status": None, "headers": [("Content-Type", "application/json")], "body": b""}

        counts = {}          # attempts spent per route (honours max_attempts)
        oauth_retried = set()   # accounts already force-refreshed for this request
        done = 0
        skipped = 0
        cursor = 0
        netfails = 0         # consecutive connection/TLS-level failures
        while done < attempts and skipped < len(ordered):
            pid = ordered[cursor % len(ordered)]
            cursor += 1
            route = routes[pid]

            cap = route.get("max_attempts")
            if cap is not None and counts.get(pid, 0) >= cap:
                skipped += 1                       # route budget spent
                continue

            oauth_fp = None
            headers = dict(base_headers)
            if route.get("auth_type") == "oauth":
                auth_file = route.get("auth_file") or ""
                cred = load_oauth(auth_file)
                if not cred:
                    skipped += 1
                    log("SKIP  %s (no oauth credentials in %s)"
                        % (route["name"], auth_file or "?"))
                    continue
                token, account_id, exp, oauth_fp = cred
                # proactive refresh only when the token is really dead; in "on"
                # mode also refresh inside the skew window
                stale_after = time.time() + (OAUTH_SKEW if OAUTH_REFRESH_MODE == "on" else 0)
                if exp and exp <= stale_after and OAUTH_REFRESH_MODE != "off":
                    fresh = oauth_refresh(auth_file, oauth_fp, reason="expired token")
                    if fresh:
                        token, account_id, exp, oauth_fp = fresh
                if exp and exp <= time.time():
                    skipped += 1
                    log("SKIP  %s (access token expired and refresh did not help)"
                        % route["name"])
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
            else:
                # fit the model to what this relay actually serves: an explicit
                # per-route mapping first, then /v1/models guessing
                attempt_body, mapped = apply_model_map(request_body, pid, route)
                if mapped:
                    log("MAP   %s: %s -> %s (route model_map)"
                        % (route["name"], wanted, mapped))
                else:
                    attempt_body = adapt_model(request_body, pid, route)
            if attempt_body is not request_body:
                headers["Content-Length"] = str(len(attempt_body))

            conn = None
            attempt_started = time.time()
            try:
                conn, resp = open_upstream(method, url, headers, attempt_body)
                status = resp.status
                raw_headers = resp.getheaders()
                ctype = (resp.getheader("Content-Type") or "").lower()
                if route.get("auth_type") == "oauth":
                    # quota comes free with every answer, streaming or not
                    note_quota(pid, raw_headers)

                if status == 200 and "event-stream" in ctype:
                    # Do not commit to this relay until it actually produces a
                    # byte. A relay that accepts the request and then stalls (or
                    # closes immediately) is the classic cause of Codex's
                    # "stream disconnected before completion"; rotating to the
                    # next relay is far better than handing the client a
                    # truncated body.
                    ok, head, had_content = await_first_content(
                        resp, FIRST_BYTE_TIMEOUT)
                    if not ok:
                        log("RETRY %s %s -> no content frame via %s (attempt %d/%d)"
                            % (method, split.path, route["name"], done, attempts))
                        last = {"status": 200,
                                "headers": [("Content-Type", "application/json")],
                                "body": json.dumps({"error": {
                                    "message": "retry bridge: empty or stalled stream",
                                    "type": "upstream_error"}}).encode()}
                        netfails += 1
                        record_attempt(pid, failed=True)
                        state = breaker_charge(pid, "timeout")
                        log_request(mount_name, route["name"], wanted, done, 200, None,
                                    "stalled", mount_id=start_pid, relay_id=pid,
                                    error_kind="no_content", breaker=state)
                        conn.close()
                        conn = None
                    else:
                        # first byte is what "faster to call" really means here
                        ttfb = time.time() - attempt_started
                        record_attempt(pid, ttfb)
                        log("OK    %s %s -> %s via %s (attempt %d/%d) model=%s"
                            % (method, split.path, status, route["name"], done,
                               attempts, wanted or "?"))
                        scanner = UsageScanner()
                        outcome = self._stream(status, raw_headers, resp, head, scanner)
                        if outcome == "complete":
                            breaker_ok(pid)
                        elif outcome == "stalled":
                            breaker_charge(pid, "timeout")
                        complete = (outcome == "complete")
                        log_request(mount_name, route["name"], wanted, done, status,
                                    ttfb, "ok", pid=pid, tokens=scanner.tokens(),
                                    stream_complete=complete,
                                    response_id=scanner.response_id,
                                    mount_id=start_pid, relay_id=pid)
                        if complete:
                            affinity_remember(conv_key, pid)
                        conn.close()
                        return

                if (status == 401 and route.get("auth_type") == "oauth"
                        and OAUTH_REFRESH_MODE != "off" and pid not in oauth_retried):
                    oauth_retried.add(pid)
                    fresh = oauth_refresh(route.get("auth_file") or "", oauth_fp,
                                          reason="401 from the account backend")
                    if fresh:
                        log("OAUTH 401 via %s -> refreshed, retrying the same account"
                            % route["name"])
                        conn.close()
                        conn = None
                        done = max(0, done - 1)      # a token refresh is not an attempt
                        cursor -= 1                  # re-pick the same relay
                        continue

                body = read_all(resp)
                conn.close()
                conn = None

                retryable = status in RETRY_STATUS or is_retryable_body(body)

                if not retryable:
                    seconds = None
                    tokens = None
                    response_id = None
                    if status == 200:
                        seconds = time.time() - attempt_started
                        record_attempt(pid, seconds)
                        breaker_ok(pid)
                        try:
                            payload = json.loads(body.decode("utf-8")) or {}
                            tokens = normalise_usage(payload.get("usage"))
                            rid = payload.get("id")
                            response_id = rid if isinstance(rid, str) else None
                        except Exception:
                            tokens = None
                    log_request(mount_name, route["name"], wanted, done, status,
                                seconds, "pass", pid=pid, tokens=tokens,
                                response_id=response_id, mount_id=start_pid,
                                relay_id=pid)
                    if status == 200:
                        affinity_remember(conv_key, pid)
                    log("PASS  %s %s -> %s via %s (attempt %d) model=%s"
                        % (method, split.path, status, route["name"], done,
                           wanted or "?"))
                    self._send(status, raw_headers, body)
                    return

                record_attempt(pid, failed=True)
                kind, retry_after, reset = classify_error(status, raw_headers, body)
                if is_retryable_body(body):
                    kind = "upstream"
                state = "noop"
                if kind not in ("bad_request", "model_missing"):
                    state = breaker_charge(pid, kind, retry_after, reset)
                if retry_after:
                    backoff_hint = min(RETRY_AFTER_MAX, retry_after)
                else:
                    backoff_hint = None
                log_request(mount_name, route["name"], wanted, done, status, None,
                            "retry", mount_id=start_pid, relay_id=pid,
                            error_kind=kind, breaker=state)
                last = {"status": status, "headers": raw_headers, "body": body}
                netfails = 0
                snippet = body[:120].decode("utf-8", "replace").replace("\n", " ")
                log("RETRY %s %s -> %s via %s (attempt %d/%d) model=%s kind=%s breaker=%s %s"
                    % (method, split.path, status, route["name"], done, attempts,
                       wanted or "?", kind, state, snippet))
                if backoff_hint:
                    time.sleep(backoff_hint)
            except (socket.timeout, OSError) as exc:
                record_attempt(pid, failed=True)
                state = breaker_charge(pid, "transport")
                log_request(mount_name, route["name"], wanted, done, None, None,
                            "network-error", mount_id=start_pid, relay_id=pid,
                            error_kind="transport", breaker=state)
                last = {"status": None,
                        "headers": [("Content-Type", "application/json")],
                        "body": json.dumps({"error": {
                            "message": "retry bridge: unreachable: %s" % exc,
                            "type": "upstream_error"}}).encode()}
                netfails += 1
                log("RETRY %s %s -> network error via %s (attempt %d/%d, net-streak %d): %s"
                    % (method, split.path, route["name"], done, attempts, netfails, exc))
            except Exception as exc:                                   # pragma: no cover
                record_attempt(pid, failed=True)
                log_request(mount_name, route["name"], wanted, done, None, None,
                            "error", mount_id=start_pid, relay_id=pid)
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
    load_state()
    load_routes(force=True)
    threading.Thread(target=housekeeping, daemon=True).start()
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "15888"))
    server = BridgeServer((host, port), Handler)
    log("retry bridge listening on %s:%d (order=%s, attempts=%d, backlog=%d, "
        "first-byte timeout=%.0fs)"
        % (host, port, ORDER_MODE, _state["attempts"], server.request_queue_size,
           FIRST_BYTE_TIMEOUT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
