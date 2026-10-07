#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for reconcile.py - the cost-truth report (bridge estimate vs CC Switch's
shared price table vs actual balance movement).

    python3 tests/test_reconcile.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import reconcile as rc                                          # noqa: E402


class MathTest(unittest.TestCase):
    def test_weighted_million_uses_the_documented_weights(self):
        tokens = {"input": 1_000_000, "output": 1_000_000,
                  "cache_read": 1_000_000, "cache_creation": 1_000_000}
        self.assertAlmostEqual(rc.weighted_million(tokens), 1 + 4 + 0.1 + 1.25)

    def test_missing_fields_are_zero(self):
        self.assertAlmostEqual(rc.weighted_million({"input": 2_000_000}), 2.0)


class BridgeWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reconcile-")
        self.old = rc.REQUESTS
        rc.REQUESTS = os.path.join(self.tmp, "requests.jsonl")
        self.now = time.time()
        rows = [
            {"epoch": self.now - 60, "result": "ok", "relay": "A",
             "tokens": {"input": 1_000_000}, "est_cost_usd": 0.05},
            {"epoch": self.now - 120, "result": "pass", "relay": "B",
             "tokens": {"input": 2_000_000}, "est_cost_usd": 0.10},
            {"epoch": self.now - 180, "result": "error", "relay": "C",
             "tokens": {"input": 9_000_000}, "est_cost_usd": 9.0},   # failure: ignored
            {"epoch": self.now - 7200, "result": "ok", "relay": "D",  # outside window
             "tokens": {"input": 1_000_000}, "est_cost_usd": 5.0},
            {"epoch": self.now - 30, "result": "ok", "relay": "A"},   # no usage: ignored
        ]
        with open(rc.REQUESTS, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def tearDown(self):
        rc.REQUESTS = self.old

    def test_only_successful_rows_with_usage_inside_the_window_count(self):
        out = rc.bridge_window(self.now - 3600, self.now)
        self.assertEqual(out["rows"], 2)
        self.assertAlmostEqual(out["cost"], 0.15, places=6)
        self.assertAlmostEqual(out["wm"], 3.0, places=6)
        self.assertEqual(set(out["per_relay"]), {"A", "B"})

    def test_missing_file_is_not_an_error(self):
        rc.REQUESTS = os.path.join(self.tmp, "nope.jsonl")
        out = rc.bridge_window(self.now - 60, self.now)
        self.assertEqual(out["cost"], 0.0)


class CcSwitchWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccsw-")
        self.db = os.path.join(self.tmp, "cc-switch.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE proxy_request_logs ("
                     "created_at INTEGER, total_cost_usd TEXT, data_source TEXT)")
        now = int(time.time())
        conn.executemany("INSERT INTO proxy_request_logs VALUES (?,?,?)", [
            (now - 60, "0.30", "proxy"),
            (now - 120, "0.20", "proxy"),
            (now - 130, "9.99", "codex_session"),          # not proxy: ignored
            (now - 9000, "5.00", "proxy")])                # outside window
        conn.commit()
        conn.close()
        self.old = rc.DB
        rc.DB = self.db

    def tearDown(self):
        rc.DB = self.old

    def test_sums_proxy_rows_in_the_window(self):
        out = rc.ccswitch_window(time.time() - 3600, time.time())
        self.assertEqual(out["rows"], 2)
        self.assertAlmostEqual(out["cost"], 0.50, places=6)

    def test_broken_db_reports_an_error_instead_of_raising(self):
        rc.DB = os.path.join(self.tmp, "missing.db")
        out = rc.ccswitch_window(0, time.time())
        self.assertEqual(out["cost"], 0.0)
        self.assertIn("error", out)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ledger-")
        self.old = rc.LEDGER
        rc.LEDGER = os.path.join(self.tmp, "balance-history.jsonl")

    def tearDown(self):
        rc.LEDGER = self.old

    def test_append_and_read_round_trip(self):
        rc.append_sample({"pp": (100.0, "pp.dog"), "wdlink": (50.0, "wdlink.xyz")},
                         ts=1000.0)
        rows = rc.read_ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["balances"], {"pp": 100.0, "wdlink": 50.0})
        self.assertEqual(rows[0]["ts"], 1000.0)

    def test_account_delta_is_first_minus_last(self):
        first = {"ts": 1.0, "balances": {"pp": 10.0, "wdlink": 5.0}}
        last = {"ts": 2.0, "balances": {"pp": 8.5, "wdlink": 4.0}}
        delta = rc.account_delta([first, last], 1.0, 2.0)
        self.assertAlmostEqual(delta["pp"], 1.5)
        self.assertAlmostEqual(delta["wdlink"], 1.0)

    def test_a_new_relay_in_the_last_sample_is_skipped(self):
        first = {"ts": 1.0, "balances": {"pp": 10.0}}
        last = {"ts": 2.0, "balances": {"pp": 9.0, "new": 3.0}}
        delta = rc.account_delta([first, last], 1.0, 2.0)
        self.assertEqual(set(delta), {"pp"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
