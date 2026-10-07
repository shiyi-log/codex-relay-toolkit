#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the subscription-account pack: OAuth refresh, safe persistence,
401 -> refresh -> retry-once, quota headers -> parked account, account pool.

Ported from thezillo/codex-proxy (single-flight refresh, read-modify-write of
the rotated token at 0600, usage_limit_reached vs throttle, quota from the
x-codex-* response headers) with one addition: a cross-process flock and a
"someone else already refreshed" check, because our auth.json is shared with the
running Codex app.

    python3 tests/test_oauth_accounts.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(ROOT, "bridge.py")
SETUP = os.path.join(ROOT, "setup.py")
sys.path.insert(0, ROOT)
import bridge as bridge_mod                                   # noqa: E402

MODEL = "gpt-6.1-sol"
ACCESS = (b"event: response.output_text.delta\n"
          b'data: {"type":"response.output_text.delta","delta":"from the account"}\n\n'
          b"event: response.completed\n"
          b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def b64url(obj):
    import base64
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(exp):
    return "%s.%s.sig" % (b64url({"alg": "none"}), b64url({"exp": exp}))


def write_auth(path, access, refresh="rt-1", extra=None):
    data = {"auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": access, "refresh_token": refresh,
                       "account_id": "acct-1", "id_token": "id-1"},
            "last_refresh": "2026-10-01T00:00:00Z",
            "something_we_do_not_know": {"keep": "me"}}
    data.update(extra or {})
    with open(path, "w") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)
    return data


