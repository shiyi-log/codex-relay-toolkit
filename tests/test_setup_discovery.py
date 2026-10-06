#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression test: a *copied* relay must not inherit the source's upstream.

Run:

    python3 tests/test_setup_discovery.py          # unittest, no network needed
    python3 -m unittest discover -s tests

It builds a throwaway CC Switch database plus two local mock relays (each one
only answers /models for its own API key) and runs setup.py against them.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(os.path.dirname(HERE), "setup.py")
PORT = 15888
SRC_ID = "11111111-1111-1111-1111-111111111111"
COPY_ID = "22222222-2222-2222-2222-222222222222"
DIRECT_ID = "33333333-3333-3333-3333-333333333333"
BADCOPY_ID = "44444444-4444-4444-4444-444444444444"
GHOST_ID = "55555555-5555-5555-5555-555555555555"   # deleted in CC Switch


class MockRelay:
    """Answers /models (and /v1/models) only for one API key."""

    def __init__(self, key):
        self.key = key
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                auth = self.headers.get("Authorization") or ""
                if not self.path.endswith("/models") or auth != "Bearer " + outer.key:
                    self.send_response(401)
                    body = b'{"error":{"message":"unauthorized"}}'
                else:
                    self.send_response(200)
                    body = json.dumps({"data": [{"id": "gpt-6.1-sol"}]}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def provider_config(base_url):
    return json.dumps({"auth": {}, "config": (
        'model = "gpt-6.1-sol"\n'
        'model_provider = "custom"\n\n'
        '[model_providers.custom]\n'
        'name = "OpenAI"\n'
        'base_url = "%s"\n'
        'wire_api = "responses"\n' % base_url)}, ensure_ascii=False)


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = {"src": "sk-src", "copy": "sk-copy", "direct": "sk-direct"}
        cls.src_relay = MockRelay(cls.keys["src"])
        cls.copy_relay = MockRelay(cls.keys["copy"])
        cls.direct_relay = MockRelay(cls.keys["direct"])
        # a host that never accepts badcopy's key
        cls.bad_relay = MockRelay("sk-somebody-else")

        cls.tmp = tempfile.mkdtemp(prefix="relay-setup-test-")
        cls.db = os.path.join(cls.tmp, "cc-switch.db")
        cls.routes = os.path.join(cls.tmp, "routes.json")
        cls.originals = os.path.join(cls.tmp, "originals.json")

        conn = sqlite3.connect(cls.db)
        conn.execute("CREATE TABLE providers ("
                     "id TEXT, app_type TEXT, name TEXT, settings_config TEXT, "
                     "category TEXT, meta TEXT, in_failover_queue INTEGER, "
                     "sort_index INTEGER, website_url TEXT)")
        rows = [
            (SRC_ID, "中转 A", provider_config(cls.src_relay.url + "/v1"), None, 10,
             None, cls.keys["src"]),
            # copied from A *after* A was bridged: mount points at A
            (COPY_ID, "中转 B", provider_config("http://127.0.0.1:%d/p/%s" % (PORT, SRC_ID)),
             None, 11, cls.copy_relay.url, cls.keys["copy"]),
            # direct base_url must beat a stale originals.json entry
            (DIRECT_ID, "中转 C", provider_config(cls.direct_relay.url), None, 12,
             None, cls.keys["direct"]),
            # copied from A, but its website_url rejects its own key
            (BADCOPY_ID, "中转 D", provider_config("http://127.0.0.1:%d/p/%s" % (PORT, SRC_ID)),
             None, 13, cls.bad_relay.url, "sk-badcopy"),
        ]
        for pid, name, cfg, category, sort_index, website, key in rows:
            cfg = json.loads(cfg)
            cfg["auth"] = {"OPENAI_API_KEY": key}
            meta = json.dumps({"usage_script": {"enabled": True,
                                                "baseUrl": "https://wrong.example"}})
            conn.execute("INSERT INTO providers VALUES (?,?,?,?,?,?,?,?,?)",
                         (pid, "codex", name, json.dumps(cfg, ensure_ascii=False),
                          category, meta, 1, sort_index, website))
        conn.commit()
        conn.close()

        with open(cls.originals, "w") as fh:
            # stale record: C has since been re-pointed in the CC Switch UI
            # ghost record: E was deleted in the CC Switch UI altogether
            json.dump({DIRECT_ID: "https://stale.example",
                       GHOST_ID: "https://gone.example"}, fh)

        env = dict(os.environ, BRIDGE_DB=cls.db, BRIDGE_ROUTES=cls.routes,
                   BRIDGE_ORIGINALS=cls.originals, BRIDGE_OFFICIAL="0",
                   BRIDGE_PROBE_TIMEOUT="3")
        cls.proc = subprocess.run([sys.executable, SETUP], env=env,
                                  capture_output=True, text=True)
        cls.out = cls.proc.stdout + cls.proc.stderr
        with open(cls.routes) as fh:
            cls.written = json.load(fh)
        cls.conn = sqlite3.connect("file:%s?mode=ro" % cls.db, uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for relay in (cls.src_relay, cls.copy_relay, cls.direct_relay, cls.bad_relay):
            relay.stop()

    def route(self, pid):
        return self.written["routes"][pid]

    def test_setup_succeeds_and_keeps_the_source(self):
        self.assertEqual(self.proc.returncode, 0, self.out)
        self.assertEqual(self.route(SRC_ID)["upstream"], self.src_relay.url)

    def test_copy_does_not_borrow_the_source_upstream(self):
        route = self.route(COPY_ID)
        self.assertEqual(route["upstream"], self.copy_relay.url)
        self.assertNotEqual(route["upstream"], self.src_relay.url)
        self.assertEqual(route["mount"], "/p/%s" % COPY_ID)

    def test_copy_balance_query_points_at_its_own_relay(self):
        cfg, meta, base = self.conn.execute(
            "SELECT settings_config, meta, 1 FROM providers WHERE id=?",
            (COPY_ID,)).fetchone()
        conf = json.loads(cfg)["config"]
        self.assertIn("base_url = \"http://127.0.0.1:%d/p/%s\"" % (PORT, COPY_ID), conf)
        usage = json.loads(meta)["usage_script"]
        self.assertEqual(usage["baseUrl"], self.copy_relay.url)

    def test_direct_base_url_beats_stale_original(self):
        self.assertEqual(self.route(DIRECT_ID)["upstream"], self.direct_relay.url)

    def test_unverifiable_copy_is_kept_with_a_warning(self):
        self.assertIn(BADCOPY_ID, self.written["routes"])
        self.assertEqual(self.route(BADCOPY_ID)["upstream"], self.bad_relay.url)
        self.assertIn("WARN", self.out)

    def test_every_route_is_in_the_round_robin_order(self):
        for pid in (SRC_ID, COPY_ID, DIRECT_ID, BADCOPY_ID):
            self.assertIn(pid, self.written["order"])

    def test_deleted_provider_is_pruned_from_originals(self):
        with open(self.originals) as fh:
            kept = json.load(fh)
        self.assertNotIn(GHOST_ID, kept)              # gone from CC Switch
        self.assertNotIn(GHOST_ID, self.written["routes"])
        self.assertEqual(kept[DIRECT_ID], self.direct_relay.url)   # refreshed


if __name__ == "__main__":
    unittest.main(verbosity=2)
