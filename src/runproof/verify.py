"""The gate. Whether an attempt counts, and the evidence either way.

Everything else in this package is plumbing for this file. An agent produces a
diff and a confident summary; neither is a verdict. This is where a run becomes
`passed` or does not, and the rule is unromantic: **the verdict is the
conjunction of the declared checks, and nothing else is consulted.** The
agent's own account of its work is evidence for a human reading the report. It
is never an input to the decision.

Two properties are worth more than the checks themselves.

**Detail is quoted, never summarised.** A report that says "tests failed" sends
the reader back to the logs, which is exactly where they were before they
installed anything. So a failing command reports the exit code and the lines
that mattered; a size limit reports the actual number against the limit; a
forbidden path reports *which* path. The rule of thumb: a check's detail should
let you decide what to do next without opening anything else.

**A check that cannot run is a failure, not a skip.** If the test command is
missing, or the file to inspect is not there, the attempt has not been shown to
work. Treating "could not verify" as "fine" would reintroduce, in one line, the
exact hole this project exists to close.

Ordering matters for cost, not correctness. Cheap structural checks -- diff
size, forbidden paths -- run before commands, so an attempt that touched
`.github/` is rejected before a test suite spends four minutes agreeing.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field

from .spec import Check, Job
from .worktree import DiffStat, Worktree

__all__ = ["CheckResult", "Verdict", "verify"]

#: Checks that need no subprocess. Run first so a cheap rejection does not
#: wait for an expensive agreement.
_STRUCTURAL = ("changed_files", "diff_lines", "must_not_touch", "must_touch")

#: How many lines of a failing command's output to quote. Enough for a pytest
#: summary and the assertion above it; short enough that a report of twenty
#: failures is still readable.
QUOTED_LINES = 12


@dataclass(frozen=True)
class CheckResult:
    """One check, its verdict, and why."""

    kind: str
    passed: bool
    detail: str
    #: Human-readable statement of what was required, from the spec.
    requirement: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "requirement": self.requirement,
        }


@dataclass
class Verdict:
    """Whether an attempt is acceptable, and the full evidence."""

    results: list[CheckResult] = field(default_factory=list)
    diff: DiffStat | None = None

    @property
    def passed(self) -> bool:
        """Conjunction. An empty verdict is **not** a pass.

        `spec` already refuses jobs with no checks, so an empty result list
        here means verification did not run at all -- a crash, a timeout, a
        worktree that vanished. Reporting that as success is the failure mode
        this class exists to make impossible.
        """
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        if not self.results:
            return "no checks ran, so nothing was verified"
        if self.passed:
            return f"{len(self.results)} of {len(self.results)} checks passed"
        first = self.failures[0]
        more = f", and {len(self.failures) - 1} more" if len(self.failures) > 1 else ""
        return f"{len(self.failures)} of {len(self.results)} checks failed: {first.detail}{more}"

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "summary": self.summary(),
            "results": [r.as_dict() for r in self.results],
            "diff": self.diff.as_dict() if self.diff else None,
        }


#: Lines that are pure decoration in test-runner output. Dropping them is
#: worth a rule of its own: with them, a twelve-line quote is half banner and
#: the assertion that explains the failure falls off the end.
_SEPARATOR = re.compile(r"(?:^[=_\-~*#]{4,})|(?:[=_\-~*#]{4,}$)")


def _quote_output(result: subprocess.CompletedProcess) -> str:
    """The lines of a failing command worth putting in a report.

    Compacted rather than raw. Progress lines like `F      [100%]` carry a
    column of padding, and separator banners carry nothing at all; both crowd
    out the assertion, which is the only line anybody actually needs.
    """
    text = (result.stdout or "") + (result.stderr or "")
    lines = []
    for line in text.splitlines():
        collapsed = re.sub(r"\s{2,}", " ", line.strip())
        # Strip the banner rules that wrap a heading. A first attempt matched
        # whole-line separators only, which left `===== FAILURES =====` intact
        # and still crowded out the assertion.
        collapsed = _SEPARATOR.sub("", collapsed).strip()
        if collapsed:
            lines.append(collapsed)
    if not lines:
        return f"exit {result.returncode}, no output"
    tail = lines[-QUOTED_LINES:]
    elided = f" (last {QUOTED_LINES} of {len(lines)} lines)" if len(lines) > QUOTED_LINES else ""
    return f"exit {result.returncode}{elided}: " + " | ".join(tail)


def _matches_any(path: str, patterns) -> str | None:
    """The first pattern that matches ``path``, or None.

    `fnmatch` treats `*` as crossing `/`, which is wrong for gitignore-style
    intent, so a pattern containing `/` is matched against the whole path and
    one without is matched against the basename as well. Returning *which*
    pattern matched, rather than a boolean, is what lets the report name it.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return pattern
        if "/" not in pattern and fnmatch.fnmatch(os.path.basename(path), pattern):
            return pattern
        if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
            return pattern
    return None