class MockOAuth:
    """Minimal /oauth/token endpoint that hands out rotating tokens."""

    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(body)
                if not outer.respond:
                    self.send_response(400)
                    payload = b'{"error":"invalid_grant"}'
                else:
                    self.send_response(200)
                    payload = json.dumps({
                        "access_token": make_jwt(time.time() + 3600),
                        "refresh_token": "rt-%d" % len(outer.calls),
                        "id_token": "id-%d" % len(outer.calls)}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.respond = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def issuer(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class AccountBackend:
    """Pretends to be the ChatGPT Codex backend.

    mode 'expired': the old token gets 401 until a refresh happened.
    mode 'quota': answers 200 but reports a spent weekly window in headers.
    """

    def __init__(self, mode="expired", reject_token=None):
        self.mode = mode
        self.reject_token = reject_token
        self.hits = 0
        self.unauthorized = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, code, payload, extra=()):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {"data": [{"id": MODEL}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits += 1
                token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                if outer.mode == "expired" and token == outer.reject_token:
                    outer.unauthorized += 1
                    return self._json(401, {"error": {"message": "token expired",
                                                      "type": "invalid_request_error"}})
                extra = []
                if outer.mode == "quota":
                    extra = [("x-codex-secondary-used-percent", "100"),
                             ("x-codex-secondary-window-minutes", "10080"),
                             ("x-codex-secondary-reset-after-seconds", "1800"),
                             ("x-codex-plan-type", "plus"),
                             ("x-codex-credits-balance", "0")]
                body = ACCESS
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RefreshUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oauth-")
        self.auth = os.path.join(self.tmp, "auth.json")
        self.oauth = MockOAuth()
        self._env(issuer=self.oauth.issuer, mode="reactive")

    def tearDown(self):
        self.oauth.stop()
        self._env(issuer="https://auth.openai.com", mode="reactive")

    def _env(self, issuer=None, mode=None, dirs=None, skew=None):
        if issuer:
            bridge_mod.OAUTH_ISSUER = issuer
        if mode:
            bridge_mod.OAUTH_REFRESH_MODE = mode
        if dirs is not None:
            bridge_mod.OAUTH_DIRS = dirs
        if skew is not None:
            bridge_mod.OAUTH_SKEW = skew

    def test_refresh_writes_rotated_tokens_and_preserves_everything_else(self):
        old = make_jwt(time.time() - 10)
        write_auth(self.auth, old)
        creds = bridge_mod.oauth_refresh(self.auth, bridge_mod.auth_fingerprint(
            bridge_mod.read_auth(self.auth)), reason="test")
        self.assertIsNotNone(creds)
        token, account_id, exp, fp = creds
        self.assertTrue(token.startswith("new") or exp > time.time())
        on_disk = bridge_mod.read_auth(self.auth)
        self.assertEqual(on_disk["something_we_do_not_know"], {"keep": "me"})
        self.assertIsNone(on_disk["OPENAI_API_KEY"])
        self.assertEqual(on_disk["tokens"]["refresh_token"], "rt-1")   # rotated
        self.assertEqual(on_disk["tokens"]["account_id"], "acct-1")    # kept
        self.assertNotEqual(on_disk["tokens"]["access_token"], old)
        self.assertEqual(len(self.oauth.calls), 1)

    def test_file_mode_is_0600(self):
        write_auth(self.auth, make_jwt(time.time() - 10))
        bridge_mod.oauth_refresh(self.auth, None, reason="test")
        self.assertEqual(oct(os.stat(self.auth).st_mode & 0o777), "0o600")

    def test_someone_else_already_refreshed_is_not_rotated_again(self):
        write_auth(self.auth, make_jwt(time.time() - 10))
        stale_fp = "deadbeefdeadbeef"          # what we tried to use
        creds = bridge_mod.oauth_refresh(self.auth, stale_fp, reason="test")
        self.assertIsNotNone(creds)            # we use the on-disk token
        self.assertEqual(self.oauth.calls, [])  # and do NOT call the endpoint

    def test_mode_off_never_touches_the_file(self):
        write_auth(self.auth, make_jwt(time.time() - 10))
        before = open(self.auth).read()
        self._env(mode="off")
        self.assertIsNone(bridge_mod.oauth_refresh(self.auth, None, reason="test"))
        self.assertEqual(open(self.auth).read(), before)
        self.assertEqual(self.oauth.calls, [])

    def test_failed_refresh_returns_none_and_leaves_the_file_alone(self):
        write_auth(self.auth, make_jwt(time.time() - 10))
        before = open(self.auth).read()
        self.oauth.respond = False
        self.assertIsNone(bridge_mod.oauth_refresh(self.auth, None, reason="test"))
        self.assertEqual(open(self.auth).read(), before)


class QuotaHeaderTest(unittest.TestCase):
    def test_worst_window_wins_and_reset_is_kept(self):
        headers = [("x-codex-primary-used-percent", "10"),
                   ("x-codex-primary-window-minutes", "300"),
                   ("x-codex-primary-reset-after-seconds", "100"),
                   ("x-codex-secondary-used-percent", "100"),
                   ("x-codex-secondary-window-minutes", "10080"),
                   ("x-codex-secondary-reset-after-seconds", "1800"),
                   ("x-codex-plan-type", "plus")]
        snap = bridge_mod.parse_quota_headers(headers)
        self.assertEqual(snap["used_percent"], 100)
        self.assertEqual(snap["reset_after_s"], 1800)
        self.assertEqual(snap["plan_type"], "plus")

    def test_window_the_plan_lacks_is_ignored(self):
        snap = bridge_mod.parse_quota_headers([
            ("x-codex-primary-used-percent", "0"),
            ("x-codex-primary-window-minutes", "0")])
        self.assertIsNone(snap)

    def test_spent_account_gets_parked(self):
        bridge_mod._stats.clear()
        snap = bridge_mod.note_quota("acct", [
            ("x-codex-primary-used-percent", "100"),
            ("x-codex-primary-window-minutes", "300"),
            ("x-codex-primary-reset-after-seconds", "1200")])
        self.assertEqual(snap["used_percent"], 100)
        self.assertTrue(bridge_mod.breaker_open("acct"))
        self.assertEqual(bridge_mod._stats["acct"]["last_error"], "quota")


class AccountFlowTest(unittest.TestCase):
    """End to end: expired token -> 401 -> refresh -> retry once -> 200."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oauthflow-")
        self.auth = os.path.join(self.tmp, "auth.json")
        self.expired_token = make_jwt(time.time() - 60)
        write_auth(self.auth, self.expired_token)              # expired
        self.oauth = MockOAuth()
        self.backend = AccountBackend(mode="expired",
                                      reject_token=self.expired_token)
        self.port = free_port()
        routes = {"port": self.port, "attempts": 3, "order": ["acct"],
                  "routes": {"acct": {"mount": "/p/acct", "prefix": "",
                                      "name": "my account",
                                      "upstream": self.backend.url,
                                      "auth_type": "oauth",
                                      "auth_file": self.auth}}}
        self.routes_path = os.path.join(self.tmp, "routes.json")
        with open(self.routes_path, "w") as fh:
            json.dump(routes, fh)
        env = dict(os.environ,
                   BRIDGE_ROUTES=self.routes_path, BRIDGE_PORT=str(self.port),
                   BRIDGE_STATE=os.path.join(self.tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(self.tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(self.tmp, "requests.jsonl"),
                   BRIDGE_ORDER_MODE="fixed", BRIDGE_VERBOSE="0",
                   BRIDGE_OAUTH_ISSUER=self.oauth.issuer,
                   BRIDGE_OAUTH_REFRESH="reactive",
                   BRIDGE_FIRST_BYTE_TIMEOUT="5", BRIDGE_BACKOFF="0.01")
        self.proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % self.port, timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.oauth.stop()
        self.backend.stop()

    def post(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/p/acct/responses" % self.port,
            data=json.dumps({"model": MODEL, "input": "hi"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            return fh.status, fh.read()

    def test_expired_token_is_refreshed_and_the_request_succeeds(self):
        status, body = self.post()
        self.assertEqual(status, 200)
        self.assertIn(b"from the account", body)
        self.assertEqual(len(self.oauth.calls), 1)          # refreshed once
        on_disk = bridge_mod.read_auth(self.auth)
        self.assertEqual(on_disk["tokens"]["refresh_token"], "rt-1")
        self.assertEqual(on_disk["something_we_do_not_know"], {"keep": "me"})

    def test_quota_headers_park_the_account_and_show_in_status(self):
        self.backend.mode = "quota"
        self.post()
        snap = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/__bridge/status" % self.port, timeout=5).read())
        row = [r for r in snap["routes"] if r["id"] == "acct"][0]
        self.assertEqual(row["quota"]["used_percent"], 100)
        self.assertEqual(row["quota"]["plan"], "plus")   # unified snapshot field
        self.assertEqual(row["breaker"], "open")
        self.assertEqual(row["last_error"], "quota")


class PoolDiscoveryTest(unittest.TestCase):
    def test_dirs_and_one_level_of_subdirs_are_discovered(self):
        tmp = tempfile.mkdtemp(prefix="pool-")
        write_auth(os.path.join(tmp, "auth.json"), make_jwt(time.time() + 60))
        for name in ("acct2", "acct3"):
            sub = os.path.join(tmp, name)
            os.makedirs(sub)
            write_auth(os.path.join(sub, "auth.json"), make_jwt(time.time() + 60))
        os.makedirs(os.path.join(tmp, "nested", "deeper"))       # ignored
        write_auth(os.path.join(tmp, "nested", "deeper", "auth.json"),
                   make_jwt(time.time() + 60))
        env = dict(os.environ, BRIDGE_OAUTH_DIRS=tmp)
        out = subprocess.run(
            [sys.executable, "-c",
             "import os,sys; sys.path.insert(0,%r); import setup; "
             "print('\\n'.join(setup.discover_accounts()))" % ROOT],
            env=env, capture_output=True, text=True)
        found = [l for l in out.stdout.splitlines() if l.strip()]
        self.assertEqual(len(found), 3)
        self.assertTrue(any(p.endswith("acct2/auth.json") for p in found))
        self.assertTrue(any(p.endswith("acct3/auth.json") for p in found))

    def test_route_id_is_stable_and_distinct(self):
        env = dict(os.environ)
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,%r); import setup; "
             "print(setup.account_route_id('/a/auth.json')); "
             "print(setup.account_route_id('/b/auth.json')); "
             "print(setup.account_route_id('/a/auth.json'))" % ROOT],
            env=env, capture_output=True, text=True)
        a, b, a2 = out.stdout.split()
        self.assertEqual(a, a2)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("oauth-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
