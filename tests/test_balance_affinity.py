#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for balance-aware parking and conversation affinity (session stickiness).

Balance: the /v1/usage refresh already reports the remaining credit, so a relay
that is about to run dry can be parked before it burns an attempt.

Affinity: a conversation that hops relays loses the upstream prompt cache (and,
on stateful relays, its context), so the relay that served the last turn gets a
bonus - not a hard pin, a clearly better relay still wins.

    python3 tests/test_balance_affinity.py
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(ROOT, "bridge.py")
sys.path.insert(0, ROOT)
import bridge as bridge_mod                                   # noqa: E402

MODEL = "gpt-6.1-sol"
SSE = (b"event: response.output_text.delta\n"
       b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
       b"event: response.completed\n"
       b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class BalanceTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()

    def test_reads_remaining_or_balance(self):
        self.assertEqual(bridge_mod.read_balance({"remaining": 12.5}), 12.5)
        self.assertEqual(bridge_mod.read_balance({"balance": 3}), 3.0)

    def test_prepaid_relays_reporting_absurd_values_are_unlimited(self):
        self.assertIsNone(bridge_mod.read_balance({"balance": 11111108283.66}))
        self.assertIsNone(bridge_mod.read_balance({"balance": "12"}))
        self.assertIsNone(bridge_mod.read_balance({}))
        self.assertIsNone(bridge_mod.read_balance(None))

    def test_low_balance_parks_and_a_top_up_releases(self):
        bridge_mod.note_balance("r1", 0.40)                 # below $1
        self.assertTrue(bridge_mod.breaker_open("r1"))
        self.assertEqual(bridge_mod._stats["r1"]["last_error"], "balance")
        bridge_mod.note_balance("r1", 25.0)                 # topped up
        self.assertFalse(bridge_mod.breaker_open("r1"))
        self.assertEqual(bridge_mod._stats["r1"]["last_error"], "")

    def test_a_success_does_not_lift_a_balance_park(self):
        bridge_mod.note_balance("r2", 0.10)
        bridge_mod.breaker_ok("r2")                          # a request succeeded
        self.assertTrue(bridge_mod.breaker_open("r2"))

    def test_healthy_balance_never_parks(self):
        bridge_mod.note_balance("r3", 50.0)
        self.assertFalse(bridge_mod.breaker_open("r3"))
        bridge_mod.note_balance("r3", None)                  # unlimited / unknown
        self.assertFalse(bridge_mod.breaker_open("r3"))


class AffinityTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._affinity.clear()
        bridge_mod._last_plan["order"] = []
        self.routes = {p: {"name": p.upper(), "upstream": "http://" + p} for p in "ab"}

    def price(self, pid, value):
        bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {MODEL: value},
                                   "overall": value, "trend": 1.0, "error": ""}

    # ---- the key ---------------------------------------------------------
    def test_explicit_headers_win(self):
        key = bridge_mod.session_key({"session-id": "sess-1"}, b"{}", {})
        self.assertEqual(key, "h:sess-1")
        key = bridge_mod.session_key({"thread-id": "t-9"}, b"{}", {})
        self.assertEqual(key, "h:t-9")

    def test_prompt_cache_key_is_used_next(self):
        key = bridge_mod.session_key({}, b"{}", {"prompt_cache_key": "ck-7"})
        self.assertEqual(key, "c:ck-7")

    def test_fallback_is_stable_across_turns(self):
        turn1 = {"instructions": "be nice", "input": [{"role": "user", "content": "hi"}]}
        turn2 = {"instructions": "be nice",
                 "input": [{"role": "user", "content": "hi"},
                           {"role": "assistant", "content": "hello"},
                           {"role": "user", "content": "again"}]}
        k1 = bridge_mod.session_key({}, None, turn1)
        k2 = bridge_mod.session_key({}, None, turn2)
        self.assertTrue(k1.startswith("p:"))
        self.assertEqual(k1, k2)                    # first input item is stable

    def test_empty_body_has_no_key(self):
        self.assertIsNone(bridge_mod.session_key({}, b"", None))

    # ---- remembering -----------------------------------------------------
    def test_remember_and_expire(self):
        bridge_mod.affinity_remember("k", "a", now=1000.0)
        self.assertEqual(bridge_mod.affinity_get("k", now=1000.0 + 10), "a")
        self.assertIsNone(bridge_mod.affinity_get("k", now=1000.0
                                                  + bridge_mod.AFFINITY_TTL + 1))

    def test_table_is_capped(self):
        old = bridge_mod.AFFINITY_MAX
        bridge_mod.AFFINITY_MAX = 3
        try:
            for i in range(6):
                bridge_mod.affinity_remember("k%d" % i, "a", now=1000.0 + i)
            self.assertLessEqual(len(bridge_mod._affinity), 3)
        finally:
            bridge_mod.AFFINITY_MAX = old

    # ---- scoring ---------------------------------------------------------
    def test_sticky_relay_survives_a_small_price_difference(self):
        self.price("a", 1.00)
        self.price("b", 1.05)
        order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL,
                                              affinity_pid="b")
        self.assertEqual(order[0], "b")
        self.assertTrue(scores["b"]["affinity"])

    def test_a_much_cheaper_relay_still_wins(self):
        self.price("a", 5.00)
        self.price("b", 1.00)
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL,
                                         affinity_pid="a")
        self.assertEqual(order[0], "b")

    def test_parked_sticky_relay_is_ignored(self):
        self.price("a", 1.00)
        self.price("b", 1.05)
        bridge_mod.breaker_charge("b", "auth")
        order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL,
                                              affinity_pid="b")
        self.assertEqual(order[0], "a")
        self.assertFalse(scores["b"]["affinity"])

    def test_affinity_never_applies_to_the_subscription_account(self):
        routes = dict(self.routes, o={"name": "acct", "upstream": "http://o",
                                      "auth_type": "oauth"})
        self.price("a", 1.0)
        self.price("b", 2.0)
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "o"], "a", MODEL,
                                         affinity_pid="o")
        self.assertEqual(order[-1], "o")
        self.assertEqual(order[0], "a")


