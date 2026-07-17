import os

from claude_code_notify import routing


def test_parse_routes_basic():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/home/me/work",
        "ROUTE_1_CHAT_ID": "111",
    })
    assert len(routes) == 1
    r = routes[0]
    assert r.dir == os.path.realpath("/home/me/work")
    assert r.chat_id == "111"
    assert r.bot_token is None
    assert r.mute is False


def test_parse_routes_bot_token_override_and_mute():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111", "ROUTE_1_BOT_TOKEN": "777:xyz",
        "ROUTE_2_DIR": "/b", "ROUTE_2_MUTE": "true",
    })
    by_dir = {r.dir: r for r in routes}
    a = by_dir[os.path.realpath("/a")]
    b = by_dir[os.path.realpath("/b")]
    assert a.bot_token == "777:xyz"
    assert a.mute is False
    assert b.mute is True
    assert b.chat_id is None


def test_parse_routes_missing_dir_skipped():
    assert routing.parse_routes({"ROUTE_1_CHAT_ID": "111"}) == []


def test_parse_routes_no_chat_and_not_muted_skipped():
    assert routing.parse_routes({"ROUTE_1_DIR": "/a"}) == []


def test_parse_routes_duplicate_dir_last_index_wins():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111",
        "ROUTE_2_DIR": "/a", "ROUTE_2_CHAT_ID": "222",
    })
    assert len(routes) == 1
    assert routes[0].chat_id == "222"


def test_parse_routes_ignores_non_route_and_unknown_keys():
    routes = routing.parse_routes({
        "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "9",
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111",
        "ROUTE_1_UNKNOWN": "ignore-me",
    })
    assert len(routes) == 1
    assert routes[0].chat_id == "111"


def test_parse_routes_non_contiguous_indices():
    routes = routing.parse_routes({
        "ROUTE_5_DIR": "/a", "ROUTE_5_CHAT_ID": "111",
        "ROUTE_42_DIR": "/b", "ROUTE_42_CHAT_ID": "222",
    })
    assert {r.chat_id for r in routes} == {"111", "222"}


def test_parse_routes_mute_truthiness():
    for val in ("true", "1", "yes", "on", "TRUE"):
        routes = routing.parse_routes({"ROUTE_1_DIR": "/a", "ROUTE_1_MUTE": val})
        assert routes and routes[0].mute is True
    # A non-truthy MUTE with no CHAT_ID is skipped (not muted, nowhere to send).
    assert routing.parse_routes({"ROUTE_1_DIR": "/a", "ROUTE_1_MUTE": "false"}) == []
