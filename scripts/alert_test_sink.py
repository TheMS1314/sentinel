"""Isolated CI TLS notification receiver; no customer traffic or payload logging."""

import hashlib
import hmac
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

seen = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16384:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        expected = hmac.new(
            os.environ["SINK_SECRET"].encode(), raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(
            expected, self.headers.get("X-Sentinel-Signature", "")
        ):
            self.send_error(401)
            return
        event = json.loads(raw)
        if event["event_id"] != self.headers.get("X-Sentinel-Event-Id"):
            self.send_error(400)
            return
        seen.add(event["event_id"])
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"unique_events": len(seen)}).encode())


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8443), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/certs/server.crt", "/certs/server.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
