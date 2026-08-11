"""Security and process-boundary tests for real agent adapters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import runproof.adapters as adapters
from runproof.adapters import AdapterError, ClaudeAdapter, CodexAdapter


def _value_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_claude_defaults_to_a_fixed_noninteractive_tool_surface():
    command = ClaudeAdapter()._command()

    assert _value_after(command, "--permission-mode") == "dontAsk"
    assert _value_after(command, "--tools") == "Read,Glob,Grep,Edit,Write"
    assert _value_after(command, "--allowedTools") == "Read(/**),Edit(/**)"
    assert "--setting-sources=" in command
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command
    assert "--no-chrome" in command
    assert "--disable-slash-commands" in command
    assert "bypassPermissions" not in command


def test_codex_explicitly_uses_the_workspace_write_sandbox():
    command = CodexAdapter()._command()

    assert _value_after(command, "--sandbox") == "workspace-write"
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[-1] == "-", "the untrusted task should be read from stdin"


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows .CMD shim resolution")
def test_claude_prompt_is_piped_and_a_windows_cmd_shim_is_resolved_without_a_shell(
    monkeypatch, tmp_path
):
    captured = {}
    prompt = "edit app.py & echo this must never become shell syntax"
    shim_dir = tmp_path / "tool with spaces"
    shim_dir.mkdir()
    native = shim_dir / "claude.exe"
    native.write_bytes(b"")
    shim = shim_dir / "claude.CMD"
    shim.write_text('@"%~dp0\\claude.exe" %*\r\n', encoding="utf-8")

    monkeypatch.setattr(adapters.shutil, "which", lambda _name: str(shim))

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        payload = json.dumps({"result": "done", "usage": {"input_tokens": 1}})
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)

    result = ClaudeAdapter().run(
        SimpleNamespace(path=str(tmp_path)),
        SimpleNamespace(prompt=prompt),
        timeout=30,
    )

    assert result.ok
    assert captured["command"][0] == str(native)
    assert captured["kwargs"]["input"] == prompt
    assert prompt not in " ".join(captured["command"])


@pytest.mark.skipif(os.name != "nt", reason="executes a Windows .CMD shim fixture")
def test_windows_npm_shim_with_spaces_resolves_without_a_shell(tmp_path):
    shim_dir = tmp_path / "tool with spaces"
    shim_dir.mkdir()
    shim = shim_dir / "agent.cmd"
    shim.write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")

    prefix = adapters._resolve_windows_shim(str(shim))
    command = [
        *prefix,
        "-c",
        "import sys; print(sys.argv[1]); print(sys.stdin.read(), end='')",
        "fixed&not-shell-syntax",
    ]
    result = subprocess.run(
        command,
        input="task text & not shell syntax\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "fixed&not-shell-syntax" in result.stdout
    assert "task text & not shell syntax" in result.stdout


def test_an_unknown_windows_shim_fails_closed_with_the_real_reason(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\nclaude-wrapper %*\r\n", encoding="utf-8")
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: str(shim))

    with pytest.raises(AdapterError, match="cannot safely resolve Windows command shim"):
        ClaudeAdapter().run(
            SimpleNamespace(path=str(tmp_path)),
            SimpleNamespace(prompt="task"),
            timeout=30,
        )
