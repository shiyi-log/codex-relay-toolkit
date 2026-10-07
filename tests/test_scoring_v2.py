#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scoring v2: honest prices, per-model latency, speed weighting, live reload.

Three things measured against reality on 2026-10-07 (see docs/ROADMAP.md):

* the `trend` factor was multiplied into the unit price, which understated the
  pp/wdlink accounts by ~4x (their recent mix is cache-heavy, which lowers the
  recent $/weighted-token without the price having moved). Real balance movement
  matched the *all-time* per-model rate within ~20%.
* latency was averaged across all models, so the mix of models flattened every
  relay to "about the same speed" and price alone decided. The real differences
  are per model, and they are only a few seconds.
* SIGHUP now re-execs the bridge while keeping the listening socket open, so a
  deploy is no longer an event CC Switch can turn into a handoff to the
  official account.

    python3 tests/test_scoring_v2.py
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

MODEL_A = "gpt-6.1-sol"
MODEL_B = "codex-auto-review"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def usage_body(price, models=(MODEL_A,), recent_price=None):
    stats = [{"model": m, "input_tokens": 1_000_000, "output_tokens": 0,
              "cache_read_tokens": 0, "cache_creation_tokens": 0,
              "total_tokens": 1_000_000, "actual_cost": price} for m in models]
    body = {"balance": 100, "model_stats": stats}
    if recent_price is not None:
        body["daily_usage"] = [
            {"date": "2026-10-06", "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0,
             "actual_cost": recent_price}]
    return body


class PriceBasisTest(unittest.TestCase):
    def test_trend_is_reported_but_not_multiplied_into_the_price(self):
        parsed = bridge_mod.parse_usage(usage_body(0.066, recent_price=0.021))
        self.assertAlmostEqual(parsed["per_model"][MODEL_A], 0.066, places=6)
        self.assertAlmostEqual(parsed["overall"], 0.066, places=6)
        self.assertLess(parsed["trend"], 0.5)                 # x0.32-ish
        self.assertAlmostEqual(parsed["recent"], 0.021, places=6)

    def test_price_index_returns_the_unscaled_rate(self):
        bridge_mod._prices.clear()
        bridge_mod._prices["p"] = dict(bridge_mod.parse_usage(
            usage_body(0.066, recent_price=0.021)), ts=time.time(), error="")
        price, exact = bridge_mod.price_index("p", MODEL_A)
        self.assertTrue(exact)
        self.assertAlmostEqual(price, 0.066, places=6)


class PerModelLatencyTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()

    def feed(self, pid, model, seconds, times=None):
        for _ in range(times or bridge_mod.MODEL_MIN_SAMPLES):
            bridge_mod.record_attempt(pid, seconds, model=model)

    def test_per_model_figure_wins_once_it_has_enough_samples(self):
        self.feed("p", MODEL_A, 9.0)
        self.feed("p", MODEL_B, 2.0)
        self.assertAlmostEqual(bridge_mod.effective_latency("p", MODEL_A), 9.0, places=6)
        self.assertAlmostEqual(bridge_mod.effective_latency("p", MODEL_B), 2.0, places=6)

    def test_thin_per_model_data_falls_back_to_the_overall_figure(self):
        self.feed("p", MODEL_A, 9.0)                     # enough for MODEL_A
        bridge_mod.record_attempt("p", 4.0, model=MODEL_B)   # a single sample
        want = bridge_mod.lat_stat(bridge_mod._stats["p"]["lat_ring"],
                                   bridge_mod._stats["p"]["lat"])
        got = bridge_mod.effective_latency("p", MODEL_B)
        self.assertAlmostEqual(got, want, places=6)      # the overall figure...
        self.assertNotAlmostEqual(got, 4.0, places=6)    # ...not the thin sample
        self.assertIsNotNone(bridge_mod.effective_latency("p"))

    def test_stale_per_model_figure_is_dropped(self):
        self.feed("p", MODEL_A, 9.0)
        bridge_mod._stats["p"]["lat_by_model"][MODEL_A]["ts"] = (
            time.time() - bridge_mod.LATENCY_MAX_AGE - 1)
        for _ in range(5):
            bridge_mod.record_attempt("p", 5.0)          # overall moves on
        want = bridge_mod.lat_stat(bridge_mod._stats["p"]["lat_ring"],
                                   bridge_mod._stats["p"]["lat"])
        got = bridge_mod.effective_latency("p", MODEL_A)
        self.assertAlmostEqual(got, want, places=6)      # fell back to overall
        self.assertNotAlmostEqual(got, 9.0, places=6)    # the stale figure is gone

    def test_state_is_bounded(self):
        for i in range(60):
            bridge_mod.record_attempt("p", 1.0, model="model-%d" % i)
        self.assertLessEqual(len(bridge_mod._stats["p"]["lat_by_model"]), 40)

    def test_speed_now_changes_the_order(self):
        """Two relays, same price: the one that is faster *for this model* wins,
        and the same pair flips for the other model."""
        routes = {p: {"name": p.upper()} for p in ("slow", "fast")}
        bridge_mod._prices.clear()
        for pid in ("slow", "fast"):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}
        self.feed("slow", MODEL_A, 9.0)
        self.feed("fast", MODEL_A, 4.0)
        self.feed("slow", MODEL_B, 4.0, times=bridge_mod.MODEL_MIN_SAMPLES + 3)
        self.feed("fast", MODEL_B, 9.0)
        order_a, scores_a = bridge_mod.plan_order(routes, ["slow", "fast"], None, MODEL_A)
        order_b, _ = bridge_mod.plan_order(routes, ["slow", "fast"], None, MODEL_B)
        self.assertEqual(order_a[0], "fast")
        self.assertEqual(order_b[0], "slow")            # per-model, not global

    def test_speed_weight_is_configurable_and_defaults_higher(self):
        self.assertGreaterEqual(bridge_mod.W_LATENCY, 1.5)

    def test_affinity_no_longer_suppresses_exploration(self):
        """A sticky conversation may still be interrupted by the 1-in-N
        exploration, otherwise a long session freezes the pool."""
        routes = {p: {"name": p.upper(), "upstream": "http://" + p}
                  for p in ("a", "b")}
        bridge_mod._prices.clear()
        for pid in ("a", "b"):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}
        bridge_mod._stats.clear()
        bridge_mod.record_attempt("a", 1.0, model=MODEL_A)
        # b was never tried: pick_warmup would promote it, but affinity blocks that
        order, _ = bridge_mod.plan_order(routes, ["a", "b"], None, MODEL_A,
                                         affinity_pid="a", explore=False)
        self.assertEqual(order[0], "a")
        # exploration is allowed through even with a sticky conversation
        order, _ = bridge_mod.plan_order(routes, ["a", "b"], None, MODEL_A,
                                         affinity_pid="a", explore=True)
        self.assertEqual(order[0], "b")


