import pytest

from claude_code_notify import config as cfg


def test_parse_env_file_basic():
    text = "# comment\nTELEGRAM_BOT_TOKEN=123:abc\n\nTELEGRAM_CHAT_ID=999\n"
    d = cfg.parse_env_file(text)
    assert d == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "999"}


def test_parse_env_file_strips_quotes():
    assert cfg.parse_env_file('K="v"\n') == {"K": "v"}
    assert cfg.parse_env_file("K='v'\n") == {"K": "v"}


def test_parse_env_file_strips_export_prefix():
    assert cfg.parse_env_file("export FOO=bar\n") == {"FOO": "bar"}


def test_parse_env_file_export_with_extra_spaces():
    assert cfg.parse_env_file("export   FOO=bar\n") == {"FOO": "bar"}


def test_parse_env_file_export_with_quoted_value():
    assert cfg.parse_env_file('export FOO="bar baz"\n') == {"FOO": "bar baz"}


def test_parse_env_file_export_mixed_with_plain_lines():
    text = "export TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    assert cfg.parse_env_file(text) == {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_ID": "999",
    }


def test_parse_env_file_does_not_mistake_export_like_key_for_prefix():
    # A real key that merely starts with the substring "export" (no space
    # after it) must be parsed as-is, not have its start chopped off.
    assert cfg.parse_env_file("exported_flag=1\n") == {"exported_flag": "1"}


def test_parse_env_file_export_multi_assignment_line_not_supported():
    # Bash allows `export A=1 B=2` on one line; parse_env_file is a
    # deliberately simple line parser (not a shell parser) and only
    # supports one `export KEY=value` per line. This documents that
    # known, intentional boundary rather than silently doing the wrong
    # thing unnoticed.
    assert cfg.parse_env_file("export A=1 B=2\n") == {"A": "1 B=2"}


def test_load_from_file(tmp_path):
    base = tmp_path
    (base / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=base)
    assert c.bot_token == "123:abc"
    assert c.chat_id == "999"
    assert c.ratelimit_seconds == 120
    assert c.api_base == "https://api.telegram.org"
    assert c.debug is False
    assert c.base_dir == base


def test_load_from_file_with_export_prefix(tmp_path):
    (tmp_path / "config.env").write_text(
        "export TELEGRAM_BOT_TOKEN=123:abc\nexport TELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.bot_token == "123:abc"
    assert c.chat_id == "999"


def test_env_overrides_file(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=fromfile\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(
        environ={"TELEGRAM_BOT_TOKEN": "fromenv", "NOTIFY_DEBUG": "true",
                 "NOTIFY_RATELIMIT_SECONDS": "5"},
        base=tmp_path,
    )
    assert c.bot_token == "fromenv"
    assert c.debug is True
    assert c.ratelimit_seconds == 5


def test_missing_required_raises(tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.load(environ={}, base=tmp_path)


def test_default_base_dir_env_override():
    p = cfg.default_base_dir(environ={"CLAUDE_NOTIFY_HOME": "/tmp/xyz"})
    assert str(p) == "/tmp/xyz"


def test_path_helpers(tmp_path):
    assert cfg.config_path(tmp_path).name == "config.env"
    assert cfg.state_path(tmp_path, "sess1").name == "sess1.state.json"
    assert cfg.marker_path(tmp_path, "sess1").name == "sess1.marker"
    assert cfg.debug_log_path(tmp_path).name == "debug.log"


def test_load_parses_routes(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "ROUTE_1_DIR=/home/me/work\nROUTE_1_CHAT_ID=111\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert len(c.routes) == 1
    assert c.routes[0].chat_id == "111"


def test_load_no_routes_gives_empty_list(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.routes == []


def test_load_usage_limit_defaults(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.usage_limit is False
    assert c.usage_limit_reset is True


def test_load_usage_limit_from_file(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "NOTIFY_USAGE_LIMIT=true\nNOTIFY_USAGE_LIMIT_RESET=false\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.usage_limit is True
    assert c.usage_limit_reset is False


def test_env_overrides_usage_limit(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={"NOTIFY_USAGE_LIMIT": "1"}, base=tmp_path)
    assert c.usage_limit is True