def _bounds(check: Check, actual: int, noun: str) -> CheckResult:
    limits = dict(check.value)
    low, high = limits.get("min"), limits.get("max")
    if high is not None and actual > high:
        return CheckResult(check.kind, False, f"{actual} {noun}, limit was {high}", check.describe())
    if low is not None and actual < low:
        return CheckResult(
            check.kind, False, f"only {actual} {noun}, at least {low} required", check.describe()
        )
    bound = f"max {high}" if high is not None else f"min {low}"
    return CheckResult(check.kind, True, f"{actual} {noun}, within {bound}", check.describe())


def _structural(check: Check, diff: DiffStat) -> CheckResult:
    if check.kind == "changed_files":
        return _bounds(check, diff.files_changed, "files changed")
    if check.kind == "diff_lines":
        return _bounds(check, diff.lines, "lines changed")

    if check.kind == "must_not_touch":
        offenders = [(p, m) for p in diff.paths if (m := _matches_any(p, check.value))]
        if offenders:
            named = ", ".join(f"{path} (matched {pattern!r})" for path, pattern in offenders[:4])
            return CheckResult(check.kind, False, f"modified {named}", check.describe())
        return CheckResult(
            check.kind, True, f"none of {len(diff.paths)} changed paths matched", check.describe()
        )

    # must_touch
    missing = [p for p in check.value if not any(_matches_any(path, [p]) for path in diff.paths)]
    if missing:
        return CheckResult(
            check.kind, False, f"nothing matched {', '.join(missing)}", check.describe()
        )
    return CheckResult(check.kind, True, "every required path was modified", check.describe())


def _run_check(check: Check, worktree: Worktree, timeout: int) -> CheckResult:
    command = str(check.value)
    try:
        result = worktree.run(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            check.kind, False, f"`{command}` did not finish within {timeout}s", check.describe()
        )
    except OSError as error:
        # The command could not be started at all. Not a skip: an attempt
        # whose verification could not run has not been shown to work.
        return CheckResult(check.kind, False, f"`{command}` could not run: {error}", check.describe())
    if result.returncode != 0:
        return CheckResult(check.kind, False, f"`{command}` {_quote_output(result)}", check.describe())
    return CheckResult(check.kind, True, f"`{command}` {_quote_output(result)}", check.describe())


def _file_contains(check: Check, worktree: Worktree) -> CheckResult:
    spec = dict(check.value) if isinstance(check.value, dict) else {}
    path, needle = spec.get("path"), spec.get("text")
    if not path or needle is None:
        return CheckResult(
            check.kind, False, "check needs {path: ..., text: ...}", check.describe()
        )
    full = os.path.join(worktree.path or "", path)
    if not os.path.isfile(full):
        return CheckResult(check.kind, False, f"{path} does not exist", check.describe())
    with open(full, encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    if needle in content:
        return CheckResult(check.kind, True, f"{path} contains {needle!r}", check.describe())
    return CheckResult(check.kind, False, f"{path} does not contain {needle!r}", check.describe())


def verify(job: Job, worktree: Worktree, timeout: int | None = None) -> Verdict:
    """Run every check the job declared and return the verdict with evidence.

    Never raises for a failing check -- a failure is a result, not an error.
    It does propagate a :class:`~runproof.worktree.WorktreeError`, because
    losing the ground the checks run on means the verdict would be a fiction.
    """
    timeout = timeout or job.limits.wall_seconds
    diff = worktree.diff()
    verdict = Verdict(diff=diff)

    for check in job.checks:
        if check.kind in _STRUCTURAL:
            verdict.results.append(_structural(check, diff))

    # Bail out before spending time on commands if the shape is already wrong.
    # The attempt is rejected either way; this only decides how long it takes.
    if any(not r.passed for r in verdict.results):
        for check in job.checks:
            if check.kind not in _STRUCTURAL:
                verdict.results.append(
                    CheckResult(
                        check.kind, False, "not run: a structural check already failed",
                        check.describe(),
                    )
                )
        return verdict

    for check in job.checks:
        if check.kind == "run":
            verdict.results.append(_run_check(check, worktree, timeout))
        elif check.kind == "file_contains":
            verdict.results.append(_file_contains(check, worktree))
    return verdict