class FailStreakTest(unittest.TestCase):
    """A relay that just failed is demoted immediately, not only through the slow
    failure-rate EWMA and not only once the breaker opens at 5 consecutive
    failures. The penalty expires, so one bad minute cannot outlive its cause."""

    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._last_plan["order"] = []          # no hysteresis from the last test
        bridge_mod._last_plan["signature"] = ""
        self.routes = {p: {"name": p.upper()} for p in ("a", "b")}
        for pid in ("a", "b"):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}

    def score(self, pid, model=MODEL_A):
        _order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], None, model)
        return scores[pid]["score"]

    def test_one_failure_demotes_immediately(self):
        bridge_mod.record_attempt("a", 5.0, model=MODEL_A)
        bridge_mod.record_attempt("b", 5.0, model=MODEL_A)
        before = self.score("a")
        bridge_mod.breaker_charge("a", "server")
        after = self.score("a")
        self.assertAlmostEqual(after - before, bridge_mod.W_FAIL_STREAK, places=6)
        _order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)
        self.assertEqual(_order[0], "b")             # no longer first

    def test_penalty_is_capped(self):
        for _ in range(10):
            bridge_mod.breaker_charge("a", "server")
        _order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)
        self.assertEqual(scores["a"]["fails_raw"], 10)
        self.assertEqual(scores["a"]["streak"], bridge_mod.FAIL_STREAK_CAP)
        self.assertAlmostEqual(scores["a"]["streak_penalty"],
                               bridge_mod.W_FAIL_STREAK * bridge_mod.FAIL_STREAK_CAP,
                               places=6)

    def test_a_success_clears_it(self):
        bridge_mod.breaker_charge("a", "server")
        self.assertAlmostEqual(self.score("a"),
                               self.score("b") + bridge_mod.W_FAIL_STREAK, places=6)
        bridge_mod.breaker_ok("a")
        self.assertAlmostEqual(self.score("a"), self.score("b"), places=6)

    def test_a_stale_failure_stops_counting(self):
        bridge_mod.breaker_charge("a", "server")
        bridge_mod._stats["a"]["last_fail_ts"] = (
            time.time() - bridge_mod.FAIL_STREAK_TTL - 1)
        _order, scores = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)
        self.assertEqual(scores["a"]["streak_penalty"], 0.0)   # nothing counted
        self.assertEqual(scores["a"]["streak_decay"], 0.0)

    def test_the_penalty_decays_to_zero(self):
        bridge_mod.breaker_charge("a", "server")
        base = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)[1]
        self.assertAlmostEqual(base["a"]["streak_penalty"], bridge_mod.W_FAIL_STREAK, places=6)
        bridge_mod._stats["a"]["last_fail_ts"] = (
            time.time() - bridge_mod.FAIL_STREAK_TTL / 2)
        half = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)[1]
        self.assertAlmostEqual(half["a"]["streak_penalty"],
                               bridge_mod.W_FAIL_STREAK * 0.5, places=1)
        bridge_mod._stats["a"]["last_fail_ts"] = (
            time.time() - bridge_mod.FAIL_STREAK_TTL - 1)
        gone = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)[1]
        self.assertEqual(gone["a"]["streak_penalty"], 0.0)     # recovered on its own
        self.assertEqual(gone["a"]["streak_decay"], 0.0)

    def test_probe_interval_grows_with_consecutive_failures(self):
        now = time.time()
        bridge_mod.breaker_charge("a", "server")
        bridge_mod._stats["a"]["attempt_ts"] = now - bridge_mod.PROBE_AFTER - 1
        self.assertTrue(bridge_mod.probe_due("a", now))
        bridge_mod._stats["a"]["fails"] = 3
        self.assertFalse(bridge_mod.probe_due("a", now))          # needs 4x the base
        bridge_mod._stats["a"]["attempt_ts"] = now - bridge_mod.PROBE_AFTER * 4 - 1
        self.assertTrue(bridge_mod.probe_due("a", now))

    def test_probe_is_not_due_while_the_breaker_owns_the_schedule(self):
        now = time.time()
        for _ in range(bridge_mod.BREAKER_THRESHOLD):
            bridge_mod.breaker_charge("a", "server")               # -> open
        bridge_mod._stats["a"]["attempt_ts"] = now - bridge_mod.PROBE_MAX * 2
        self.assertTrue(bridge_mod.breaker_open("a"))
        self.assertFalse(bridge_mod.probe_due("a", now))

    def test_a_failing_relay_is_probed_back_to_the_front(self):
        now = time.time()
        bridge_mod._prices["a"]["overall"] = 9.0                   # make it unappealing
        bridge_mod.breaker_charge("a", "server")
        bridge_mod._stats["a"]["attempt_ts"] = now - bridge_mod.PROBE_AFTER - 5
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)
        self.assertEqual(order[0], "a")                            # probed back in

    def test_probe_yields_to_a_sticky_conversation(self):
        now = time.time()
        bridge_mod.breaker_charge("a", "server")
        bridge_mod._stats["a"]["attempt_ts"] = now - bridge_mod.PROBE_AFTER - 5
        order, _ = bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A,
                                         affinity_pid="b")
        self.assertEqual(order[0], "b")

    def test_a_client_abort_is_not_a_relay_failure(self):
        """Closing the client must not charge the relay (otherwise a user hitting
        Ctrl-C would demote a healthy relay)."""
        import http.client
        port = free_port()
        relay_port = free_port()
        stop = threading.Event()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": MODEL_A}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while not stop.is_set():            # keep the stream alive
                    chunk = (b'event: response.output_text.delta\n'
                             b'data: {"type":"response.output_text.delta",'
                             b'"delta":"x"}\n\n')
                    try:
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.1)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", relay_port), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="abort-")
        routes_path = os.path.join(tmp, "routes.json")
        with open(routes_path, "w") as fh:
            json.dump({"port": port, "attempts": 2, "order": ["r"],
                       "routes": {"r": {"mount": "/p/r", "prefix": "", "name": "r",
                                        "upstream": "http://127.0.0.1:%d" % relay_port,
                                        "auth": "sk"}}}, fh)
        env = dict(os.environ, BRIDGE_ROUTES=routes_path, BRIDGE_PORT=str(port),
                   BRIDGE_STATE=os.path.join(tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(tmp, "requests.jsonl"),
                   BRIDGE_VERBOSE="0", BRIDGE_FIRST_BYTE_TIMEOUT="10",
                   BRIDGE_STREAM_STALL="60")
        proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/__bridge/status" % port, timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.1)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/p/r/responses",
                         body=json.dumps({"model": MODEL_A, "stream": True,
                                          "input": "hi"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read(64)                            # start reading, then vanish
            conn.close()
            time.sleep(1.5)
            snapshot = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/__bridge/status" % port, timeout=5).read())
            row = snapshot["routes"][0]
            self.assertEqual(row["consecutive_fails"], 0)
            self.assertEqual(row["streak"], 0)
            self.assertEqual(row["breaker"], "closed")
        finally:
            stop.set()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
            server.server_close()


class SlowFirstByteTest(unittest.TestCase):
    """A first byte slower than BRIDGE_SLOW_TTFB (default 15s) demotes a relay by
    its *rate*: the median hides the tail (6s typical, 20s on one request in
    five), which is exactly what the user feels."""

    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._last_plan["order"] = []          # no hysteresis from the last test
        bridge_mod._last_plan["signature"] = ""
        self.routes = {p: {"name": p.upper()} for p in ("a", "b")}
        for pid in ("a", "b"):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}
        for _ in range(6):
            bridge_mod.record_attempt("a", 6.0, model=MODEL_A, first_byte=True)
            bridge_mod.record_attempt("b", 6.0, model=MODEL_A, first_byte=True)

    def scores(self):
        return bridge_mod.plan_order(self.routes, ["a", "b"], None, MODEL_A)

    def test_at_the_threshold_is_not_slow_and_above_is(self):
        bridge_mod.record_attempt("a", bridge_mod.SLOW_TTFB, model=MODEL_A,
                                  first_byte=True)
        self.assertEqual(bridge_mod._stats["a"]["slow_ttfb"], 0)
        bridge_mod.record_attempt("a", bridge_mod.SLOW_TTFB + 0.1, model=MODEL_A,
                                  first_byte=True)
        self.assertEqual(bridge_mod._stats["a"]["slow_ttfb"], 1)

    def test_a_slow_first_byte_demotes_and_a_fast_one_recovers(self):
        order, scores = self.scores()
        self.assertEqual(order[0], "a")
        self.assertEqual(scores["a"]["slow_penalty"], 0.0)
        bridge_mod.record_attempt("a", 25.0, model=MODEL_A, first_byte=True)
        order, scores = self.scores()
        self.assertEqual(order[0], "b")
        self.assertAlmostEqual(scores["a"]["slow_rate"], bridge_mod.SLOW_ALPHA, places=6)
        self.assertAlmostEqual(
            scores["a"]["slow_penalty"],
            bridge_mod.W_SLOW_TTFB * bridge_mod.SLOW_ALPHA + bridge_mod.W_SLOW_STREAK,
            places=6)
        for _ in range(8):                       # fast replies decay it away
            bridge_mod.record_attempt("a", 6.0, model=MODEL_A, first_byte=True)
        self.assertLess(self.scores()[1]["a"]["slow_penalty"], 0.1)

    def test_repeated_slowness_compounds(self):
        for _ in range(4):
            bridge_mod.record_attempt("a", 20.0, model=MODEL_A, first_byte=True)
        _order, scores = self.scores()
        self.assertGreater(scores["a"]["slow_penalty"], bridge_mod.W_SLOW_TTFB * 0.5)
        self.assertEqual(scores["a"]["slow_ttfb"], 4)

    def test_a_single_slow_first_byte_demotes_immediately(self):
        before = self.scores()[1]["a"]["slow_penalty"]
        bridge_mod.record_attempt("a", 20.0, model=MODEL_A, first_byte=True)
        after = self.scores()[1]["a"]
        self.assertEqual(before, 0.0)
        self.assertEqual(after["slow_streak"], 1)
        self.assertAlmostEqual(
            after["slow_penalty"],
            bridge_mod.W_SLOW_TTFB * bridge_mod.SLOW_ALPHA + bridge_mod.W_SLOW_STREAK,
            places=6)

    def test_a_fast_reply_clears_the_slow_streak(self):
        bridge_mod.record_attempt("a", 20.0, model=MODEL_A, first_byte=True)
        bridge_mod.record_attempt("a", 20.0, model=MODEL_A, first_byte=True)
        self.assertEqual(self.scores()[1]["a"]["slow_streak"], 2)
        bridge_mod.record_attempt("a", 6.0, model=MODEL_A, first_byte=True)
        self.assertEqual(self.scores()[1]["a"]["slow_streak"], 0)

    def test_the_slow_streak_is_capped_and_decays(self):
        for _ in range(6):
            bridge_mod.record_attempt("a", 30.0, model=MODEL_A, first_byte=True)
        scores = self.scores()[1]["a"]
        self.assertEqual(scores["slow_streak"], bridge_mod.SLOW_STREAK_CAP)
        self.assertEqual(scores["slow_streak_raw"], 6)
        self.assertEqual(scores["slow_streak_decay"], 1.0)
        bridge_mod._stats["a"]["slow_streak_ts"] = (
            time.time() - bridge_mod.SLOW_STREAK_TTL - 1)
        self.assertEqual(self.scores()[1]["a"]["slow_streak_decay"], 0.0)

    def test_a_slow_failure_is_charged_as_slow_too(self):
        """2026-10-07: one attempt wasted 24s before dying, the winner measured
        10.8s - the client waited 34.8s but nothing was demoted, because only
        successful first bytes were counted."""
        bridge_mod.record_slow_event("a", 24.0)
        scores = self.scores()[1]["a"]
        self.assertGreater(scores["slow_penalty"], 0)
        self.assertEqual(bridge_mod._stats["a"]["slow_ttfb"], 1)
        self.assertAlmostEqual(bridge_mod._stats["a"]["slow_last"], 24.0)

    def test_recording_a_slow_event_does_not_pollute_latency(self):
        bridge_mod.record_attempt("a", 5.0, model=MODEL_A, first_byte=True)
        before = bridge_mod._stats["a"]["lat"]
        bridge_mod.record_slow_event("a", 24.0)
        self.assertEqual(bridge_mod._stats["a"]["lat"], before)   # latency untouched

    def test_the_waste_threshold_is_half_the_slow_threshold(self):
        self.assertAlmostEqual(bridge_mod.SLOW_WASTE_FACTOR, 0.5)
        self.assertAlmostEqual(bridge_mod.SLOW_TTFB * bridge_mod.SLOW_WASTE_FACTOR, 7.5)

    def test_a_non_streaming_total_is_not_a_first_byte(self):
        bridge_mod.record_attempt("a", 99.0, model=MODEL_A)      # no first_byte flag
        self.assertEqual(bridge_mod._stats["a"]["slow_ttfb"], 0)
        self.assertEqual(self.scores()[1]["a"]["slow_penalty"], 0.0)

    def test_it_demotes_without_being_a_failure(self):
        bridge_mod.record_attempt("a", 25.0, model=MODEL_A, first_byte=True)
        self.assertEqual(bridge_mod._stats["a"]["fails"], 0)     # not a failure
        self.assertFalse(bridge_mod.breaker_open("a"))
        self.assertEqual(bridge_mod._stats["a"]["fail"], 0.0)

    def test_the_request_log_marks_it(self):
        tmp = tempfile.mkdtemp(prefix="slowlog-")
        old = bridge_mod.REQUEST_LOG
        bridge_mod.REQUEST_LOG = os.path.join(tmp, "requests.jsonl")
        try:
            bridge_mod.log_request("m", "r", MODEL_A, 1, 200, 22.0, "ok",
                                   slow_first_byte=True)
            bridge_mod.log_request("m", "r", MODEL_A, 1, 200, 3.0, "ok",
                                   slow_first_byte=False)
        finally:
            bridge_mod.REQUEST_LOG = old
        rows = [json.loads(l) for l in open(os.path.join(tmp, "requests.jsonl"))]
        self.assertTrue(rows[0]["slow_first_byte"])
        self.assertFalse(rows[1]["slow_first_byte"])


class SlowFailureIntegrationTest(unittest.TestCase):
    """A relay that fails *slowly* must end up with both a failure and a slow
    charge, and the request must still be served by the other relay."""

    def test_slow_failure_is_charged(self):
        import http.client
        port = free_port()
        slow_relay = free_port()
        fast_relay = free_port()
        tmp = tempfile.mkdtemp(prefix="slowfail-")

        class Slow(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": MODEL_A}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                time.sleep(2.0)                     # waste the client's time
                try:
                    self.send_response(500)         # ...then fail
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        class Fast(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": MODEL_A}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                body = (b'event: response.output_text.delta\n'
                        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
                        b'event: response.completed\n'
                        b'data: {"type":"response.completed","response":'
                        b'{"status":"completed"}}\n\n')
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        servers = []
        for cls, p in ((Slow, slow_relay), (Fast, fast_relay)):
            srv = ThreadingHTTPServer(("127.0.0.1", p), cls)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append(srv)

        routes_path = os.path.join(tmp, "routes.json")
        with open(routes_path, "w") as fh:
            json.dump({"port": port, "attempts": 3, "order": ["slow", "fast"],
                       "routes": {
                           "slow": {"mount": "/p/slow", "prefix": "", "name": "slow",
                                    "upstream": "http://127.0.0.1:%d" % slow_relay,
                                    "auth": "sk"},
                           "fast": {"mount": "/p/fast", "prefix": "", "name": "fast",
                                    "upstream": "http://127.0.0.1:%d" % fast_relay,
                                    "auth": "sk"}}}, fh)
        env = dict(os.environ, BRIDGE_ROUTES=routes_path, BRIDGE_PORT=str(port),
                   BRIDGE_STATE=os.path.join(tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(tmp, "requests.jsonl"),
                   BRIDGE_VERBOSE="0", BRIDGE_ORDER_MODE="fixed",
                   BRIDGE_SLOW_WASTE_FACTOR="0.1", BRIDGE_BACKOFF="0.01",
                   BRIDGE_FIRST_BYTE_TIMEOUT="10")
        proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/__bridge/status" % port, timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.1)
            body = json.dumps({"model": MODEL_A, "stream": True, "input": "hi"}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/p/slow/responses" % port, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as fh:
                answer = fh.read()
            self.assertIn(b"response.completed", answer)
            snapshot = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/__bridge/status" % port, timeout=5).read())
            rows = {r["id"]: r for r in snapshot["routes"]}
            self.assertGreaterEqual(rows["slow"]["consecutive_fails"], 1)
            self.assertGreater(rows["slow"]["slow_rate"], 0)      # wasted time counted
            log = open(os.path.join(tmp, "bridge.log"), errors="replace").read()
            self.assertIn("SLOW-FAIL", log)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            for srv in servers:
                srv.shutdown()
                srv.server_close()


class LegacyStateTest(unittest.TestCase):
    """A stats bucket restored from an older bridge-state.json lacks fields added
    later. On 2026-10-07 that turned into KeyError('lat_by_model') inside the
    recorder, which the retry loop counted as a *relay failure*: every fail rate
    hit 0.6-0.8, breakers opened, and real requests needed 10 attempts. Every new
    field must therefore be backfilled, never assumed."""

    def setUp(self):
        bridge_mod._stats.clear()

    def test_recording_tolerates_a_legacy_bucket(self):
        legacy = {"lat": 5.0, "ok": 0.9, "fail": 0.0, "samples": 3,
                  "ts": time.time(), "lat_ts": time.time(),
                  "attempt_ts": time.time(), "byhour": {},
                  "fails": 0, "open_until": 0.0, "cooldown": 60,
                  "last_error": ""}                     # no lat_by_model, no lat_ring
        bridge_mod._stats["old"] = legacy
        bridge_mod.record_attempt("old", 4.0, model=MODEL_A)      # must not raise
        self.assertIn(MODEL_A, bridge_mod._stats["old"]["lat_by_model"])
        self.assertTrue(bridge_mod._stats["old"]["lat_ring"])
        self.assertIsNotNone(bridge_mod.effective_latency("old", MODEL_A))

    def test_every_bucket_field_is_backfilled(self):
        bridge_mod._stats["legacy"] = {}
        bucket = bridge_mod._bucket("legacy")
        for key in bridge_mod.BUCKET_DEFAULTS:
            self.assertIn(key, bucket)

    def test_mutable_defaults_are_not_shared(self):
        a = bridge_mod._bucket("a")
        a["lat_by_model"]["m"] = {"lat": 1.0}
        b = bridge_mod._bucket("b")
        self.assertEqual(b["lat_by_model"], {})
        self.assertEqual(b["lat_ring"], [])

    def test_normalize_state_fixes_everything_restored(self):
        bridge_mod._stats["x"] = {"lat": 1.0}
        bridge_mod._stats["y"] = {}
        bridge_mod.normalize_state()
        for pid in ("x", "y"):
            self.assertIn("lat_by_model", bridge_mod._stats[pid])
            self.assertIn("lat_ring", bridge_mod._stats[pid])


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod.record_attempt("p", 5.0, failed=True, model=MODEL_A)
        bridge_mod.breaker_charge("p", "server")

    def test_penalties_can_be_cleared_without_losing_measurements(self):
        before = bridge_mod._stats["p"]["lat"]
        bridge_mod._stats["p"]["fail"] = 0.7
        with bridge_mod._stats_lock:
            for pid, b in bridge_mod._stats.items():
                b.update({"fail": 0.0, "ok": 0.0, "fails": 0, "open_until": 0.0,
                          "cooldown": bridge_mod.BREAKER_COOLDOWN,
                          "last_error": ""})
        self.assertFalse(bridge_mod.breaker_open("p"))
        self.assertEqual(bridge_mod._stats["p"]["fail"], 0.0)
        self.assertEqual(bridge_mod._stats["p"]["lat"], before)   # kept


class LiveReloadTest(unittest.TestCase):
    """SIGHUP must reload without the port ever refusing a connection."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reload-")
        self.port = free_port()
        self.relay_port = free_port()
        relay = self._relay(self.relay_port)
        self.relay = relay
        routes = {"port": self.port, "attempts": 2, "order": ["r"],
                  "routes": {"r": {"mount": "/p/r", "prefix": "", "name": "r",
                                   "upstream": "http://127.0.0.1:%d" % self.relay_port,
                                   "auth": "sk"}}}
        self.routes_path = os.path.join(self.tmp, "routes.json")
        with open(self.routes_path, "w") as fh:
            json.dump(routes, fh)
        self.env = dict(os.environ, BRIDGE_ROUTES=self.routes_path,
                        BRIDGE_PORT=str(self.port),
                        BRIDGE_STATE=os.path.join(self.tmp, "state.json"),
                        BRIDGE_LOG=os.path.join(self.tmp, "bridge.log"),
                        BRIDGE_REQUEST_LOG=os.path.join(self.tmp, "requests.jsonl"),
                        BRIDGE_VERBOSE="0", BRIDGE_FIRST_BYTE_TIMEOUT="5")
        self.proc = subprocess.Popen([sys.executable, BRIDGE], env=self.env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.wait_ready()

    def _relay(self, port):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": MODEL_A}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                body = (b'event: response.output_text.delta\n'
                        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
                        b'event: response.completed\n'
                        b'data: {"type":"response.completed","response":'
                        b'{"status":"completed"}}\n\n')
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def wait_ready(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % self.port, timeout=1).read()
                return
            except Exception:
                time.sleep(0.1)
        self.fail("bridge did not come up")

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.relay.shutdown()
        self.relay.server_close()

    def test_reload_keeps_the_port_open_and_the_pid(self):
        pid_before = self.proc.pid
        os.kill(pid_before, 15 - 14)                    # 1 == SIGHUP
        refused = 0
        served = 0
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % self.port,
                    timeout=2).read()
                served += 1
            except Exception:
                refused += 1
            time.sleep(0.05)
        self.assertEqual(refused, 0, "the listening socket must never close")
        self.assertGreater(served, 20)
        self.assertEqual(self.proc.pid, pid_before, "re-exec must keep the pid")
        log = open(os.path.join(self.tmp, "bridge.log"), errors="replace").read()
        self.assertIn("adopted listening socket", log)

    def test_maintenance_endpoint_answers_immediately(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/__bridge/maintenance" % self.port,
            data=b'{"reset":[]}', headers={"Content-Type": "application/json"})
        started = time.time()
        with urllib.request.urlopen(req, timeout=5) as fh:
            payload = json.loads(fh.read())
        self.assertLess(time.time() - started, 3)        # a keep-alive hang would blow this
        self.assertEqual(payload["reset"], [])

    def test_maintenance_resets_penalties_on_the_live_bridge(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/__bridge/maintenance" % self.port,
            data=b'{"reset":["penalties"]}',
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as fh:
            payload = json.loads(fh.read())
        self.assertEqual(payload["reset"], ["penalties"])

    def test_a_request_still_works_after_the_reload(self):
        os.kill(self.proc.pid, 1)
        time.sleep(1.5)
        body = json.dumps({"model": MODEL_A, "stream": True, "input": "hi"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/p/r/responses" % self.port, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            answer = fh.read()
        self.assertIn(b"response.completed", answer)


class RobustLatencyTest(unittest.TestCase):
    """The EWMA is dragged up by the heavy tail; the median is what a user
    actually waits. Before this, every relay looked 'about 8s'."""

    def setUp(self):
        bridge_mod._stats.clear()

    def test_median_ignores_the_outlier(self):
        for value in (6.0, 6.2, 6.4, 6.1, 30.0):        # one stall
            bridge_mod.record_attempt("p", value, model=MODEL_A)
        got = bridge_mod.effective_latency("p", MODEL_A)
        self.assertAlmostEqual(got, 6.2, places=6)
        self.assertLess(got, bridge_mod._stats["p"]["lat"])   # EWMA is higher

    def test_falls_back_to_ewma_while_the_ring_is_thin(self):
        bridge_mod.record_attempt("p", 9.0, model=MODEL_A)
        bridge_mod.record_attempt("p", 9.0, model=MODEL_A)
        self.assertAlmostEqual(bridge_mod.effective_latency("p", MODEL_A), 9.0, places=6)

    def test_ring_is_bounded(self):
        for i in range(50):
            bridge_mod.record_attempt("p", 1.0 + i * 0.01, model=MODEL_A)
        self.assertLessEqual(len(bridge_mod._stats["p"]["lat_ring"]),
                             bridge_mod.LAT_RING)
        self.assertLessEqual(len(bridge_mod._stats["p"]["lat_by_model"][MODEL_A]["ring"]),
                             bridge_mod.LAT_RING)

    def test_typical_speed_decides_and_a_rare_stall_does_not_disqualify(self):
        routes = {p: {"name": p.upper()} for p in ("steady", "spiky")}
        bridge_mod._prices.clear()
        for pid in ("steady", "spiky"):
            bridge_mod._prices[pid] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}
        for value in (6.0, 6.2, 6.4, 6.1, 25.0):        # typical 6.2, one 25s stall
            bridge_mod.record_attempt("spiky", value, model=MODEL_A)
        for _ in range(5):
            bridge_mod.record_attempt("steady", 6.3, model=MODEL_A)
        # both have the same price, so the faster *typical* relay wins - the
        # 25s stall does not erase spiky's better median
        order, _ = bridge_mod.plan_order(routes, ["spiky", "steady"], None, MODEL_A)
        self.assertEqual(order[0], "spiky")
        self.assertLess(bridge_mod.effective_latency("spiky", MODEL_A),
                        bridge_mod.effective_latency("steady", MODEL_A))

if __name__ == "__main__":
    unittest.main(verbosity=2)
