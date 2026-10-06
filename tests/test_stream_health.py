#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the stability pack: error classification, circuit breaker,
content-aware first-byte commit, and the mid-stream stall watchdog.

Ideas ported from model-hotel (TTFT probe, stall watchdog + terminal error
frame, per-provider breaker), codex-proxy and LiteLLM (error-class policy,
quota vs throttle, Retry-After).

    python3 tests/test_stream_health.py
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
sys.path.insert(0, ROOT)
import bridge as bridge_mod                                   # noqa: E402

MODEL = "gpt-6.1-sol"
DELTA = (b"event: response.output_text.delta\n"
         b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n')
DONE = (b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"status":"completed",'
        b'"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n\n')


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class MockRelay:
    """mode: good | bookkeeping_only | stall_mid"""

    def __init__(self, mode):
        self.mode = mode
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": MODEL}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(raw):
                    self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                    self.wfile.flush()

                if outer.mode == "good":
                    send(DELTA)
                    send(DONE)
                elif outer.mode == "bookkeeping_only":
                    # opens the stream, sends only lifecycle events, then hangs
                    send(b'event: response.created\n'
                         b'data: {"type":"response.created","response":{"id":"resp_x"}}\n\n')
                    send(b'event: response.in_progress\n'
                         b'data: {"type":"response.in_progress","response":{"id":"resp_x"}}\n\n')
                    time.sleep(30)
                elif outer.mode == "stall_mid":
                    send(DELTA)
                    time.sleep(30)          # no further bytes, socket stays open
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

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


def start_bridge(routes, attempts=4, extra_env=None):
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="bridge-health-")
    routes = dict(routes, port=port, attempts=attempts)
    routes_path = os.path.join(tmp, "routes.json")
    with open(routes_path, "w") as fh:
        json.dump(routes, fh)
    env = dict(os.environ, BRIDGE_ROUTES=routes_path, BRIDGE_PORT=str(port),
               BRIDGE_STATE=os.path.join(tmp, "state.json"),
               BRIDGE_LOG=os.path.join(tmp, "bridge.log"),
               BRIDGE_REQUEST_LOG=os.path.join(tmp, "requests.jsonl"),
               BRIDGE_ORDER_MODE="fixed", BRIDGE_VERBOSE="0",
               BRIDGE_FIRST_BYTE_TIMEOUT="2", BRIDGE_STREAM_STALL="2",
               BRIDGE_BACKOFF="0.01", BRIDGE_PRICE_TTL="600",
               BRIDGE_MODELS_TTL="600")
    env.update(extra_env or {})
    proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/__bridge/status" % port,
                                   timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("bridge did not start")
    return proc, port


