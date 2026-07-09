import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from claude_code_notify import notifier
from claude_code_notify.config import Config


def _cfg(api_base, token="123:secret", chat_id="999"):
    from pathlib import Path
    return Config(bot_token=token, chat_id=chat_id, ratelimit_seconds=120,
                  api_base=api_base, debug=False, base_dir=Path("/tmp"))


def test_scrub_removes_token():
    assert notifier.scrub("url bot123:secret/x", "123:secret") == "url bot***/x"


def test_scrub_noop_without_token():
    assert notifier.scrub("hello", "") == "hello"


def test_build_message_finished_with_title():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task")
    assert msg == "Claude Code finished | My Task | /home/x | 09/07/2026 10:00:00"


def test_build_message_omits_absent_title():
    msg = notifier.build_message("error", "/home/x", "09/07/2026 10:00:00", None)
    assert msg == "Claude Code stopped with error | /home/x | 09/07/2026 10:00:00"


def test_build_message_needs_input():
    msg = notifier.build_message("needs-input", "/home/x", "T")
    assert msg.startswith("Claude Code needs your input |")


class _Capture(BaseHTTPRequestHandler):
    received = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        _Capture.received = {"path": self.path, "form": parse_qs(body)}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def test_send_posts_expected_payload():
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        cfg = _cfg(f"http://{host}:{port}")
        notifier.send(cfg, "hello world")
        assert _Capture.received["path"] == "/bot123:secret/sendMessage"
        assert _Capture.received["form"]["chat_id"] == ["999"]
        assert _Capture.received["form"]["text"] == ["hello world"]
    finally:
        thread.join(timeout=5)
        server.server_close()


def test_send_failure_scrubs_token():
    # Nothing is listening on this port → connection refused.
    cfg = _cfg("http://127.0.0.1:1")
    with pytest.raises(notifier.NotifierError) as exc:
        notifier.send(cfg, "hi")
    assert "123:secret" not in str(exc.value)