class LiveAffinityTest(unittest.TestCase):
    """One conversation must keep being served by the same relay."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="affinity-")
        self.relays = {}
        for name, price in (("cheap", 0.10), ("close", 0.105)):
            self.relays[name] = self._relay(name, price)
        self.port = free_port()
        routes = {"port": self.port, "attempts": 3, "order": ["cheap", "close"],
                  "routes": {}}
        for name, relay in self.relays.items():
            routes["routes"][name] = {"mount": "/p/" + name, "prefix": "",
                                      "name": name, "upstream": relay["url"],
                                      "auth": "sk-" + name}
        self.routes_path = os.path.join(self.tmp, "routes.json")
        with open(self.routes_path, "w") as fh:
            json.dump(routes, fh)
        env = dict(os.environ, BRIDGE_ROUTES=self.routes_path,
                   BRIDGE_PORT=str(self.port),
                   BRIDGE_STATE=os.path.join(self.tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(self.tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(self.tmp, "requests.jsonl"),
                   BRIDGE_VERBOSE="0", BRIDGE_FIRST_BYTE_TIMEOUT="5",
                   BRIDGE_BACKOFF="0.01", BRIDGE_STATE_TTL="86400")
        self.proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                snap = self.status()
                if all(r["price_per_m"] for r in snap["routes"]):
                    break
            except Exception:
                pass
            time.sleep(0.2)

    def _relay(self, name, price):
        port = free_port()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if self.path.endswith("/usage"):
                    body = json.dumps({
                        "balance": 50, "remaining": 50, "unit": "USD",
                        "model_stats": [{"model": MODEL, "input_tokens": 1_000_000,
                                         "output_tokens": 0, "cache_read_tokens": 0,
                                         "cache_creation_tokens": 0,
                                         "total_tokens": 1_000_000,
                                         "actual_cost": price}]}).encode()
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
                outer["hits"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(SSE)))
                self.end_headers()
                self.wfile.write(SSE)

            def log_message(self, *a):
                pass

        outer = {"hits": 0, "url": "http://127.0.0.1:%d" % port,
                 "server": ThreadingHTTPServer(("127.0.0.1", port), H)}
        threading.Thread(target=outer["server"].serve_forever, daemon=True).start()
        return outer

    def status(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/__bridge/status?model=%s" % (self.port, MODEL),
                timeout=5) as fh:
            return json.load(fh)

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for relay in self.relays.values():
            relay["server"].shutdown()
            relay["server"].server_close()

    def post(self, conversation):
        body = json.dumps({"model": MODEL, "stream": True,
                           "instructions": "be nice",
                           "input": [{"role": "user", "content": conversation}]})
        req = urllib.request.Request(
            "http://127.0.0.1:%d/p/close/responses" % self.port,
            data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            fh.read()

    def test_a_conversation_sticks_to_the_relay_that_served_it(self):
        self.post("first turn")
        first = self.relays["cheap"]["hits"]
        self.assertEqual(first, 1)                  # cheapest relay served it
        self.post("first turn")                     # same conversation
        self.assertEqual(self.relays["cheap"]["hits"], 2)
        self.assertEqual(self.relays["close"]["hits"], 0)
        rows = {r["id"]: r for r in self.status()["routes"]}
        self.assertTrue(rows["cheap"]["affinity"])   # and the status says so
        self.assertEqual(rows["cheap"]["affinity_sessions"], 1)
        self.assertFalse(rows["close"]["affinity"])
        self.assertEqual(rows["cheap"]["balance"], 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
