"""Printing results so that a rejection is more informative than a pass.

Most tools put their effort into the success path. This one inverts that: a
green run needs one line, and a rejected one needs to say what failed, by how
much, and where to go and look. If you have to open a log after reading a
runproof report, the report did not do its job.

Two carried-over rules, both learned the hard way in a sibling project.

**Drawing characters are asked for, not assumed.** `print` substitutes `?` for
anything the terminal cannot encode, silently, so a report can look like it
worked while being rows of noise. The first Windows console this family of
tools met was cp936. So the stream is asked what it can encode and there is an
ASCII fallback.

**Counting is in English.** "1 attempts" and "1 schedules" both shipped
elsewhere before being noticed.
"""

from __future__ import annotations

import os
import sys
import time

__all__ = ["render_doctor", "render_run", "render_status", "supports_colour", "supports_unicode"]

_ANSI = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}

_GLYPHS = {
    True: {"pass": "✓", "fail": "✗", "dot": "·", "arrow": "→"},
    False: {"pass": "PASS", "fail": "FAIL", "dot": "|", "arrow": "->"},
}


def supports_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def supports_unicode(stream=None) -> bool:
    """Ask the stream, rather than guess from the platform.

    The same Windows machine answers differently depending on its code page,
    and the failure is silent.
    """
    encoding = getattr(stream or sys.stdout, "encoding", None) or ""
    try:
        "".join(_GLYPHS[True].values()).encode(encoding)
    except (LookupError, UnicodeEncodeError, TypeError):
        return False
    return True


class Ink:
    def __init__(self, colour: bool = True, unicode_ok: bool = True):
        self.colour = colour
        self.glyph = _GLYPHS[bool(unicode_ok)]

    def __call__(self, text: str, *names: str) -> str:
        if not self.colour or not names:
            return text
        return "".join(_ANSI[n] for n in names) + text + _ANSI["reset"]

    def mark(self, ok: bool) -> str:
        key = "pass" if ok else "fail"
        return self(self.glyph[key], "green" if ok else "red")


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _ago(when: float | None, now: float | None = None) -> str:
    if when is None:
        return "never"
    seconds = (time.time() if now is None else now) - when
    if seconds < 90:
        return plural(round(seconds), "second") + " ago"
    if seconds < 5400:
        return plural(round(seconds / 60), "minute") + " ago"
    return plural(round(seconds / 3600), "hour") + " ago"


def render_run(outcome, *, colour: bool = True, unicode_ok: bool = True) -> str:
    """One run. Short when it passed, detailed when it did not."""
    ink = Ink(colour, unicode_ok)
    lines = ["", ink(f"runproof {outcome.job}", "bold") + ink(f"  run {outcome.run_id}", "dim")]

    verdict = ink("PASSED", "bold", "green") if outcome.passed else ink("FAILED", "bold", "red")
    lines.append(f"  {verdict}  {outcome.summary()}")

    for attempt in outcome.attempts:
        head = (
            f"  {ink.mark(attempt.passed)} attempt {attempt.ordinal}  {attempt.wall_seconds:.1f}s"
        )
        if attempt.branch:
            head += ink(f"  {attempt.branch}", "cyan")
        lines.append(head)

        if attempt.error:
            lines.append(ink(f"      error: {attempt.error}", "red"))
            continue
        if not attempt.verdict:
            continue

        # The failing checks first and in full. A passing run does not need
        # its evidence recited; a rejected one is the entire point.
        for check in attempt.verdict.results:
            if check.passed and not _verbose_pass(outcome):
                continue
            detail = check.detail if len(check.detail) < 300 else check.detail[:300] + " ..."
            lines.append(f"      {ink.mark(check.passed)} {check.kind}")
            lines.append(ink(f"         {detail}", "dim"))
        if attempt.verdict.diff:
            diff = attempt.verdict.diff
            lines.append(
                ink(
                    f"      {plural(diff.files_changed, 'file')} changed, "
                    f"{plural(diff.lines, 'line')}",
                    "dim",
                )
            )

    if not outcome.passed:
        lines.append("")
        lines.append(ink("  Nothing was merged. Each attempt is on its own branch above,", "dim"))
        lines.append(ink("  still checked out-able, so you can see what it did.", "dim"))
    lines.append("")
    return "\n".join(lines)


def _verbose_pass(outcome) -> bool:
    """Recite every check only when the run failed somewhere."""
    return not outcome.passed


def render_status(runs, *, colour: bool = True, unicode_ok: bool = True) -> str:
    ink = Ink(colour, unicode_ok)
    if not runs:
        return "\nNo runs recorded yet.\n"
    lines = ["", ink("recent runs", "bold")]
    for run in runs:
        state = run.state
        colour_name = {"passed": "green", "failed": "red", "error": "red"}.get(state, "yellow")
        flag = ink(" STALE", "yellow") if run.stale() else ""
        lines.append(
            f"  {run.id:>4}  {ink(state.ljust(8), colour_name)} {run.job[:24]:<24}"
            f" {ink(run.trigger, 'dim'):<10} {ink(_ago(run.started_at), 'dim')}{flag}"
        )
    lines.append("")
    return "\n".join(lines)


def render_doctor(health, *, colour: bool = True, unicode_ok: bool = True) -> str:
    """The answer to 'did the overnight work run, and if not, why not?'"""
    ink = Ink(colour, unicode_ok)
    lines = ["", ink("runproof doctor", "bold"), ""]

    lines.append(f"  {ink.mark(health.alive)} {health.summary()}")
    lines.append(ink(f"     last tick {_ago(health.last_tick)}", "dim"))

    if health.overdue:
        lines.append("")
        lines.append(ink("  overdue schedules", "bold"))
        for schedule in health.overdue:
            late = schedule.overdue_by()
            lines.append(
                f"    {ink.mark(False)} {schedule.job[:28]:<28} "
                f"late by {plural(round(late / 60), 'minute')}"
            )

    if health.stale_runs:
        lines.append("")
        lines.append(ink("  runs that claim to be alive but stopped saying so", "bold"))
        for run in health.stale_runs:
            lines.append(
                f"    {ink.mark(False)} run {run.id} {run.job[:24]:<24} "
                f"last heartbeat {_ago(run.heartbeat)}"
            )
        lines.append(ink("     These are dispatches whose process died mid-run.", "dim"))

    if health.alive and not health.overdue and not health.stale_runs:
        lines.append(ink("     Nothing is late and no run has gone quiet.", "dim"))
    lines.append("")
    return "\n".join(lines)
