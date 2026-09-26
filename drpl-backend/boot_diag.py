"""Serve the boot log over $PORT so a failed start is readable from outside.

Vercel's runtime-log API answers 403 for this project, so a container that
cannot start is otherwise completely opaque: every request returns
FUNCTION_INVOCATION_FAILED and the traceback that caused it never leaves the
box. This binds the port immediately -- which also keeps the platform from
killing the container while the real startup is still working -- and serves
whatever has been written to the boot log so far.

It is replaced by uvicorn the moment the app imports cleanly.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/boot.log"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as exc:
            body = f"(no boot log yet: {exc})"
        payload = (
            "DRPL backend: still starting, or failed to start.\n"
            "This is the container's boot log.\n"
            "=" * 60 + "\n" + body
        ).encode("utf-8", "replace")
        self.send_response(503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
