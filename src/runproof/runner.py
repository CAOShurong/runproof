"""Putting it together: dispatch, isolate, drive, verify, record.

This is the only module that knows about all the others, and it is deliberately
short. Everything difficult has been pushed down into the piece that owns it --
the gate does not know what an adapter is, the store does not know what a check
is -- so what is left here is sequencing and the two decisions that only make
sense with the whole picture in view.

**A run is `passed` only if every requested attempt passed.** With
`attempts: n`, the run reports how many succeeded, and the *rate* is the honest
headline. Agents are stochastic: one green attempt out of three is not "it
works", it is "it works sometimes", and must not produce a successful process
exit. The naive alternative -- report the last or best attempt -- turns a coin
flip into a fact.

**A crash is a state, not an absence.** Every exit path writes a terminal state
to the store, including the ones nobody plans for. A run that ends without a
row is exactly the thing this project exists to make impossible, so the store
update lives in `finally` and the heartbeat is written before any work starts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .adapters import AdapterError, AdapterResult, get_adapter
from .spec import Job
from .store import Store
from .verify import Verdict, verify
from .worktree import Worktree, WorktreeError, repository_root

__all__ = ["AttemptOutcome", "RunOutcome", "run_job"]


@dataclass
class AttemptOutcome:
    """One try, and everything known about it."""

    ordinal: int
    passed: bool
    branch: str | None
    verdict: Verdict | None
    adapter_result: AdapterResult | None
    wall_seconds: float
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "passed": self.passed,
            "branch": self.branch,
            "wall_seconds": round(self.wall_seconds, 2),
            "error": self.error,
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "agent": self.adapter_result.as_dict() if self.adapter_result else None,
        }


@dataclass
class RunOutcome:
    """Every attempt at one job, and what the run as a whole means."""

    run_id: int
    job: str
    attempts: list[AttemptOutcome] = field(default_factory=list)
    state: str = "running"

    @property
    def passed_count(self) -> int:
        return sum(1 for a in self.attempts if a.passed)

    @property
    def error_count(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.error)

    @property
    def passed(self) -> bool:
        return bool(self.attempts) and self.passed_count == len(self.attempts)

    @property
    def rate(self) -> str:
        """The honest headline for a stochastic process."""
        return f"{self.passed_count}/{len(self.attempts)}"

    def summary(self) -> str:
        if not self.attempts:
            return "no attempts ran"
        if self.error_count:
            first_error = next(attempt.error for attempt in self.attempts if attempt.error)
            if self.error_count == len(self.attempts):
                if len(self.attempts) == 1:
                    return f"the single attempt could not complete: {first_error}"
                return f"none of {len(self.attempts)} attempts could complete: {first_error}"
            return (
                f"{self.rate} attempts passed; {self.error_count} could not complete -- "
                "the run is incomplete"
            )
        if self.passed_count == len(self.attempts):
            if len(self.attempts) == 1:
                # Worth its own sentence rather than "all 1 attempts passed":
                # a single green attempt is the weakest evidence this tool
                # produces, and the wording should not dress it up.
                return "the single attempt passed"
            return f"all {len(self.attempts)} attempts passed"
        if self.passed_count == 0:
            first = self.attempts[0]
            why = first.error or (first.verdict.summary() if first.verdict else "unknown")
            # "0 of 1 attempts passed" is the same plural slip as the pass
            # side, and it reads as a tally when there is nothing to tally.
            if len(self.attempts) == 1:
                return f"the single attempt was rejected: {why}"
            return f"none of {len(self.attempts)} attempts passed: {why}"
        return (
            f"{self.rate} attempts passed -- the job succeeds sometimes, "
            "which is not the same as working"
        )

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "job": self.job,
            "state": self.state,
            "passed": self.passed,
            "rate": self.rate,
            "summary": self.summary(),
            "attempts": [a.as_dict() for a in self.attempts],
        }


def _one_attempt(job: Job, root: str, ordinal: int, adapter, store: Store, run_id: int):
    """Isolate, drive the agent, verify, and record. Never raises."""
    started = time.monotonic()
    attempt_id = store.start_attempt(run_id, ordinal)
    worktree = Worktree(root, job.name, ordinal=ordinal)
    outcome = AttemptOutcome(ordinal, False, None, None, None, 0.0)

    try:
        worktree.create()
        outcome.branch = worktree.branch
        store.heartbeat(run_id)

        result = adapter.run(worktree, job, job.limits.wall_seconds)
        outcome.adapter_result = result
        store.heartbeat(run_id)
        if not result.ok:
            outcome.error = result.detail or "adapter process did not complete successfully"

        # Verified even when the adapter reported failure. A timed-out or
        # crashed agent still leaves a diff, and whether that diff is
        # acceptable is a question the checks answer better than the exit
        # code does.
        verdict = verify(job, worktree)
        outcome.verdict = verdict
        # Green repository checks cannot turn an authentication failure,
        # timeout, or crashed adapter into accepted work. We still run and
        # record the checks because the partial diff is useful evidence.
        outcome.passed = result.ok and verdict.passed

        for check in verdict.results:
            store.record_check(attempt_id, check.kind, check.passed, check.detail)

        if outcome.passed:
            worktree.commit(f"runproof: {job.name} (attempt {ordinal})")

        attempt_state = "passed" if outcome.passed else ("error" if outcome.error else "rejected")
        detail = outcome.error or verdict.summary()
        store.finish_attempt(
            attempt_id,
            attempt_state,
            branch=worktree.branch,
            worktree=worktree.path,
            tokens=result.tokens,
            files_changed=verdict.diff.files_changed if verdict.diff else None,
            diff_lines=verdict.diff.lines if verdict.diff else None,
            detail=detail,
        )
    except (WorktreeError, AdapterError) as error:
        outcome.error = str(error)
        store.finish_attempt(attempt_id, "error", branch=outcome.branch, detail=str(error))
    except Exception as error:  # noqa: BLE001 - a crash must still be recorded
        outcome.error = f"{type(error).__name__}: {error}"
        store.finish_attempt(attempt_id, "error", branch=outcome.branch, detail=outcome.error)
    finally:
        outcome.wall_seconds = time.monotonic() - started
        # The branch survives; the directory does not. A rejected attempt you
        # can check out is evidence, one that was deleted is a rumour.
        worktree.remove(keep_branch=True)
    return outcome


def run_job(job: Job, root: str = ".", trigger: str = "manual", store: Store | None = None):
    """Run ``job`` to completion and record everything about it."""
    root = repository_root(root)
    owned = store is None
    store = store or Store.for_repository(root)
    adapter = get_adapter(job.adapter)

    run_id = store.start_run(job, trigger=trigger)
    outcome = RunOutcome(run_id=run_id, job=job.name)
    try:
        for ordinal in range(1, job.attempts + 1):
            outcome.attempts.append(_one_attempt(job, root, ordinal, adapter, store, run_id))
            store.heartbeat(run_id)
        if any(attempt.error for attempt in outcome.attempts):
            outcome.state = "error"
        elif outcome.passed:
            outcome.state = "passed"
        else:
            outcome.state = "failed"
    except BaseException as error:  # noqa: BLE001 - including KeyboardInterrupt
        outcome.state = "error"
        store.finish_run(run_id, "error", f"{type(error).__name__}: {error}")
        raise
    else:
        store.finish_run(run_id, outcome.state, outcome.summary())
    finally:
        if owned:
            store.close()
    return outcome
