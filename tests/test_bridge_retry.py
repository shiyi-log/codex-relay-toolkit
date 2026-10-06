#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the retry loop itself (it was refactored when the
attempt order became adaptive).

    python3 tests/test_bridge_retry.py

A local mock relay fails the first N requests with the exact envelope real
relays return, then streams a normal SSE body. The bridge must
  * retry that 400 and hand the client an intact stream,
  * stop after its attempt budget and answer with BRIDGE_EXHAUST_STATUS.
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
UPSTREAM_400 = (b'{"error":{"message":"Upstream request failed",'
                b'"type":"upstream_error"}}')
# a real-shaped stream: first byte early, usage only in the final event
SSE = (b"event: response.output_text.delta\n"
       b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
       b"event: response.completed\n"
       b'data: {"type":"response.completed","response":{"status":"completed",'
       b'"usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},'
       b'"output_tokens":10,"total_tokens":1010}}}\n\n')


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FlakyRelay:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if self.path.endswith("/usage"):
                    # actual_cost 0.5 for 1M input tokens -> $0.5 per weighted M
                    body = json.dumps({
                        "balance": 5, "remaining": 5, "unit": "USD",
                        "model_stats": [{"model": MODEL, "input_tokens": 1_000_000,
                                         "output_tokens": 0, "cache_read_tokens": 0,
                                         "cache_creation_tokens": 0,
                                         "total_tokens": 1_000_000,
                                         "cost": 1.0, "actual_cost": 0.5}]}).encode()
                else:
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
                if outer.hits <= outer.fail_first:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(UPSTREAM_400)))
                    self.end_headers()
                    self.wfile.write(UPSTREAM_400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(SSE)))
                self.end_headers()
                self.wfile.write(SSE)

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


class RetryLoopTest(unittest.TestCase):
    def start_bridge(self, relay, attempts, exhaust_status=None):
        port = free_port()
        tmp = tempfile.mkdtemp(prefix="bridge-retry-")
        routes = {"port": port, "attempts": attempts,
                  "order": ["r1"],
                  "routes": {"r1": {"mount": "/p/r1", "prefix": "",
                                    "name": "flaky", "upstream": relay.url,
                                    "auth": "sk-test"}}}
        routes_path = os.path.join(tmp, "routes.json")
        with open(routes_path, "w") as fh:
            json.dump(routes, fh)
        env = dict(os.environ, BRIDGE_ROUTES=routes_path, BRIDGE_PORT=str(port),
                   BRIDGE_STATE=os.path.join(tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(tmp, "requests.jsonl"),
                   BRIDGE_PRICE_TTL="600", BRIDGE_VERBOSE="0",
                   BRIDGE_ORDER_MODE="fixed", BRIDGE_BACKOFF="0.01")
        if exhaust_status is not None:
            env["BRIDGE_EXHAUST_STATUS"] = str(exhaust_status)
        proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % port, timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            proc.kill()
            self.fail("bridge did not start")
        # wait for the first price refresh so est_cost_usd can be computed
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                snap = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % port, timeout=2).read())
                if any(r["price_per_m"] for r in snap["routes"]):
                    break
            except Exception:
                pass
            time.sleep(0.1)
        return proc, port, tmp

    def post(self, port):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/p/r1/responses" % port,
            data=json.dumps({"model": MODEL, "input": "hi"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as fh:
            return fh.status, fh.read()

    def test_400_upstream_error_is_retried_and_stream_kept(self):
        relay = FlakyRelay(fail_first=2)
        proc, port, tmp = self.start_bridge(relay, attempts=5)
        try:
            status, body = self.post(port)
            self.assertEqual(status, 200)
            self.assertIn(b"response.completed", body)   # intact stream, not truncated
            self.assertEqual(relay.hits, 3)

            # the structured request log is the only per-request record of which
            # relay really served it (CC Switch can only see the mount)
            with open(os.path.join(tmp, "requests.jsonl")) as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            self.assertEqual([l["result"] for l in lines],
                             ["retry", "retry", "ok"])
            self.assertEqual(lines[-1]["relay"], "flaky")
            self.assertEqual(lines[-1]["mount"], "flaky")
            self.assertEqual(lines[-1]["model"], MODEL)
            self.assertEqual(lines[-1]["attempt"], 3)
            self.assertIsNotNone(lines[-1]["first_byte_ms"])
            # tokens come from the relay's own usage block, cost from that
            # relay's measured price (0.5 for 1M input -> $0.5/weighted-M)
            self.assertEqual(lines[-1]["tokens"], {"input": 200, "output": 10,
                                                   "cache_read": 800,
                                                   "cache_creation": 0, "total": 1010})
            self.assertAlmostEqual(lines[-1]["price_per_m"], 0.5, places=4)
            self.assertAlmostEqual(lines[-1]["est_cost_usd"], 320e-6 * 0.5, places=8)
            self.assertTrue(lines[-1]["stream_complete"])
            # retry lines carry no usage
            self.assertNotIn("tokens", lines[0])
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            relay.stop()

    def test_budget_is_bounded_and_exhaust_status_is_returned(self):
        relay = FlakyRelay(fail_first=10_000)
        proc, port, tmp = self.start_bridge(relay, attempts=3, exhaust_status=400)
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post(port)
            self.assertEqual(ctx.exception.code, 400)
            payload = json.loads(ctx.exception.read())
            self.assertIn("error", payload)
            self.assertEqual(relay.hits, 3)              # exactly the budget
            with open(os.path.join(tmp, "requests.jsonl")) as fh:
                self.assertEqual(len([l for l in fh if l.strip()]), 3)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            relay.stop()


class UsageScannerTest(unittest.TestCase):
    """The usage block only arrives at the end of a stream, in pieces."""

    def test_truncated_usage_is_never_used(self):
        s = bridge_mod.UsageScanner()
        s.feed(b'{"type":"response.completed","response":{"usage":{"input_tokens":43')
        self.assertIsNone(s.tokens())
        s.feed(b'88,"output_tokens":5')
        self.assertIsNone(s.tokens())
        s.feed(b',"input_tokens_details":{"cached_tokens":3840},'
               b'"total_tokens":4393}}}\n\n')
        self.assertEqual(s.tokens(), {"input": 548, "output": 5, "cache_read": 3840,
                                      "cache_creation": 0, "total": 4393})

    def test_marker_split_across_chunks(self):
        s = bridge_mod.UsageScanner()
        s.feed(b'...{"usa')
        s.feed(b'ge":{"input_tokens":100,"output_tokens":1,'
               b'"input_tokens_details":{"cached_tokens":90}}}')
        self.assertEqual(s.tokens()["cache_read"], 90)
        self.assertEqual(s.tokens()["input"], 10)

    def test_empty_usage_then_the_real_one(self):
        s = bridge_mod.UsageScanner()
        s.feed(b'{"type":"response.in_progress","response":{"usage":{}}}')
        self.assertIsNone(s.tokens())
        s.feed(b'{"type":"response.completed","response":{"usage":'
               b'{"input_tokens":50,"output_tokens":2,"total_tokens":52}}}')
        self.assertEqual(s.tokens()["output"], 2)

    def test_non_stream_usage_is_normalised_the_same_way(self):
        tok = bridge_mod.normalise_usage(
            {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80},
             "output_tokens": 7, "total_tokens": 107})
        self.assertEqual(tok, {"input": 20, "output": 7, "cache_read": 80,
                               "cache_creation": 0, "total": 107})
        self.assertIsNone(bridge_mod.normalise_usage(None))
        self.assertIsNone(bridge_mod.normalise_usage({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