def post(port, mount, timeout=30):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s/responses" % (port, mount),
        data=json.dumps({"model": MODEL, "input": "hi", "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status, fh.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class ClassifyTest(unittest.TestCase):
    def test_quota_is_not_a_throttle(self):
        body = json.dumps({"error": {"type": "usage_limit_reached",
                                     "resets_in_seconds": 7200}}).encode()
        kind, retry_after, reset = bridge_mod.classify_error(429, [], body)
        self.assertEqual(kind, "quota")
        self.assertAlmostEqual(reset, 7200)

    def test_plain_429_is_a_throttle_with_retry_after(self):
        kind, retry_after, _ = bridge_mod.classify_error(
            429, [("Retry-After", "3")], b'{"error":{"message":"slow down"}}')
        self.assertEqual(kind, "rate_limit")
        self.assertAlmostEqual(retry_after, 3)

    def test_reset_field_spellings_are_all_accepted(self):
        for key in ("resets_at", "reset_at", "reset_after_seconds"):
            body = json.dumps({"error": {"message": "quota", "type": key, key: 300}}).encode()
            _kind, _ra, reset = bridge_mod.classify_error(429, [], body)
            self.assertAlmostEqual(reset, 300, msg=key)

    def test_other_classes(self):
        self.assertEqual(bridge_mod.classify_error(401, [], b"")[0], "auth")
        self.assertEqual(bridge_mod.classify_error(403, [], b"")[0], "auth")
        self.assertEqual(bridge_mod.classify_error(404, [], b"")[0], "model_missing")
        self.assertEqual(bridge_mod.classify_error(503, [], b"")[0], "server")
        self.assertEqual(bridge_mod.classify_error(400, [], b"")[0], "bad_request")
        self.assertEqual(bridge_mod.classify_error(402, [], b"")[0], "quota")


class BreakerTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()

    def test_opens_after_threshold_and_doubles_on_failed_probe(self):
        pid = "r1"
        for _ in range(bridge_mod.BREAKER_THRESHOLD - 1):
            bridge_mod.breaker_charge(pid, "server")
        self.assertFalse(bridge_mod.breaker_open(pid))
        bridge_mod.breaker_charge(pid, "server")
        self.assertTrue(bridge_mod.breaker_open(pid))
        first = bridge_mod._stats[pid]["open_until"] - time.time()
        # cooldown passes -> half-open (real traffic is the probe)
        bridge_mod._stats[pid]["open_until"] = time.time() - 1
        self.assertFalse(bridge_mod.breaker_open(pid))
        self.assertEqual(bridge_mod.breaker_state(pid), "half-open")
        bridge_mod.breaker_charge(pid, "server")          # probe failed
        second = bridge_mod._stats[pid]["open_until"] - time.time()
        self.assertGreater(second, first)                 # doubled
        self.assertLessEqual(second, bridge_mod.BREAKER_COOLDOWN_MAX)

    def test_success_closes_it(self):
        pid = "r2"
        for _ in range(bridge_mod.BREAKER_THRESHOLD + 2):
            bridge_mod.breaker_charge(pid, "server")
        bridge_mod.breaker_ok(pid)
        self.assertEqual(bridge_mod.breaker_state(pid), "closed")
        self.assertEqual(bridge_mod._stats[pid]["fails"], 0)

    def test_auth_park_is_long_and_quota_hold_is_extend_only(self):
        pid = "r3"
        bridge_mod.breaker_charge(pid, "auth")
        self.assertGreater(bridge_mod._stats[pid]["open_until"] - time.time(),
                           bridge_mod.BREAKER_COOLDOWN)
        pid2 = "r4"
        bridge_mod.breaker_charge(pid2, "quota", reset=7200)
        far = bridge_mod._stats[pid2]["open_until"]
        bridge_mod.breaker_charge(pid2, "quota", reset=60)   # nearer reset
        self.assertEqual(bridge_mod._stats[pid2]["open_until"], far)

    def test_breaker_demotes_a_relay_in_the_plan(self):
        routes = {p: {"name": p, "upstream": "http://x"} for p in "abc"}
        bridge_mod._prices.clear()
        bridge_mod._last_plan["order"] = []
        for pid in "abc":
            bridge_mod._prices[pid] = {"ts": time.time(),
                                       "per_model": {MODEL: 0.10}, "overall": 0.10,
                                       "trend": 1.0, "error": ""}
        bridge_mod.breaker_charge("a", "auth")
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL)
        self.assertEqual(order[-1], "a")                  # parked, tried last


class FirstContentTest(unittest.TestCase):
    class FakeResp:
        def __init__(self, payload, sock_timeout_after=None):
            self.payload = payload
            self.pos = 0

            class Sock:
                def settimeout(self, *a):
                    pass
            self.fp = type("FP", (), {"raw": type("Raw", (), {"_sock": Sock()})()})()

        def read(self, n):
            if self.pos >= len(self.payload):
                if self.stall_after_first:
                    time.sleep(30)
                return b""
            chunk = self.payload[self.pos:self.pos + n]
            self.pos += len(chunk)
            return chunk

    def _resp(self, payload, stall=False):
        r = FirstContentTest.FakeResp(payload)
        r.stall_after_first = stall
        return r

    def test_bookkeeping_only_is_not_a_commit(self):
        ok, buf, content = bridge_mod.await_first_content(
            self._resp(b'event: response.created\ndata: {"type":"response.created"}\n\n',
                       stall=True), 0.4)
        self.assertFalse(ok)
        self.assertFalse(content)

    def test_content_frame_commits(self):
        ok, buf, content = bridge_mod.await_first_content(self._resp(DELTA), 2)
        self.assertTrue(ok)
        self.assertTrue(content)
        self.assertIn(b"hello", buf)

    def test_terminal_without_content_commits(self):
        ok, buf, content = bridge_mod.await_first_content(self._resp(DONE), 2)
        self.assertTrue(ok)
        self.assertFalse(content)


class StreamHealthIntegrationTest(unittest.TestCase):
    def test_bookkeeping_only_relay_rotates_before_committing(self):
        stall = MockRelay("bookkeeping_only")
        good = MockRelay("good")
        routes = {"order": ["stall", "good"], "routes": {
            "stall": {"mount": "/p/stall", "prefix": "", "name": "stall",
                      "upstream": stall.url, "auth": "sk"},
            "good": {"mount": "/p/good", "prefix": "", "name": "good",
                     "upstream": good.url, "auth": "sk"}}}
        proc, port = start_bridge(routes)
        try:
            status, body = post(port, "/p/stall")
            self.assertEqual(status, 200)
            self.assertIn(b"hello", body)              # good relay's content
            self.assertEqual(stall.hits, 1)
            self.assertEqual(good.hits, 1)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            stall.stop()
            good.stop()

    def test_mid_stream_stall_ends_with_a_failed_frame(self):
        relay = MockRelay("stall_mid")
        routes = {"order": ["only"], "routes": {
            "only": {"mount": "/p/only", "prefix": "", "name": "only",
                     "upstream": relay.url, "auth": "sk"}}}
        proc, port = start_bridge(routes)
        try:
            status, body = post(port, "/p/only")
            self.assertEqual(status, 200)
            self.assertIn(b'"delta":"hello"', body)    # what did arrive, arrived
            self.assertIn(b"response.failed", body)    # and it ends cleanly
            self.assertIn(b"stream_stalled", body)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            relay.stop()

    def test_breaker_shows_up_in_the_status_view(self):
        relay = MockRelay("bookkeeping_only")
        routes = {"order": ["only"], "routes": {
            "only": {"mount": "/p/only", "prefix": "", "name": "only",
                     "upstream": relay.url, "auth": "sk"}}}
        proc, port = start_bridge(routes, attempts=1,
                                  extra_env={"BRIDGE_BREAKER_THRESHOLD": "1"})
        try:
            post(port, "/p/only")
            snap = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/__bridge/status" % port, timeout=5).read())
            row = [r for r in snap["routes"] if r["id"] == "only"][0]
            self.assertEqual(row["breaker"], "open")
            self.assertEqual(row["last_error"], "timeout")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            relay.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
