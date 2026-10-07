#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the adaptive attempt order (price + measured speed).

Run:

    python3 tests/test_adaptive_order.py
    python3 -m unittest discover -s tests

Two halves:

  * unit tests on the scoring functions (no network, no server);
  * an end-to-end test that starts bridge.py against two local mock relays -
    one cheap, one expensive - and checks that a request entering through the
    *expensive* relay's mount is actually served by the cheap one, because the
    order is adaptive and not the fixed routes.json order.
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


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class MockRelay:
    """A relay that bills `price_per_m` and answers /models, /usage, /responses."""

    def __init__(self, name, price_per_m, delay=0.0, models=(MODEL,), fail=False):
        self.name = name
        self.price_per_m = price_per_m
        self.delay = delay
        self.models = list(models)
        self.fail = fail
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self._json(200, {"data": [{"id": m} for m in outer.models]})
                if self.path.endswith("/usage"):
                    outer.usage_hits += 1
                    return self._json(200, outer.usage_body())
                return self._json(404, {"error": {"message": "no such path"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits += 1
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.fail:
                    return self._json(500, {"error": {"message": "boom",
                                                      "type": "upstream_error"}})
                return self._json(200, {"served_by": outer.name, "status": "completed"})

            def log_message(self, *a):
                pass

        self.usage_hits = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def usage_body(self):
        # input tokens only, so actual_cost == $/M tokens directly
        return {"balance": 5, "remaining": 5, "unit": "USD", "planName": "钱包余额",
                "model_stats": [{
                    "model": MODEL, "input_tokens": 1_000_000, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "total_tokens": 1_000_000, "cost": self.price_per_m * 10,
                    "actual_cost": self.price_per_m, "account_cost": self.price_per_m}]}

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ScoringTest(unittest.TestCase):
    """Unit level: the order must follow price, speed, failures and model fit."""

    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._models_cache.clear()
        bridge_mod._last_plan["order"] = []
        bridge_mod._last_plan["signature"] = ""
        self.routes = {"a": {"name": "A", "upstream": "http://a"},
                       "b": {"name": "B", "upstream": "http://b"}}

    def set_price(self, pid, price):
        bridge_mod._prices[pid] = {"ts": time.time(),
                                   "per_model": {MODEL: price},
                                   "overall": price, "error": ""}

    def set_stats(self, pid, lat, ok, fail):
        bridge_mod._stats[pid] = {"lat": lat, "ok": ok, "fail": fail, "samples": 1,
                                  "lat_ts": time.time(), "attempt_ts": time.time(),
                                  "byhour": {}}

    def test_cheapest_and_fastest_wins(self):
        self.set_price("a", 0.10)
        self.set_price("b", 2.00)
        self.set_stats("a", 0.05, 1.0, 0.0)
        self.set_stats("b", 0.50, 1.0, 0.0)
        # routes.json says B first; adaptive must still pick A
        order, scores = bridge_mod.plan_order(self.routes, ["b", "a"], "b", MODEL)
        self.assertEqual(order[0], "a")
        self.assertLess(scores["a"]["score"], scores["b"]["score"])

    def test_fixed_mode_keeps_the_old_start(self):
        self.set_price("a", 0.10)
        self.set_price("b", 2.00)
        old = bridge_mod.ORDER_MODE
        bridge_mod.ORDER_MODE = "fixed"
        try:
            order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "b", MODEL)
        finally:
            bridge_mod.ORDER_MODE = old
        self.assertEqual(order[0], "b")

    def test_failures_and_slowness_demote(self):
        self.set_price("a", 1.0)
        self.set_price("b", 1.2)
        self.set_stats("a", 3.0, 0.0, 1.0)     # stalling
        self.set_stats("b", 0.2, 1.0, 0.0)     # fast and healthy
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL)
        self.assertEqual(order[0], "b")

    def test_unknown_relay_sits_midpack(self):
        routes = dict(self.routes, c={"name": "C", "upstream": "http://c"})
        self.set_price("a", 0.10)
        self.set_price("b", 1.00)
        order, scores = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL)
        # "a" is the known cheapest; a relay with no price data is assumed to be
        # at the median (here the more expensive one), never silently first
        self.assertEqual(order[0], "a")
        self.assertAlmostEqual(scores["a"]["price_norm"], 1.0)
        self.assertAlmostEqual(scores["c"]["price_norm"], scores["b"]["price_norm"])

    def test_model_penalty_prefers_the_relay_that_serves_it(self):
        self.set_price("a", 0.10)
        self.set_price("b", 0.10)
        bridge_mod._models_cache["a"] = {"ids": {"gpt-6-sol"}, "ts": time.time()}
        bridge_mod._models_cache["b"] = {"ids": {MODEL}, "ts": time.time()}
        order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL)
        self.assertEqual(scores["a"]["model_penalty"], bridge_mod.MODEL_ADAPT_PENALTY)
        self.assertEqual(scores["b"]["model_penalty"], 0.0)
        self.assertEqual(order[0], "b")

    def test_subscription_account_is_always_last(self):
        routes = dict(self.routes)
        routes["o"] = {"name": "account", "upstream": "http://o",
                       "auth_type": "oauth"}
        self.set_price("a", 5.0)
        self.set_price("b", 0.1)
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "o"], "a", MODEL)
        self.assertEqual(order[-1], "o")

    def test_state_survives_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(prefix="bridge-state-"), "bridge-state.json")
        old = bridge_mod.STATE_FILE
        bridge_mod.STATE_FILE = path
        try:
            self.set_price("a", 0.10)
            self.set_stats("a", 0.05, 1.0, 0.0)
            bridge_mod.save_state()
            bridge_mod._stats.clear()
            bridge_mod._prices.clear()
            bridge_mod.load_state()
        finally:
            bridge_mod.STATE_FILE = old
        self.assertAlmostEqual(bridge_mod._prices["a"]["per_model"][MODEL], 0.10)
        self.assertAlmostEqual(bridge_mod._stats["a"]["lat"], 0.05)


