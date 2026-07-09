import pytest

from claude_code_notify import config as cfg


def test_parse_env_file_basic():
    text = "# comment\nTELEGRAM_BOT_TOKEN=123:abc\n\nTELEGRAM_CHAT_ID=999\n"
    d = cfg.parse_env_file(text)
    assert d == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "999"}


def test_parse_env_file_strips_quotes():
    assert cfg.parse_env_file('K="v"\n') == {"K": "v"}
    assert cfg.parse_env_file("K='v'\n") == {"K": "v"}


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
