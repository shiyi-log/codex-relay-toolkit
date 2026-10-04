#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mock upstream that records the auth headers it received and always 400s."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "19330"))
OUT = os.environ.get("OUT", "/tmp/echo_headers.log")
BAD = b'{"error":{"message":"Upstream request failed","type":"upstream_error"}}'


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with open(OUT, "a") as fh:
            fh.write(json.dumps({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "account": self.headers.get("chatgpt-account-id"),
            }) + "\n")
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BAD)))
        self.end_headers()
        self.wfile.write(BAD)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
