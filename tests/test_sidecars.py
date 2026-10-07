#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sidecar integration: a chat-completions-only upstream must work through
codex-relay + our bridge, with model mapping and a declared fixed price.

The end-to-end test uses the real `codex-relay` binary when it is available
(set BRIDGE_CODEX_RELAY or install it with `python3 sidecar.py --install`);
otherwise that test is skipped and only the bridge-side logic is checked.

    python3 tests/test_sidecars.py
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
SETUP = os.path.join(ROOT, "setup.py")
sys.path.insert(0, ROOT)
import bridge as bridge_mod                                   # noqa: E402

MODEL = "gpt-6.1-sol"
UPSTREAM_MODEL = "deepseek-chat"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def relay_binary():
    for candidate in (os.environ.get("BRIDGE_CODEX_RELAY"),
                      os.path.join(ROOT, "bin", "codex-relay"),
                      "/tmp/crw/ex/codex_relay-0.5.8.data/scripts/codex-relay"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    found = shutil_which("codex-relay")
    return found


def shutil_which(name):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = os.path.join(directory, name)
        if os.access(path, os.X_OK):
            return path
    return None


class ChatOnlyUpstream:
    """A provider that only speaks /v1/chat/completions (like DeepSeek)."""

    def __init__(self):
        self.models = []
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = json.dumps({"data": [{"id": UPSTREAM_MODEL}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                outer.hits += 1
                outer.models.append(req.get("model"))
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                def sse(obj):
                    raw = ("data: " + json.dumps(obj) + "\n\n").encode()
                    self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                    self.wfile.flush()

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                sse({"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "model": req.get("model"),
                     "choices": [{"index": 0, "delta": {"role": "assistant",
                                                        "content": "hello from "},
                                  "finish_reason": None}]})
                sse({"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "model": req.get("model"),
                     "choices": [{"index": 0, "delta": {"content": UPSTREAM_MODEL},
                                  "finish_reason": None}]})
                sse({"id": "chatcmpl-1", "object": "chat.completion.chunk",
                     "model": req.get("model"),
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1200, "completion_tokens": 7,
                               "total_tokens": 1207,
                               "prompt_tokens_details": {"cached_tokens": 1000}}})
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ModelMapTest(unittest.TestCase):
    def setUp(self):
        bridge_mod._stats.clear()
        bridge_mod._prices.clear()

    def test_exact_then_wildcard(self):
        route = {"model_map": {MODEL: "kimi-k2", "*": "deepseek-chat"}}
        body, mapped = bridge_mod.apply_model_map(
            json.dumps({"model": MODEL, "input": "x"}).encode(), "p", route)
        self.assertEqual(mapped, "kimi-k2")
        self.assertEqual(json.loads(body)["model"], "kimi-k2")

        body, mapped = bridge_mod.apply_model_map(
            json.dumps({"model": "gpt-6-sol"}).encode(), "p", route)
        self.assertEqual(mapped, "deepseek-chat")

    def test_no_map_is_a_noop(self):
        route = {}
        original = json.dumps({"model": MODEL}).encode()
        body, mapped = bridge_mod.apply_model_map(original, "p", route)
        self.assertIsNone(mapped)
        self.assertIs(body, original)

    def test_adapt_model_defers_to_an_explicit_map(self):
        route = {"model_map": {"*": "deepseek-chat"}, "auth": "sk"}
        bridge_mod._models_cache["p"] = {"ids": {"gpt-6-sol"}, "ts": time.time()}
        body = json.dumps({"model": MODEL}).encode()
        self.assertIs(bridge_mod.adapt_model(body, "p", route), body)

    def test_declared_price_is_used_for_costing(self):
        bridge_mod.load_routes  # (documentation: seeded by load_routes)
        with bridge_mod._stats_lock:
            bridge_mod._prices["sidecar-deepseek"] = {
                "ts": time.time(), "per_model": {}, "overall": 0.28,
                "trend": 1.0, "error": "", "static": True}
        price, exact = bridge_mod.price_index("sidecar-deepseek", MODEL)
        self.assertEqual(price, 0.28)
        self.assertFalse(exact)


class SetupMergeTest(unittest.TestCase):
    def test_sidecar_entries_become_routes(self):
        tmp = tempfile.mkdtemp(prefix="sidecar-")
        cfg = os.path.join(tmp, "sidecars.json")
        with open(cfg, "w") as fh:
            json.dump({"relays": [
                {"id": "deepseek", "name": "DeepSeek 直连", "port": 15901,
                 "upstream": "https://api.deepseek.com/v1", "api_key": "sk-x",
                 "model_map": {"*": "deepseek-chat"}, "price_per_m": 0.28},
                {"id": "broken", "upstream": "https://x"},          # no port
            ]}, fh)
        out = subprocess.run(
            [sys.executable, "-c",
             "import os,sys,json; sys.path.insert(0,%r); import setup; "
             "print(json.dumps([setup.sidecar_route(e) for e in setup.load_sidecars()]))" % ROOT],
            env=dict(os.environ, BRIDGE_SIDECARS=cfg), capture_output=True, text=True)
        rows = json.loads(out.stdout)
        rid, route = rows[0]
        self.assertEqual(rid, "sidecar-deepseek")
        self.assertEqual(route["upstream"], "http://127.0.0.1:15901")
        self.assertEqual(route["prefix"], "/v1")
        self.assertEqual(route["model_map"], {"*": "deepseek-chat"})
        self.assertEqual(route["price_per_m"], 0.28)
        self.assertIsNone(rows[1][0])                  # invalid entry rejected
        self.assertEqual(rows[1][1], "missing/invalid port")


@unittest.skipUnless(relay_binary(), "codex-relay binary not installed")
class EndToEndSidecarTest(unittest.TestCase):
    """Codex -> our bridge -> codex-relay -> chat-completions-only upstream."""

    @classmethod
    def setUpClass(cls):
        cls.upstream = ChatOnlyUpstream()
        cls.sidecar_port = free_port()
        cls.sidecar = subprocess.Popen(
            [relay_binary(), "--port", str(cls.sidecar_port),
             "--upstream", "http://127.0.0.1:%d/v1" % cls.upstream.port,
             "--api-key", "sk-test"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/v1/models" % cls.sidecar_port, timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)

        cls.bridge_port = free_port()
        cls.tmp = tempfile.mkdtemp(prefix="sidecar-e2e-")
        routes = {"port": cls.bridge_port, "attempts": 3, "order": ["sidecar-deepseek"],
                  "routes": {"sidecar-deepseek": {
                      "mount": "/p/sidecar-deepseek", "prefix": "/v1",
                      "name": "DeepSeek 直连",
                      "upstream": "http://127.0.0.1:%d" % cls.sidecar_port,
                      "auth": "", "model_map": {"*": UPSTREAM_MODEL},
                      "price_per_m": 0.28}}}
        cls.routes_path = os.path.join(cls.tmp, "routes.json")
        with open(cls.routes_path, "w") as fh:
            json.dump(routes, fh)
        env = dict(os.environ, BRIDGE_ROUTES=cls.routes_path,
                   BRIDGE_PORT=str(cls.bridge_port),
                   BRIDGE_STATE=os.path.join(cls.tmp, "state.json"),
                   BRIDGE_LOG=os.path.join(cls.tmp, "bridge.log"),
                   BRIDGE_REQUEST_LOG=os.path.join(cls.tmp, "requests.jsonl"),
                   BRIDGE_VERBOSE="0", BRIDGE_FIRST_BYTE_TIMEOUT="20",
                   BRIDGE_BACKOFF="0.01")
        cls.bridge = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/__bridge/status" % cls.bridge_port,
                    timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.bridge, cls.sidecar):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        cls.upstream.stop()

    def test_a_chat_only_upstream_serves_a_codex_request(self):
        body = json.dumps({"model": MODEL, "stream": True,
                           "instructions": "be nice",
                           "input": [{"role": "user",
                                      "content": [{"type": "input_text", "text": "hi"}]}]})
        req = urllib.request.Request(
            "http://127.0.0.1:%d/p/sidecar-deepseek/responses" % self.bridge_port,
            data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as fh:
            answer = fh.read()
        self.assertIn(b"response.output_text.delta", answer)
        self.assertIn(b"hello from deepseek-chat", answer)
        # the client's model name was rewritten for the chat-only upstream
        self.assertIn(UPSTREAM_MODEL, self.upstream.models)

    def test_status_shows_the_fixed_price(self):
        snap = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/__bridge/status" % self.bridge_port, timeout=5).read())
        row = snap["routes"][0]
        self.assertTrue(row["fixed_price"])
        self.assertEqual(row["price_per_m"], 0.28)


if __name__ == "__main__":
    unittest.main(verbosity=2)
