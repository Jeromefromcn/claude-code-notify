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


def test_build_message_with_duration_and_title():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task", "3m12s")
    assert msg == "Claude Code finished | 3m12s | My Task | /home/x | 09/07/2026 10:00:00"


def test_build_message_with_duration_no_title():
    msg = notifier.build_message("error", "/home/x", "09/07/2026 10:00:00", None, "45s")
    assert msg == "Claude Code stopped with error | 45s | /home/x | 09/07/2026 10:00:00"


def test_build_message_omits_absent_duration():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task")
    assert msg == "Claude Code finished | My Task | /home/x | 09/07/2026 10:00:00"


def test_build_message_usage_limit():
    assert notifier.build_message(
        "usage-limit", "/w", "WHEN",
        title="You've hit your session limit · resets 9pm") == (
        "Claude Code usage limit reached | "
        "You've hit your session limit · resets 9pm | /w | WHEN")


def test_build_message_usage_limit_reset_omits_empty_cwd():
    assert notifier.build_message("usage-limit-reset", "", "WHEN") == (
        "Claude Code usage limit reset | WHEN")


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


class _NotFound(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_send_failure_clears_exception_context_leak():
    # A 404 response makes urlopen raise urllib.error.HTTPError, a URLError
    # subclass whose `.url` attribute holds the *unredacted* request URL
    # (including the bot token) even though str(exc) does not contain it.
    # This reproduces Telegram returning 401/404 for a bad token/chat_id.
    server = HTTPServer(("127.0.0.1", 0), _NotFound)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        cfg = _cfg(f"http://{host}:{port}")
        with pytest.raises(notifier.NotifierError) as exc:
            notifier.send(cfg, "hi")
        assert "123:secret" not in str(exc.value)
        # The gap: `raise ... from None` only suppresses default traceback
        # display, it does NOT clear __context__, so the original
        # HTTPError (with the token-bearing .url) stays reachable to any
        # code that walks the chain explicitly.
        assert exc.value.__context__ is None
        assert exc.value.__cause__ is None
    finally:
        thread.join(timeout=5)
        server.server_close()
