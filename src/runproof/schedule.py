"""Dispatching work on a timer, and being able to prove the timer is alive.

This module exists because of one night. Thirteen agent runs were scheduled to
build a project overnight and **not one of them fired**. The scheduler stopped
dispatching and never recovered. Every task sat with its next-run time in the
past and no last-run time at all, which is exactly what a task that had never
been scheduled looks like, so the failure was invisible from the outside and
took an afternoon of hand-parsing logs to establish.

The design follows directly from that.

**The scheduler records that it woke up, not merely that it dispatched.** Every
tick writes a row, including the ones where nothing was due. That single
decision is what separates "the scheduler is running and there was no work"
from "the scheduler is dead", and no amount of looking at the job list can tell
those apart. It is the cheapest row in the database and the only one that
answers the question anybody actually asks at 9am.

**Being overdue is a fact with a number attached.** :func:`doctor` reports how
late each schedule is and how long since the last tick, rather than a green
tick or a red cross. "Last tick 9 hours ago, 13 schedules overdue by up to 7
hours" is a diagnosis; a status light is a rumour.

**A missed window is caught up once, not N times.** A scheduler that was down
for six hours should not fire six backlogged runs the moment it returns --
that is a thundering herd of agents against one repository. Each schedule
catches up a single run and the skipped windows are recorded with a reason.

There is no daemon here. `tick()` is meant to be called by whatever already
wakes up on your machine -- cron, a systemd timer, Task Scheduler, a loop in a
terminal. Owning the wake-up is how a scheduler acquires the ability to die
quietly, and this one deliberately does not.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .spec import load_job
from .store import Store

__all__ = ["Health", "Schedule", "Scheduler", "doctor"]

#: How long without a tick before the scheduler is presumed dead. Deliberately
#: only a few missed intervals: the entire point is to notice quickly, and a
#: false alarm costs a glance while a missed night costs the night.
TICK_STALE_SECONDS = 900

#: The shortest interval a schedule may declare. Anything faster is almost
#: certainly a typo, and a runaway schedule spawning agents in a loop is
#: expensive in a way that a typo should not be.
MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class Schedule:
    """A job that should run on a timer."""

    job: str
    spec_path: str
    every_seconds: int
    enabled: bool
    next_due: float
    last_dispatch: float | None

    def overdue_by(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.next_due) if self.enabled else 0.0

    def as_dict(self) -> dict:
        return {
            "job": self.job,
            "spec_path": self.spec_path,
            "every_seconds": self.every_seconds,
            "enabled": self.enabled,
            "next_due": self.next_due,
            "last_dispatch": self.last_dispatch,
            "overdue_by": round(self.overdue_by(), 1),
        }


@dataclass
class Health:
    """What `runproof doctor` answers, in facts rather than a status light."""

    alive: bool
    last_tick: float | None
    seconds_since_tick: float | None
    overdue: list[Schedule]
    stale_runs: list
    total_schedules: int

    #: True when the newest tick is stamped in the future. Almost always a
    #: clock that moved -- but worth saying, because the liveness test is a
    #: comparison against that timestamp, and a future one would otherwise
    #: make a dead scheduler look permanently healthy.
    clock_skew: bool = False

    @staticmethod
    def _plural(count: int, word: str) -> str:
        return f"{count} {word}" + ("" if count == 1 else "s")

    def summary(self) -> str:
        if self.last_tick is None:
            return (
                "the scheduler has never ticked. Nothing is dispatching, and "
                "no run has ever been attempted on a timer."
            )
        if self.clock_skew:
            return (
                "the newest tick is stamped in the future, so liveness cannot "
                "be judged from it. Check the system clock."
            )
        ago = self._plural(round(self.seconds_since_tick / 60), "minute") + " ago"
        late = len(self.overdue)
        verb = "is" if late == 1 else "are"
        if not self.alive:
            return (
                f"the scheduler last ticked {ago} and is presumed dead. "
                f"{late} of {self._plural(self.total_schedules, 'schedule')} {verb} overdue."
            )
        if self.overdue:
            worst = self._plural(round(max(s.overdue_by() for s in self.overdue) / 60), "minute")
            return (
                f"the scheduler is alive (last tick {ago}) but "
                f"{self._plural(late, 'schedule')} {verb} overdue, the worst by {worst}."
            )
        return f"the scheduler is alive; last tick {ago}, nothing overdue."

    def as_dict(self) -> dict:
        return {
            "alive": self.alive,
            "last_tick": self.last_tick,
            "seconds_since_tick": self.seconds_since_tick,
            "summary": self.summary(),
            "overdue": [s.as_dict() for s in self.overdue],
            "stale_runs": [r.as_dict() for r in self.stale_runs],
            "total_schedules": self.total_schedules,
        }


class Scheduler:
    """Schedules for one repository, and the dispatcher that services them."""

    def __init__(self, root: str, store: Store | None = None):
        from .worktree import repository_root

        self.root = repository_root(root)
        self._owned = store is None
        self.store = store or Store.for_repository(self.root)
        self._connection = self.store._connection  # deliberate: same database

    def close(self) -> None:
        if self._owned:
            self.store.close()

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- managing schedules ------------------------------------------------

    def add(self, spec_path: str, every_seconds: int, now: float | None = None) -> Schedule:
        """Register a job to run on a timer.

        The spec is parsed now rather than at dispatch time. A schedule whose
        spec is invalid should fail while somebody is watching, not silently
        at three in the morning.
        """
        if every_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"every_seconds must be at least {MIN_INTERVAL_SECONDS}; "
                f"{every_seconds} is almost certainly a typo, and a runaway "
                "schedule spawns agents in a loop."
            )
        job = load_job(spec_path)
        now = time.time() if now is None else now
        self._connection.execute(
            "INSERT OR REPLACE INTO schedules"
            " (job, spec_path, every_seconds, enabled, next_due, last_dispatch, created_at)"
            " VALUES (?, ?, ?, 1, ?, NULL, ?)",
            (job.name, os.path.abspath(spec_path), every_seconds, now + every_seconds, now),
        )
        self._connection.commit()
        return self.get(job.name)

    def remove(self, job: str) -> None:
        self._connection.execute("DELETE FROM schedules WHERE job = ?", (job,))
        self._connection.commit()

    def set_enabled(self, job: str, enabled: bool) -> None:
        self._connection.execute(
            "UPDATE schedules SET enabled = ? WHERE job = ?", (1 if enabled else 0, job)
        )
        self._connection.commit()

    def _schedule(self, row) -> Schedule:
        return Schedule(
            job=row["job"],
            spec_path=row["spec_path"],
            every_seconds=row["every_seconds"],
            enabled=bool(row["enabled"]),
            next_due=row["next_due"],
            last_dispatch=row["last_dispatch"],
        )

    def get(self, job: str) -> Schedule | None:
        row = self._connection.execute(
            "SELECT * FROM schedules WHERE job = ?", (job,)
        ).fetchone()
        return self._schedule(row) if row else None

    def all(self) -> list[Schedule]:
        rows = self._connection.execute("SELECT * FROM schedules ORDER BY job")
        return [self._schedule(row) for row in rows]

    def due(self, now: float | None = None) -> list[Schedule]:
        now = time.time() if now is None else now
        return [s for s in self.all() if s.enabled and s.next_due <= now]

    # -- the pulse ---------------------------------------------------------

    def record_tick(self, dispatched: int = 0, note: str = "", now=None) -> None:
        """Say the scheduler woke up. Written even when nothing was due.

        This is the row the whole module is for. Without it, "no runs last
        night" has two explanations -- nothing was scheduled, or the
        dispatcher was dead -- and no way to choose between them.
        """
        self._connection.execute(
            "INSERT INTO ticks (at, dispatched, note) VALUES (?, ?, ?)",
            (time.time() if now is None else now, dispatched, note),
        )
        self._connection.commit()

    def last_tick(self) -> float | None:
        row = self._connection.execute("SELECT MAX(at) AS at FROM ticks").fetchone()
        return row["at"] if row and row["at"] is not None else None

    def tick(self, now: float | None = None, runner=None) -> list:
        """Dispatch everything due, and record having looked either way.

        A schedule that is overdue by several intervals is caught up **once**.
        Firing the whole backlog would put a herd of agents on one repository
        the moment a laptop wakes from sleep.
        """
        now = time.time() if now is None else now
        if runner is None:
            from .runner import run_job as runner

        outcomes = []
        due = self.due(now)
        for schedule in due:
            missed = int((now - schedule.next_due) // schedule.every_seconds)
            job = load_job(schedule.spec_path)
            self._connection.execute(
                "UPDATE schedules SET next_due = ?, last_dispatch = ? WHERE job = ?",
                (now + schedule.every_seconds, now, schedule.job),
            )
            self._connection.commit()
            if missed:
                self.record_tick(
                    0, f"{schedule.job}: skipped {missed} missed window(s), catching up once", now
                )
            outcomes.append(runner(job, root=self.root, trigger="schedule", store=self.store))

        self.record_tick(len(due), "" if due else "nothing due", now)
        return outcomes


def doctor(root: str, now: float | None = None) -> Health:
    """Is the scheduler alive, and what is late?

    The function that would have answered, in one call, the question that cost
    an afternoon: *did the overnight work run, and if not, why not?*
    """
    now = time.time() if now is None else now
    with Scheduler(root) as scheduler:
        last = scheduler.last_tick()
        since = None if last is None else now - last
        # A tick stamped in the future means the clock moved, and the liveness
        # test is a comparison against that stamp -- so without this a
        # backwards clock would make a dead scheduler read as permanently
        # healthy, which is the one answer this function must never give.
        skew = since is not None and since < 0
        return Health(
            alive=(since is not None and 0 <= since <= TICK_STALE_SECONDS),
            last_tick=last,
            seconds_since_tick=None if since is None else max(since, 0.0),
            overdue=[s for s in scheduler.all() if s.overdue_by(now) > 0],
            stale_runs=scheduler.store.stale_runs(now),
            total_schedules=len(scheduler.all()),
            clock_skew=skew,
        )
