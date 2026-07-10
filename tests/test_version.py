import os
import re
import subprocess
import sys

import claude_code_notify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_version_string():
    assert claude_code_notify.__version__ == "0.1.1"


def test_version_cli():
    out = subprocess.run(
        [sys.executable, "-m", "claude_code_notify", "--version"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "0.1.1"


def test_pyproject_version_matches_package_version():
    # Guards against releasing a tag/package where __version__ and
    # pyproject.toml's [project].version have drifted apart. Regex rather
    # than tomllib, since tomllib is 3.11+ and this project supports 3.8.
    pyproject = open(os.path.join(REPO, "pyproject.toml")).read()
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert match, "no version = \"...\" line found in pyproject.toml"
    assert match.group(1) == claude_code_notify.__version__
