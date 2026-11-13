#!/usr/bin/env python3
"""Dev static server for ui/ that disables browser caching.

Plain `python -m http.server` sends Last-Modified but no Cache-Control, so
browsers apply heuristic caching and can keep running a stale app.js after the
file changed on disk (symptom: errors like "ReferenceError: state is not
defined" that no longer exist in the current code). This wrapper sends
`Cache-Control: no-store` on every response.

Usage: python scripts/dev_ui_server.py [port] [directory]
"""

import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
DIRECTORY = sys.argv[2] if len(sys.argv) > 2 else "ui"


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):  # quieter output
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    os.chdir(DIRECTORY)
    handler = functools_compat()
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler) as httpd:
        print(f"serving {os.getcwd()} on http://127.0.0.1:{PORT} (no-cache)")
        httpd.serve_forever()


def functools_compat():
    """Bind DIRECTORY without depending on functools.partial typing quirks."""
    import functools

    return functools.partial(NoCacheHandler, directory=os.getcwd())


if __name__ == "__main__":
    main()
