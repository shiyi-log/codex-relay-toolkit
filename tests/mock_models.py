#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mock relay for testing model adaptation.

GET  /models     -> only serves the models in MODELS
POST /responses  -> records the "model" it received, then fails (so the bridge
                    keeps rotating and we can see every attempt)
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "19500"))
OUT = os.environ.get("OUT", "/tmp/mock_models.log")
MODELS = os.environ.get("MODELS", "gpt-6-sol,gpt-5.6-sol").split(",")
FAIL = os.environ.get("FAIL", "1") == "1"
BAD = b'{"error":{"message":"Upstream request failed","type":"upstream_error"}}'
OK = (b"event: response.completed\n"
      b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
        else:
            body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            model = json.loads(raw.decode("utf-8")).get("model")
        except Exception:
            model = "?"
        with open(OUT, "a") as fh:
            fh.write("%s %s\n" % (self.path, model))
        if FAIL:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BAD)))
            self.end_headers()
            self.wfile.write(BAD)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(OK)))
            self.end_headers()
            self.wfile.write(OK)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
