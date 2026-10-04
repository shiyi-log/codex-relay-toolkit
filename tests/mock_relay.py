#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mock relay for testing the retry bridge.

Fails the first $FAIL requests with the exact 400 envelope the real relays
return, then answers 200 with a small SSE body.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAIL = int(os.environ.get("FAIL", "2"))
PORT = int(os.environ.get("PORT", "19001"))
COUNT = {"n": 0}
LOCK = threading.Lock()
MODE = os.environ.get("MODE", "upstream400")
BAD = b'{"error":{"message":"Upstream request failed","type":"upstream_error"}}'
OTHER = b'{"error":{"message":"Model \\"gpt-6-sol\\" is not supported","type":"model_not_found"}}'
OK = (b"event: response.output_text.delta\n"
      b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
      b"event: response.completed\n"
      b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n')


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with LOCK:
            COUNT["n"] += 1
            n = COUNT["n"]
        with open(os.environ.get("HITLOG", "/tmp/mock_relay.hits"), "a") as fh:
            fh.write("%d %s\n" % (n, self.path))
        if MODE == "other400":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(OTHER)))
            self.end_headers()
            self.wfile.write(OTHER)
        elif n <= FAIL:
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
