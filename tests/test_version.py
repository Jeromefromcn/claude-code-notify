import subprocess
import sys

import claude_code_notify


def test_version_string():
    assert claude_code_notify.__version__ == "0.1.0"


def test_version_cli():
    out = subprocess.run(
        [sys.executable, "-m", "claude_code_notify", "--version"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "0.1.0"
