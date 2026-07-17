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


def _mk(dirpath, chat_id=None, bot_token=None, mute=False):
    return routing.Route(dir=os.path.realpath(dirpath), chat_id=chat_id,
                         bot_token=bot_token, mute=mute)


def test_resolve_exact_match():
    res = routing.resolve("/home/me/work", [_mk("/home/me/work", chat_id="111")],
                          "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "111"
    assert res.bot_token == "G:tok"  # no per-route bot -> global
    assert res.matched_dir == os.path.realpath("/home/me/work")


def test_resolve_subtree_inheritance():
    res = routing.resolve("/home/me/work/proj/sub",
                          [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "111"


def test_resolve_deeper_overrides_shallower():
    routes = [_mk("/home/me/work", chat_id="111"),
              _mk("/home/me/work/acme", chat_id="222")]
    res = routing.resolve("/home/me/work/acme/sub", routes, "G:tok", "999")
    assert res.chat_id == "222"


def test_resolve_segment_boundary_no_false_match():
    res = routing.resolve("/home/me/workspace",
                          [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "999"  # global, not 111
    assert res.matched_dir is None


def test_resolve_no_match_uses_global():
    res = routing.resolve("/tmp/other", [], "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "999"
    assert res.bot_token == "G:tok"
    assert res.matched_dir is None


def test_resolve_muted_subtree():
    res = routing.resolve("/home/me/scratch/x",
                          [_mk("/home/me/scratch", chat_id="888", bot_token="M:tok", mute=True)], "G:tok", "999")
    assert res.muted is True
    assert res.matched_dir == os.path.realpath("/home/me/scratch")
    # Verify that muted routes discard their own chat_id/bot_token, not fall back to route's values
    assert res.chat_id is None
    assert res.bot_token is None


def test_resolve_muted_parent_normal_deeper_child():
    routes = [_mk("/home/me/scratch", mute=True),
              _mk("/home/me/scratch/keep", chat_id="333")]
    res = routing.resolve("/home/me/scratch/keep/x", routes, "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "333"


def test_resolve_normal_parent_muted_deeper_child():
    routes = [_mk("/home/me/work", chat_id="111"),
              _mk("/home/me/work/secret", mute=True)]
    res = routing.resolve("/home/me/work/secret/x", routes, "G:tok", "999")
    assert res.muted is True


def test_resolve_per_route_bot_override():
    res = routing.resolve("/home/me/work",
                          [_mk("/home/me/work", chat_id="111", bot_token="777:xyz")],
                          "G:tok", "999")
    assert res.bot_token == "777:xyz"
    assert res.chat_id == "111"


def test_resolve_empty_cwd_uses_global():
    res = routing.resolve("", [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "999"
    assert res.matched_dir is None


def test_resolve_normalizes_paths(tmp_path):
    work = tmp_path / "work"
    (work / "proj").mkdir(parents=True)
    routes = [_mk(str(work), chat_id="111")]
    res = routing.resolve(str(work / "proj" / ".."), routes, "G:tok", "999")
    assert res.chat_id == "111"
