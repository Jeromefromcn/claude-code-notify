from claude_code_notify import __main__ as m


def _write_cfg(tmp_path, extra=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n" + extra
    )


def test_check_route_matched(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={work}\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(work / "sub")]) == 0
    out = capsys.readouterr().out
    assert "chat_id: 111" in out
    assert "global default bot" in out
    assert "123:abc" not in out


def test_check_route_bot_override(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={work}\nROUTE_1_CHAT_ID=111\nROUTE_1_BOT_TOKEN=777:xyz\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(work)]) == 0
    out = capsys.readouterr().out
    assert "per-route override" in out
    assert "777:xyz" not in out


def test_check_route_muted(tmp_path, monkeypatch, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={scratch}\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(scratch)]) == 0
    assert "MUTED" in capsys.readouterr().out


def test_check_route_no_match_uses_global(tmp_path, monkeypatch, capsys):
    _write_cfg(tmp_path)
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "chat_id: 999" in out
    assert "none — using global default" in out


def test_check_route_config_error_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))  # no config.env
    assert m.main(["prog", "--check-route", str(tmp_path)]) == 1
