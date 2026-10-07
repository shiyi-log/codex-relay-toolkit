#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Subscription-quota pacing: the account allowance is perishable, so spend it
against an even pace of the window - never all at once, never left to expire.

    python3 tests/test_quota_pace.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import bridge as bridge_mod                                   # noqa: E402

WINDOW = 604800          # 7 days, the real shape of the prolite plan
ACCT = "acct"


def quota_snapshot(**kw):
    now = time.time()
    base = {"usage_ts": now, "probe_ts": now, "source": "probe", "ts": now,
            "plan": "prolite",
            "allowed": True, "limit_reached": False, "used_percent": 0,
            "window_s": WINDOW, "reset_in_s": WINDOW - int(WINDOW * 0.06),
            "reset_at": int(time.time()) + WINDOW}
    base.update(kw)
    return base


class PaceTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._pace["n"] = 0

    def pace(self, now=None, **kw):
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(**kw)
        return bridge_mod.quota_pace(ACCT, now or time.time())

    def test_a_fresh_window_is_behind_and_gets_used(self):
        got = self.pace()
        self.assertEqual(got["state"], "behind")          # 0% used, 6% elapsed
        self.assertGreater(got["share"], 0)
        self.assertLessEqual(got["share"], bridge_mod.QUOTA_CATCHUP_MAX)

    def test_spending_on_pace_is_left_alone(self):
        got = self.pace(used_percent=6.0)
        self.assertEqual(got["state"], "on-track")
        self.assertEqual(got["share"], 0.0)

    def test_spending_too_fast_stops_the_fallback(self):
        got = self.pace(used_percent=60.0)
        self.assertEqual(got["state"], "ahead")
        self.assertEqual(got["share"], 0.0)

    def test_quota_left_near_the_reset_triggers_a_final_push(self):
        got = self.pace(used_percent=3.0, reset_in_s=int(0.8 * 86400))
        self.assertEqual(got["state"], "behind")
        self.assertEqual(got["share"], 1.0)               # use it or lose it

    def test_nothing_left_near_the_reset_is_not_a_push(self):
        got = self.pace(used_percent=90.0, reset_in_s=int(0.8 * 86400))
        self.assertEqual(got["state"], "on-track")
        self.assertEqual(got["share"], 0.0)

    def test_an_explicit_expiry_compresses_the_horizon(self):
        old = bridge_mod.QUOTA_EXPIRES
        bridge_mod.QUOTA_EXPIRES = time.time() + 2 * 86400
        try:
            got = self.pace(used_percent=0.0)
        finally:
            bridge_mod.QUOTA_EXPIRES = old
        self.assertEqual(got["state"], "behind")
        self.assertGreater(got["target_percent"], 50)     # most of it should be gone
        self.assertGreaterEqual(got["share"], bridge_mod.QUOTA_CATCHUP_SHARE)

    def test_a_bogus_header_reset_cannot_trigger_the_final_push(self):
        """Header-derived reset times can be 0/nonsense; trusting one made the
        final push fire, i.e. the account would serve *every* request."""
        bridge_mod._bucket(ACCT)["quota"] = {"usage_ts": time.time(),
                                             "used_percent": 0.0,
                                             "reset_in_s": 0, "source": "headers",
                                             "window_s": WINDOW}
        got = bridge_mod.quota_pace(ACCT)
        self.assertEqual(got["state"], "behind")
        self.assertLessEqual(got["share"], bridge_mod.QUOTA_CATCHUP_MAX)

    def test_a_fresh_probe_wins_over_misleading_headers(self):
        bridge_mod._bucket(ACCT)["quota"] = {
            "usage_ts": time.time(), "probe_ts": time.time(), "source": "probe",
            "used_percent": 0.0, "window_s": WINDOW, "reset_in_s": WINDOW - 3600}
        bridge_mod.note_quota(ACCT, [("x-codex-primary-used-percent", "10"),
                                     ("x-codex-primary-window-minutes", "300"),
                                     ("x-codex-primary-reset-after-seconds", "1")])
        snap = bridge_mod._stats[ACCT]["quota"]
        self.assertEqual(snap["window_s"], WINDOW)          # probe schedule kept
        self.assertEqual(snap["reset_in_s"], WINDOW - 3600)
        self.assertEqual(snap["used_percent"], 10.0)        # headers still refresh usage

    def test_no_data_means_no_action(self):
        bridge_mod._stats.clear()
        got = bridge_mod.quota_pace(ACCT)
        self.assertEqual(got["state"], "unknown")
        self.assertEqual(got["share"], 0.0)


class PlanIntegrationTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()
        bridge_mod._pace["n"] = 0
        bridge_mod._last_plan["order"] = []
        bridge_mod._last_plan["signature"] = ""
        self.routes = {
            "relay": {"name": "relay", "upstream": "http://relay"},
            ACCT: {"name": "my account", "upstream": "http://acct",
                   "auth_type": "oauth", "auth_file": "/tmp/none.json"},
        }
        bridge_mod._prices["relay"] = {"ts": time.time(), "per_model": {},
                                       "overall": 0.05, "trend": 1.0, "error": ""}

    def order(self, count=1):
        out = []
        for _ in range(count):
            rows, _scores = bridge_mod.plan_order(
                self.routes, ["relay", ACCT], None, "gpt-6.1-sol", remember=True)
            out.append(rows)
        return out

    def test_on_track_keeps_the_account_as_the_last_resort(self):
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=6.0)
        rows = self.order()[0]
        self.assertEqual(rows[-1], ACCT)

    def test_behind_promotes_the_account_to_the_front(self):
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=0.0,
                                                           reset_in_s=int(0.5 * 86400))
        rows = self.order()[0]
        self.assertEqual(rows[0], ACCT)                   # final push: share = 1.0

    def test_ahead_drops_the_account_entirely(self):
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=80.0)
        rows = self.order()[0]
        self.assertNotIn(ACCT, rows)
        self.assertEqual(rows, ["relay"])

    def test_promotions_are_spread_over_requests(self):
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=0.0)
        orders = self.order(6)
        promoted = sum(1 for rows in orders if rows[0] == ACCT)
        self.assertGreater(promoted, 0)
        self.assertLess(promoted, len(orders))            # not every request

    def test_pacing_does_not_hijack_a_pinned_conversation(self):
        """Moving a live conversation between the account and a relay makes the
        backend reject its item ids, so pacing must leave pinned threads alone."""
        bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=0.0)
        rows, _ = bridge_mod.plan_order(self.routes, ["relay", ACCT], None,
                                        "gpt-6.1-sol", remember=True,
                                        affinity_pid="relay")
        self.assertEqual(rows[0], "relay")
        rows, _ = bridge_mod.plan_order(self.routes, ["relay", ACCT], None,
                                        "gpt-6.1-sol", remember=True,
                                        affinity_pid=ACCT)
        self.assertEqual(rows[0], ACCT)           # stays where its state lives

    def test_pacing_can_be_switched_off(self):
        old = bridge_mod.QUOTA_PACE
        bridge_mod.QUOTA_PACE = False
        try:
            bridge_mod._bucket(ACCT)["quota"] = quota_snapshot(used_percent=0.0,
                                                               reset_in_s=3600)
            rows = self.order()[0]
        finally:
            bridge_mod.QUOTA_PACE = old
        self.assertEqual(rows[-1], ACCT)                  # back to plain fallback


class ProbeTest(unittest.TestCase):
    """The probe must read the allowance without spending any of it, and must
    never keep the account email that the same body carries."""

    def setUp(self):
        self.hits = []
        self.limit_reached = False
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                outer.hits.append(self.path)
                payload = json.dumps({
                    "user_id": "user-secret", "email": "someone@example.com",
                    "account_id": "acct-1", "plan_type": "prolite",
                    "rate_limit": {"allowed": True,
                                   "limit_reached": outer.limit_reached,
                                   "primary_window": {"used_percent": 12.5,
                                                      "limit_window_seconds": WINDOW,
                                                      "reset_after_seconds": 500000,
                                                      "reset_at": int(time.time()) + 500000}},
                    "credits": {"balance": "0", "has_credits": False},
                    "model_usage": {"gpt-6-astra": {"available": True}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp(prefix="quota-")
        self.auth = os.path.join(self.tmp, "auth.json")
        import base64
        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
        with open(self.auth, "w") as fh:
            json.dump({"auth_mode": "chatgpt",
                       "tokens": {"access_token": "%s.%s.sig" % (
                           b64({"alg": "none"}), b64({"exp": time.time() + 3600})),
                           "account_id": "acct-1"}}, fh)
        os.chmod(self.auth, 0o600)
        bridge_mod._stats.clear()
        self.old_url = bridge_mod.QUOTA_USAGE_URL
        bridge_mod.QUOTA_USAGE_URL = "http://127.0.0.1:%d/usage" % self.server.server_address[1]

    def tearDown(self):
        bridge_mod.QUOTA_USAGE_URL = self.old_url
        self.server.shutdown()
        self.server.server_close()

    def route(self):
        return {"auth_type": "oauth", "auth_file": self.auth, "_pid": ACCT,
                "name": "my account"}

    def test_probe_parses_the_window_and_never_stores_the_email(self):
        snap = bridge_mod.quota_probe(self.route(), force=True)
        self.assertEqual(snap["plan"], "prolite")
        self.assertEqual(snap["used_percent"], 12.5)
        self.assertEqual(snap["window_s"], WINDOW)
        stored = json.dumps(bridge_mod._stats[ACCT]["quota"], ensure_ascii=False)
        self.assertNotIn("example.com", stored)
        self.assertNotIn("user-secret", stored)

    def test_probe_is_cached_within_the_ttl(self):
        bridge_mod.quota_probe(self.route(), force=True)
        before = len(self.hits)
        bridge_mod.quota_probe(self.route())
        self.assertEqual(len(self.hits), before)           # no second request

    def test_a_failed_probe_records_why_and_returns_none(self):
        bridge_mod.QUOTA_USAGE_URL = "http://127.0.0.1:1/usage"
        self.assertIsNone(bridge_mod.quota_probe(self.route(), force=True))
        self.assertIn("quota_error", bridge_mod._stats[ACCT])

    def test_limit_reached_parks_the_account(self):
        """When the backend says the allowance is gone, park it until the reset
        instead of feeding it requests that would 429."""
        self.limit_reached = True
        snap = bridge_mod.quota_probe(self.route(), force=True)
        self.assertTrue(snap["limit_reached"])
        self.assertTrue(bridge_mod.breaker_open(ACCT))
        self.assertEqual(bridge_mod._stats[ACCT]["last_error"], "quota")


if __name__ == "__main__":
    unittest.main(verbosity=2)