class VariableInputsTest(unittest.TestCase):
    """Price and speed are variables - the scoring must follow the *recent*
    numbers and stop trusting stale ones."""

    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._models_cache.clear()
        bridge_mod._last_plan["order"] = []
        self.routes = {"a": {"name": "A", "upstream": "http://a"},
                       "b": {"name": "B", "upstream": "http://b"}}

    def test_price_uses_weighted_tokens(self):
        data = {"model_stats": [{"model": MODEL, "input_tokens": 0,
                                 "output_tokens": 1_000_000, "cache_read_tokens": 0,
                                 "cache_creation_tokens": 0, "actual_cost": 8.0}]}
        parsed = bridge_mod.parse_usage(data)
        # 1M output tokens = 4 weighted million -> $2 / weighted M
        self.assertAlmostEqual(parsed["per_model"][MODEL], 2.0)

    def test_price_trend_follows_the_recent_days(self):
        data = {
            "model_stats": [{"model": MODEL, "input_tokens": 1_000_000,
                             "output_tokens": 0, "cache_read_tokens": 0,
                             "cache_creation_tokens": 0, "actual_cost": 1.0}],
            "daily_usage": [
                {"date": "2026-10-01", "input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0, "actual_cost": 1.0},
                {"date": "2026-10-06", "input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0, "actual_cost": 3.0}]}
        parsed = bridge_mod.parse_usage(data, days=1)
        # all-time $1/M, yesterday $3/M -> trend 3, reported so the status page
        # can say "this relay got pricier".
        self.assertAlmostEqual(parsed["trend"], 3.0)
        self.assertAlmostEqual(parsed["recent"], 3.0)
        # The price itself stays the all-time per-model rate: multiplying by the
        # trend double-counted the mix and understated pp/wdlink by ~4x against
        # real balance movement (see tests/test_scoring_v2.py).
        self.assertAlmostEqual(parsed["per_model"][MODEL], 1.0)
        self.assertAlmostEqual(parsed["overall"], 1.0)

    def test_hourly_latency_is_used_when_it_has_samples(self):
        now = time.time()
        hour = str(time.localtime(now).tm_hour)
        bridge_mod._stats["a"] = {"lat": 1.0, "ok": 1.0, "fail": 0.0, "samples": 10,
                                  "lat_ts": now,
                                  "byhour": {hour: {"lat": 0.2, "samples": 3}}}
        self.assertAlmostEqual(bridge_mod.effective_latency("a"), 0.2)
        # ... but only once this hour has enough of its own samples
        bridge_mod._stats["a"]["byhour"][hour]["samples"] = 1
        self.assertAlmostEqual(bridge_mod.effective_latency("a"), 1.0)

    def test_stale_latency_is_not_trusted(self):
        bridge_mod._stats["a"] = {"lat": 0.5, "ok": 1.0, "fail": 0.0, "samples": 5,
                                  "lat_ts": time.time() - bridge_mod.LATENCY_MAX_AGE - 60,
                                  "byhour": {}}
        self.assertIsNone(bridge_mod.effective_latency("a"))

    def test_stale_price_is_not_trusted(self):
        bridge_mod._prices["a"] = {"ts": time.time() - bridge_mod.PRICE_MAX_AGE - 60,
                                   "per_model": {MODEL: 0.01}, "overall": 0.01,
                                   "trend": 1.0, "error": ""}
        self.assertIsNone(bridge_mod.price_index("a", MODEL)[0])

    def test_exploration_promotes_the_stalest_relay(self):
        routes = dict(self.routes, c={"name": "C", "upstream": "http://c"})
        for pid, price in (("a", 0.10), ("b", 1.0), ("c", 0.15)):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {MODEL: price},
                                       "overall": price, "trend": 1.0, "error": ""}
        for pid in ("a", "b"):
            bridge_mod._stats[pid] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                      "lat_ts": time.time(), "attempt_ts": time.time(),
                                      "byhour": {}}
        bridge_mod._stats["c"] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time() - 4000,
                                  "attempt_ts": time.time() - 4000, "byhour": {}}
        # normal requests exploit the cheap relay...
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL)
        self.assertEqual(order[0], "a")
        # ...and the occasional exploration re-measures the stale one
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL,
                                         explore=True)
        self.assertEqual(order[0], "c")

    def test_exploration_skips_relays_that_cannot_win_on_price(self):
        routes = dict(self.routes, c={"name": "C", "upstream": "http://c"})
        for pid, price in (("a", 0.10), ("b", 1.0), ("c", 9.0)):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {MODEL: price},
                                       "overall": price, "trend": 1.0, "error": ""}
        for pid in ("a", "b"):
            bridge_mod._stats[pid] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                      "lat_ts": time.time(), "attempt_ts": time.time(),
                                      "byhour": {}}
        bridge_mod._stats["c"] = {"lat": None, "ok": 0.0, "fail": 0.0, "samples": 0,
                                  "lat_ts": 0.0, "attempt_ts": 0.0, "byhour": {}}
        # c is 90x the cheapest: no speed result could ever make it win, so the
        # exploration budget is not spent on it
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL,
                                         explore=True)
        self.assertEqual(order[0], "a")

    def test_exploration_uses_last_attempt_not_last_success(self):
        routes = dict(self.routes, c={"name": "C", "upstream": "http://c"})
        for pid in ("a", "b", "c"):
            bridge_mod._prices[pid] = {"ts": time.time(),
                                       "per_model": {MODEL: 0.10}, "overall": 0.10,
                                       "trend": 1.0, "error": ""}
        bridge_mod._stats["a"] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time(), "attempt_ts": time.time(),
                                  "byhour": {}}
        # b always fails (never a latency sample) but was tried a moment ago
        bridge_mod._stats["b"] = {"lat": None, "ok": 0.0, "fail": 1.0, "samples": 0,
                                  "lat_ts": 0.0, "attempt_ts": time.time(), "byhour": {}}
        bridge_mod._stats["c"] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time() - 4000,
                                  "attempt_ts": time.time() - 4000, "byhour": {}}
        order, _ = bridge_mod.plan_order(routes, ["a", "b", "c"], "a", MODEL,
                                         explore=True)
        self.assertEqual(order[0], "c")     # not the relay that keeps failing

    def test_never_measured_cheap_relay_gets_warmed_up_once(self):
        for pid, price in (("a", 0.10), ("b", 0.15)):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {MODEL: price},
                                       "overall": price, "trend": 1.0, "error": ""}
        bridge_mod._stats["a"] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time(), "byhour": {}}
        # b is 1.5x the cheapest but has never been tried: measure it once,
        # otherwise "faster" can never be discovered
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL)
        self.assertEqual(order[0], "b")
        # once measured, exploitation goes back to the cheapest
        bridge_mod._stats["b"] = {"lat": 0.5, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time(), "byhour": {}}
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL)
        self.assertEqual(order[0], "a")

    def test_expensive_unmeasured_relay_is_not_warmed_up(self):
        for pid, price in (("a", 0.10), ("b", 5.0)):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {MODEL: price},
                                       "overall": price, "trend": 1.0, "error": ""}
        bridge_mod._stats["a"] = {"lat": 0.1, "ok": 1.0, "fail": 0.0, "samples": 1,
                                  "lat_ts": time.time(), "byhour": {}}
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], "a", MODEL)
        self.assertEqual(order[0], "a")


