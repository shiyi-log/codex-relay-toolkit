#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for relay_attrib.py: CC Switch's request-log rows must be re-pointed at
the relay that really served, matched by the response id, and be reversible.

    python3 tests/test_relay_attrib.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ATTRIB = os.path.join(ROOT, "relay_attrib.py")

MOUNT = "5fee0521-be88-4400-b9e9-65d6b64a6219"
REAL = "9b1ba997-4636-45cc-b3aa-5497c3bd6ac3"
OTHER = "44594b87-1d73-49f3-a5bb-50a7f779e39f"
RESP = "resp_abc123"


def req_line(**kw):
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": time.time(),
           "mount": "pp 特惠",
           "relay": "pp  plus", "model": "gpt-6.1-sol", "attempt": 1, "status": 200,
           "result": "ok", "mount_id": MOUNT, "relay_id": REAL, "response_id": RESP}
    row.update(kw)
    return json.dumps(row, ensure_ascii=False)


class RelayAttribTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="attrib-")
        self.db = os.path.join(self.tmp, "cc-switch.db")
        self.log = os.path.join(self.tmp, "bridge-requests.jsonl")
        self.env = dict(os.environ, BRIDGE_DB=self.db, BRIDGE_REQUEST_LOG=self.log,
                        ATTRIB_STATE=os.path.join(self.tmp, "state.json"),
                        ATTRIB_CHANGELOG=os.path.join(self.tmp, "changelog.jsonl"))
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE proxy_request_logs (request_id TEXT PRIMARY KEY,"
                     " provider_id TEXT, model TEXT, data_source TEXT)")
        conn.execute("INSERT INTO proxy_request_logs VALUES (?,?,?,?)",
                     ("session:codex:%s:%s" % (MOUNT, RESP), MOUNT, "gpt-6.1-sol", "proxy"))
        # a Codex-session row that happens to share nothing: must never be touched
        conn.execute("INSERT INTO proxy_request_logs VALUES (?,?,?,?)",
                     ("session:codex:_codex_session:resp_zzz", "_codex_session",
                      "gpt-6.1-sol", "codex_session"))
        conn.commit()
        conn.close()
        with open(self.log, "w") as fh:
            fh.write(req_line() + "\n")                       # normal, should be fixed
            fh.write(req_line(result="retry", response_id=None) + "\n")   # retry: skip
            fh.write(req_line(response_id="resp_not_in_db") + "\n")       # unknown: skip

    def provider_of(self, request_id):
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT provider_id FROM proxy_request_logs WHERE request_id=?",
                           (request_id,)).fetchone()
        conn.close()
        return row[0] if row else None

    def run_attrib(self, *args):
        return subprocess.run([sys.executable, ATTRIB, *args], env=self.env,
                              capture_output=True, text=True)

    def test_dry_run_changes_nothing(self):
        out = self.run_attrib("--once", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("dry-run", out.stdout)
        self.assertEqual(self.provider_of("session:codex:%s:%s" % (MOUNT, RESP)), MOUNT)

    def test_row_is_repointed_to_the_real_relay(self):
        out = self.run_attrib("--once")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.provider_of("session:codex:%s:%s" % (MOUNT, RESP)), REAL)

    def test_idempotent_and_session_rows_untouched(self):
        self.run_attrib("--once")
        second = self.run_attrib("--once")
        self.assertIn("0/", second.stdout.split("已改")[1][:6])
        self.assertEqual(self.provider_of("session:codex:_codex_session:resp_zzz"),
                         "_codex_session")

    def test_rollback_restores_the_original_provider(self):
        self.run_attrib("--once")
        self.assertEqual(self.provider_of("session:codex:%s:%s" % (MOUNT, RESP)), REAL)
        out = self.run_attrib("--rollback")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.provider_of("session:codex:%s:%s" % (MOUNT, RESP)), MOUNT)

    def test_status_reports_progress(self):
        self.run_attrib("--once")
        out = self.run_attrib("--status")
        self.assertIn("累计修正   : 1 行", out.stdout)

    def test_row_written_later_is_retried(self):
        # the request-log line exists but CC Switch has not written its row yet
        late = "session:codex:%s:resp_late1" % MOUNT
        with open(self.log, "a") as fh:
            fh.write(req_line(response_id="resp_late1") + "\n")
        self.run_attrib("--once")
        self.assertIsNone(self.provider_of(late))
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO proxy_request_logs VALUES (?,?,?,?)",
                     (late, MOUNT, "gpt-6.1-sol", "proxy"))
        conn.commit()
        conn.close()
        out = self.run_attrib("--once")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.provider_of(late), REAL)   # picked up on the retry


if __name__ == "__main__":
    unittest.main(verbosity=2)
