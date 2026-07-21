from pathlib import Path

from claude_code_notify import broadcast
from claude_code_notify.config import Config
from claude_code_notify.routing import Route


def _cfg(routes):
    return Config(bot_token="G:tok", chat_id="999", ratelimit_seconds=120,
                  api_base="http://127.0.0.1:1", debug=False, base_dir=Path("/tmp"),
                  routes=routes)


def test_destinations_global_only():
    assert broadcast.destinations(_cfg([])) == [("G:tok", "999")]


def test_destinations_include_routes_and_dedupe():
    routes = [
        Route(dir="/a", chat_id="111", bot_token=None, mute=False),      # uses global bot
        Route(dir="/b", chat_id="222", bot_token="B:tok", mute=False),   # own bot
        Route(dir="/c", chat_id="999", bot_token="G:tok", mute=False),   # dup of global
    ]
    got = broadcast.destinations(_cfg(routes))
    assert got == [("G:tok", "999"), ("G:tok", "111"), ("B:tok", "222")]


def test_destinations_skip_route_without_chat():
    routes = [Route(dir="/m", chat_id=None, bot_token=None, mute=True)]  # muted, no chat
    assert broadcast.destinations(_cfg(routes)) == [("G:tok", "999")]


def test_destinations_muted_route_with_chat_still_included():
    # Mute is not consulted here; a muted route that carries a chat_id is a
    # configured destination and receives the account-global broadcast.
    routes = [Route(dir="/m", chat_id="333", bot_token=None, mute=True)]
    assert broadcast.destinations(_cfg(routes)) == [("G:tok", "999"), ("G:tok", "333")]


def test_send_all_hits_each_destination_and_survives_failures():
    routes = [Route(dir="/a", chat_id="111", bot_token=None, mute=False)]
    seen = []

    def fake_send(cfg, text):
        if cfg.chat_id == "999":
            raise Exception("boom")   # one dead destination
        seen.append((cfg.bot_token, cfg.chat_id, text))

    sent = broadcast.send_all(_cfg(routes), "hello", send=fake_send)
    assert seen == [("G:tok", "111", "hello")]
    assert sent == 1  # the failing destination is not counted, others proceed
