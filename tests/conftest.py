"""Shared fixtures: a throwaway git repository, and a job that works on it.

Every test that touches a worktree gets its own repository in `tmp_path`. That
is not fastidiousness -- this package's job is to run other people's code
against a checkout, and a test suite that operated on the developer's own
repository would be the exact failure the tool is built to prevent, committed
by the tool's own tests.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

APP = "def add(a, b):\n    return a + b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

#: Shell prompts that need no backslashes, so a spec written in a test reads
#: the same as one written by a user.
APPEND_SUB = (
    """python -c "import io; io.open('app.py','a').write("""
    """chr(10) + 'def sub(a, b): return a - b' + chr(10))" """
)
BREAK_TESTS = (
    """python -c "import io; io.open('app.py','w').write("""
    """'def add(a, b): return a * b' + chr(10))" """
)
TOUCH_CI = (
    """python -c "import io, os; os.makedirs('.github', exist_ok=True); """
    """io.open('.github/ci.yml','a').write('# sneaky' + chr(10))" """
)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A git repository with a passing test suite, and nothing else."""
    root = str(tmp_path / "project")
    os.makedirs(root)
    git("init", "-q", cwd=root)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
    git("config", "user.email", "test@example.com", cwd=root)
    git("config", "user.name", "Test", cwd=root)
    for name, body in (("app.py", APP), ("test_app.py", TEST), (".gitignore", ".runproof/\n")):
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "initial", cwd=root)
    return root


def spec_text(name, prompt, *, attempts=1, extra_checks=""):
    return (
        f"name: {name}\n"
        "prompt: |\n"
        f"  {prompt}\n"
        "adapter: shell\n"
        f"attempts: {attempts}\n"
        "checks:\n"
        "  - run: python -m pytest -q\n"
        "  - changed_files: {max: 3}\n"
        f"{extra_checks}"
    )
