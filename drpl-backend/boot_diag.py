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

HEADER = (
    "DRPL backend: still starting, or failed to start.\n"
    "This is the container's boot log.\n"
)
# Built as its own expression on purpose. Adjacent string literals concatenate
# before `*` binds, so writing the banner as `"a\n" "b\n" "=" * 60` repeats the
# whole banner sixty times instead of the "=" -- which is what the first
# deploy of this file did, burying the traceback it exists to show.
SEPARATOR = "=" * 60


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as exc:
            body = "(no boot log yet: %s)" % exc
        payload = (HEADER + SEPARATOR + "\n" + body).encode("utf-8", "replace")
        self.send_response(503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Every method, not just GET. BaseHTTPRequestHandler answers an
    # unimplemented verb with a 501 HTML page, and the platform may route a
    # POST to an instance that is still booting -- which reached the browser as
    # "Error response / Unsupported method ('POST')" instead of anything the
    # SPA could handle. A JSON 503 with Retry-After is at least an answer the
    # client understands.
    def do_POST(self):
        self._busy()

    def do_PUT(self):
        self._busy()

    def do_PATCH(self):
        self._busy()

    def do_DELETE(self):
        self._busy()

    def do_HEAD(self):
        self.send_response(503)
        self.send_header("Retry-After", "30")
        self.end_headers()

    def _busy(self):
        body = (
            b'{"detail":"The service is starting up. Please retry in a moment.",'
            b'"status":"starting"}'
        )
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "30")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
