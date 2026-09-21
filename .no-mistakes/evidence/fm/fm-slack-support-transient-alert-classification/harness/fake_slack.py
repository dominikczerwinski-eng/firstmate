#!/usr/bin/env python3
"""A stand-in Slack Web API endpoint used to drive bin/fm-slack-support.sh live.

Behaviour is switched at runtime by writing a mode word into <mode-file>, so a
single long-lived endpoint can serve a whole operator session: healthy polls,
a dropped connection, an HTTP 429, a truncated body, a missing_scope refusal.
Every request and every chat.postMessage body is appended to the log files so
the transcript shows exactly what the poller did and what the reporter saw.
"""
import json
import os
import pathlib
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

mode_file = pathlib.Path(sys.argv[1])
port_file = pathlib.Path(sys.argv[2])
req_log = pathlib.Path(sys.argv[3])
post_log = pathlib.Path(sys.argv[4])
store = pathlib.Path(sys.argv[5])  # JSON: channel/dm message fixtures

REPORTER = {"id": "U-martyna", "name": "martyna.nowak", "real_name": "Martyna Nowak"}
BOT = {"id": "U-fenek", "name": "fenek", "is_bot": True}


def mode():
    return mode_file.read_text(encoding="utf-8").strip()


def fixtures():
    return json.loads(store.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _json(self, body):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _trace(self, method, detail=""):
        with req_log.open("a", encoding="utf-8") as fh:
            fh.write((method + " " + detail).strip() + "\n")

    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)
        method = split.path.rsplit("/", 1)[-1]
        channel = (query.get("channel") or [""])[0]
        self._trace(method, channel)
        m = mode()
        fx = fixtures()

        if method == "conversations.list" and query.get("types") == ["im"]:
            if m == "probe_timeout":
                self.close_connection = True
                self.connection.close()
                return
            if m == "probe_missing_scope":
                return self._json({"ok": False, "error": "missing_scope"})
            return self._json({"ok": True, "channels": [{"id": "D-martyna"}, {"id": "D-kasia"}]})

        if method == "conversations.list":
            return self._json({"ok": True, "channels": [{"id": "C-support", "name": "support"}]})

        if method == "users.list":
            return self._json({"ok": True, "members": [REPORTER, BOT]})

        if method == "auth.test":
            return self._json({"ok": True, "user_id": "U-fenek"})

        if method == "conversations.replies":
            return self._json({"ok": True, "messages": []})

        if method == "conversations.history":
            if channel == "D-martyna":
                if m == "dm_429":
                    self.send_error(429, "slow down")
                    return
                if m == "dm_truncated":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "4096")
                    self.end_headers()
                    self.wfile.write(b'{"ok": true, "messages": [')
                    self.close_connection = True
                    return
                if m == "dm_invalid_args":
                    return self._json({"ok": False, "error": "invalid_arguments"})
            return self._json({"ok": True, "messages": fx.get(channel, [])})

        return self._json({"ok": False, "error": "unexpected_method"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        method = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        self._trace(method, str(payload.get("channel", "")))
        if method == "chat.postMessage":
            with post_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "channel": payload.get("channel"),
                            "thread_ts": payload.get("thread_ts"),
                            "text": payload.get("text"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            return self._json({"ok": True, "ts": "9999.0001"})
        return self._json({"ok": False, "error": "unexpected_method"})


server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
port_file.write_text(str(server.server_address[1]), encoding="utf-8")
threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Event().wait(float(os.environ.get("FAKE_SLACK_MAX_SECONDS", "600")))
