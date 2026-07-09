from claude_code_notify import ratelimit as rl


def test_no_marker_allows(tmp_path):
    assert rl.should_send(str(tmp_path / "m"), 120, now=1000.0) is True


def test_within_threshold_blocks(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1050.0) is False


def test_after_threshold_allows(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1121.0) is True


def test_exactly_at_threshold_allows(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1120.0) is True


def test_corrupt_marker_allows(tmp_path):
    marker = tmp_path / "m"
    marker.write_text("garbage")
    assert rl.should_send(str(marker), 120, now=1000.0) is True
