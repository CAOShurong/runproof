"""Driving whatever is going to edit the worktree.

An adapter's whole job is: given a prepared worktree and a prompt, make edits
and come back. It does not decide whether the edits were any good --
:mod:`runproof.verify` does that, from the diff, with no knowledge of which
adapter produced it. Keeping those apart is what stops a well-behaved agent
from being graded more kindly than a shell script.

**The shell adapter is not a toy.** It exists so the whole pipeline can be
tested end to end with no agent, no API key and no cost, and so that
"run this codemod and prove the tests still pass" is a first-class use rather
than an afterthought. Every test in this package uses it.

**An adapter that exits zero has not succeeded.** It has merely finished. The
exit code says the process ran; the checks say whether the work is acceptable,
and they are the only thing consulted. So `AdapterResult.ok` deliberately means
"produced a result to verify", not "did the right thing", and the naming is
blunt about it because conflating those two is how an agent's confidence ends
up standing in for evidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["AdapterError", "AdapterResult", "get_adapter"]


class AdapterError(RuntimeError):
    """The agent could not be driven at all, so there is nothing to verify."""


SAFE_CLAUDE_TOOLS = "Read,Glob,Grep,Edit,Write"
SAFE_CLAUDE_ALLOW = "Read(/**),Edit(/**)"
CLAUDE_STDIN_INSTRUCTION = (
    "Execute the coding task supplied on standard input. Work only inside the current checkout."
)


def _shim_path(value: str, shim: str) -> str:
    """Expand the two npm spellings for the directory containing a shim."""
    base = str(Path(shim).parent) + os.sep
    value = re.sub(r"%~dp0", lambda _: base, value, flags=re.IGNORECASE)
    value = re.sub(r"%dp0%", lambda _: base, value, flags=re.IGNORECASE)
    return os.path.normpath(os.path.expandvars(value))


def _resolve_windows_shim(shim: str) -> list[str]:
    """Resolve an npm ``.CMD`` shim without sending arguments through a shell.

    A shell fallback would make characters such as ``&`` in an otherwise
    ordinary argument executable. Modern Claude Code shims point directly to a
    native executable; older npm shims point to a JavaScript entry point. Both
    shapes can be resolved to a direct ``CreateProcess`` argv. Unknown shapes
    fail closed instead of guessing at quoting rules.
    """
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise AdapterError(f"cannot read Windows command shim {shim!r}: {error}") from error

    executable_matches = re.findall(r'"([^"\r\n]+\.exe)"', text, flags=re.IGNORECASE)
    script_matches = re.findall(r'"([^"\r\n]+\.(?:c?js|mjs))"', text, flags=re.IGNORECASE)
    scripts = [_shim_path(value, shim) for value in script_matches]
    scripts = [value for value in scripts if os.path.isfile(value)]

    for value in executable_matches:
        executable = _shim_path(value, shim)
        if os.path.isfile(executable):
            if scripts and os.path.basename(executable).lower() in {"node.exe", "node64.exe"}:
                return [executable, scripts[0]]
            return [executable]

    if scripts:
        node = shutil.which("node")
        if node and not node.lower().endswith((".cmd", ".bat")):
            return [node, scripts[0]]

    raise AdapterError(
        f"cannot safely resolve Windows command shim {shim!r}; install a native "
        "CLI executable or a standard npm shim that points to Node"
    )


def _resolve_cli(name: str) -> list[str]:
    executable = shutil.which(name)
    if executable is None:
        raise AdapterError(f"the `{name}` CLI is not on PATH")
    if executable.lower().endswith((".cmd", ".bat")):
        return _resolve_windows_shim(executable)
    return [executable]


@dataclass(frozen=True)
class AdapterResult:
    """What driving the agent produced. Not a judgement about the work."""

    #: The process ran to completion and left the worktree in a state worth
    #: verifying. **Not** a claim that the task was done correctly.
    ok: bool
    #: Whatever the agent said about its own work. Reported to humans,
    #: never consulted by the gate.
    summary: str = ""
    tokens: int | None = None
    cost_usd: float | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary[:2000],
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "detail": self.detail[:2000],
        }


def _tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


class ShellAdapter:
    """Runs the prompt as a shell command inside the worktree.

    Useful in its own right -- codemods, formatters, migration scripts all
    want the same "change the tree, then prove nothing broke" treatment --
    and indispensable for testing, because it makes the entire pipeline
    exercisable without an agent.
    """

    name = "shell"

    def run(self, worktree, job, timeout: int) -> AdapterResult:
        try:
            result = worktree.run(job.prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            return AdapterResult(False, detail=f"command exceeded {timeout}s")
        return AdapterResult(
            ok=result.returncode == 0,
            summary=_tail(result.stdout),
            detail=f"exit {result.returncode}: {_tail(result.stderr or result.stdout, 400)}",
        )


class ClaudeAdapter:
    """Drives the `claude` CLI in headless print mode.

    Flags verified against `claude --help` on 2026-08-04 rather than
    remembered: `-p/--print` for non-interactive, `--output-format json` for a
    single parseable result, `--permission-mode` for how much it may do
    unattended. Guessing a CLI's interface and discovering it at 3am is the
    kind of thing this project is supposed to prevent, not commit.

    The default is deliberately fail-closed: ``dontAsk`` plus a fixed set of
    repository read/edit tools. Anything outside that surface is denied rather
    than prompting an unattended process or inheriting a broad host allowlist.
    A worktree protects the caller's checkout from normal edits, but it is not
    an operating-system sandbox and cannot make bypass mode safe.
    """

    name = "claude"

    def __init__(self, model: str | None = None, permission_mode: str = "dontAsk"):
        self.model = model
        self.permission_mode = permission_mode

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def _command(self) -> list[str]:
        command = [
            "claude",
            "-p",
            CLAUDE_STDIN_INSTRUCTION,
            "--output-format",
            "json",
            "--permission-mode",
            self.permission_mode,
            "--tools",
            SAFE_CLAUDE_TOOLS,
            "--allowedTools",
            SAFE_CLAUDE_ALLOW,
            "--setting-sources=",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--no-chrome",
            "--disable-slash-commands",
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def run(self, worktree, job, timeout: int) -> AdapterResult:
        try:
            command_prefix = _resolve_cli("claude")
        except AdapterError as error:
            raise AdapterError(
                f"{error}. Install a supported Claude Code CLI, or use "
                "`adapter: shell` for a job that does not need an agent."
            ) from error
        logical_command = self._command()
        command = [*command_prefix, *logical_command[1:]]
        try:
            result = subprocess.run(
                command,
                cwd=worktree.path,
                input=job.prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            # Not an error to propagate: a timed-out agent still leaves a
            # worktree, and whatever it managed to write should be verified
            # rather than thrown away unexamined.
            return AdapterResult(False, detail=f"agent exceeded {timeout}s")

        summary, tokens, cost = _parse_claude_json(result.stdout)
        return AdapterResult(
            ok=result.returncode == 0,
            summary=summary or _tail(result.stdout),
            tokens=tokens,
            cost_usd=cost,
            detail=f"exit {result.returncode}: {_tail(result.stderr, 400)}",
        )


def _parse_claude_json(text: str):
    """Pull the summary and usage out of `--output-format json`.

    Tolerant on purpose. The shape of another tool's JSON is not under this
    project's control, so a change in it should cost the *reporting* of tokens,
    never the verdict -- which is computed from the diff and the checks and
    does not consult this at all.
    """
    if not text or not text.strip():
        return "", None, None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _tail(text), None, None
    if isinstance(payload, list):
        payload = payload[-1] if payload else {}
    if not isinstance(payload, dict):
        return _tail(text), None, None

    summary = payload.get("result") or payload.get("text") or ""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    tokens = None
    if usage:
        counted = [
            usage.get(key)
            for key in ("input_tokens", "output_tokens")
            if isinstance(usage.get(key), int)
        ]
        tokens = sum(counted) if counted else None
    cost = payload.get("total_cost_usd") or payload.get("cost_usd")
    return (
        (summary if isinstance(summary, str) else str(summary)),
        tokens,
        (float(cost) if isinstance(cost, (int, float)) else None),
    )


class CodexAdapter:
    """Drives the `codex` CLI, when it is present.

    Deliberately thin. The point of having a third adapter is that the core
    treats them all as "something that edits a worktree", so that comparing
    agents on the same job is a configuration change rather than a fork.
    """

    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def _command(self) -> list[str]:
        return [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-",
        ]

    def run(self, worktree, job, timeout: int) -> AdapterResult:
        command_prefix = _resolve_cli("codex")
        logical_command = self._command()
        command = [*command_prefix, *logical_command[1:]]
        try:
            result = subprocess.run(
                command,
                cwd=worktree.path,
                input=job.prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(False, detail=f"agent exceeded {timeout}s")
        return AdapterResult(
            ok=result.returncode == 0,
            summary=_tail(result.stdout),
            detail=f"exit {result.returncode}: {_tail(result.stderr, 400)}",
        )


_ADAPTERS = {"shell": ShellAdapter, "claude": ClaudeAdapter, "codex": CodexAdapter}


def get_adapter(name: str, **kwargs):
    try:
        return _ADAPTERS[name](**kwargs)
    except KeyError:
        raise AdapterError(
            f"unknown adapter {name!r}. Known: {', '.join(sorted(_ADAPTERS))}"
        ) from None