class AdaptiveRoutingTest(unittest.TestCase):
    """End to end: cheap relay must win even when the request enters at the
    expensive relay's mount."""

    @classmethod
    def setUpClass(cls):
        cls.expensive = MockRelay("expensive", price_per_m=2.00, delay=0.2)
        cls.cheap = MockRelay("cheap", price_per_m=0.10)
        cls.port = free_port()
        cls.tmp = tempfile.mkdtemp(prefix="bridge-adaptive-")
        cls.routes_path = os.path.join(cls.tmp, "routes.json")
        cls.log_path = os.path.join(cls.tmp, "bridge.log")
        routes = {
            "port": cls.port, "attempts": 4,
            "order": ["exp", "cheap"],
            "routes": {
                "exp": {"mount": "/p/exp", "prefix": "", "name": "expensive",
                        "upstream": cls.expensive.url, "auth": "sk-exp"},
                "cheap": {"mount": "/p/cheap", "prefix": "", "name": "cheap",
                          "upstream": cls.cheap.url, "auth": "sk-cheap"},
            }}
        with open(cls.routes_path, "w") as fh:
            json.dump(routes, fh)

        env = dict(os.environ,
                   BRIDGE_ROUTES=cls.routes_path,
                   BRIDGE_PORT=str(cls.port),
                   BRIDGE_STATE=os.path.join(cls.tmp, "bridge-state.json"),
                   BRIDGE_LOG=cls.log_path,
                   BRIDGE_PRICE_TTL="2",
                   BRIDGE_HOUSEKEEPING="1",
                   BRIDGE_VERBOSE="0")
        cls.proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        cls._wait_for_prices()

    @classmethod
    def _status(cls):
        url = "http://127.0.0.1:%d/__bridge/status?model=%s" % (cls.port, MODEL)
        with urllib.request.urlopen(url, timeout=5) as fh:
            return json.load(fh)

    @classmethod
    def _wait_for_prices(cls):
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                snap = cls._status()
                if all(r["price_per_m"] for r in snap["routes"]):
                    cls.snapshot = snap
                    return
            except Exception:
                pass
            time.sleep(0.2)
        try:
            with open(cls.log_path) as fh:
                tail = fh.read()[-2000:]
        except Exception:
            tail = "(no log)"
        raise AssertionError("bridge never reported prices:\n%s" % tail)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.expensive.stop()
        cls.cheap.stop()

    def post(self, mount):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s/responses" % (self.port, mount),
            data=json.dumps({"model": MODEL, "input": "hi"}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer sk-local"})
        with urllib.request.urlopen(req, timeout=15) as fh:
            return json.load(fh)

    def test_ranking_puts_the_cheap_relay_first(self):
        self.assertEqual(self.snapshot["order"][0], "cheap")
        self.assertEqual(self.snapshot["mode"], "adaptive")
        prices = {r["name"]: r["price_per_m"] for r in self.snapshot["routes"]}
        self.assertAlmostEqual(prices["cheap"], 0.10)
        self.assertAlmostEqual(prices["expensive"], 2.00)

    def test_request_entering_at_the_expensive_mount_is_served_cheap(self):
        body = self.post("/p/exp")
        self.assertEqual(body["served_by"], "cheap")
        self.assertEqual(self.expensive.hits, 0)

    def test_status_text_view_renders(self):
        url = "http://127.0.0.1:%d/__bridge/status?text=1" % self.port
        with urllib.request.urlopen(url, timeout=5) as fh:
            text = fh.read().decode()
        self.assertIn("cheap", text)
        self.assertIn("$/加权M", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
