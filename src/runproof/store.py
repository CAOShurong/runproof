"""Where runs are remembered, so that silence becomes a fact.

The failure this project is named after was silent in both directions: a chain
of scheduled agent runs never started, and nothing anywhere recorded that they
had not. The sidebar showed the tasks. The tasks had never fired. There was no
way to tell those two states apart without hand-parsing log files.

So the store is built around one idea: **absence must be queryable**. A run
that is dispatched writes a row *before* it does anything else, and that row
says `running` until something changes it. If a process dies, the row stays
`running` with a stale heartbeat, and `stale_runs()` finds it. A gap in the
history is therefore always attributable — either there is a row saying what
happened, or there is no row and nothing was ever dispatched. Those are
different answers and the tool can give either one.

Three decisions follow from that.

**Append-only.** Attempts and check results are never updated or deleted, only
inserted. A rejected attempt keeps its branch name so you can go and look at
it; the interesting failures are the ones you can still inspect. Overwriting
history to keep the table tidy would destroy the evidence the tool exists to
produce.

**SQLite, in the repository, not a service.** `.runproof/runs.db` sits next to
the code it describes. No daemon to be down, no port to be wrong, and the file
is trivially copyable when somebody asks "what happened on your machine".

**Schema version in the database.** Migrations run forward on open. A store
written by a newer version refuses to open rather than silently
mis-reading columns that have moved.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass

__all__ = ["Attempt", "Run", "Store", "StoreError"]

#: Bumped whenever the schema changes. `_MIGRATIONS` must gain a matching
#: entry, and old databases are upgraded on open.
SCHEMA_VERSION = 2

#: A run whose heartbeat is older than this is presumed dead. Generous on
#: purpose: an agent can legitimately think for minutes without writing
#: anything, and declaring a live run dead is worse than noticing late,
#: because the recovery path is to start a second one on the same branch.
STALE_AFTER_SECONDS = 600

#: Terminal states. Anything not in here is still in flight.
FINAL_STATES = ("passed", "failed", "rejected", "cancelled", "error")

_MIGRATIONS = [
    # 0 -> 1
    """
    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job         TEXT    NOT NULL,
        spec        TEXT    NOT NULL,
        adapter     TEXT    NOT NULL,
        state       TEXT    NOT NULL,
        started_at  REAL    NOT NULL,
        finished_at REAL,
        heartbeat   REAL    NOT NULL,
        detail      TEXT,
        trigger     TEXT    NOT NULL DEFAULT 'manual'
    );
    CREATE INDEX runs_job_started ON runs (job, started_at DESC);
    CREATE INDEX runs_state ON runs (state);

    CREATE TABLE attempts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      INTEGER NOT NULL REFERENCES runs (id),
        ordinal     INTEGER NOT NULL,
        branch      TEXT,
        worktree    TEXT,
        state       TEXT    NOT NULL,
        started_at  REAL    NOT NULL,
        finished_at REAL,
        wall_seconds REAL,
        tokens      INTEGER,
        files_changed INTEGER,
        diff_lines  INTEGER,
        detail      TEXT
    );
    CREATE INDEX attempts_run ON attempts (run_id, ordinal);

    CREATE TABLE check_results (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id  INTEGER NOT NULL REFERENCES attempts (id),
        kind        TEXT    NOT NULL,
        passed      INTEGER NOT NULL,
        detail      TEXT    NOT NULL,
        recorded_at REAL    NOT NULL
    );
    CREATE INDEX check_attempt ON check_results (attempt_id);
    """,
    # 1 -> 2: schedules, and the scheduler's own pulse.
    #
    # `ticks` exists because of the failure this project is named after. The
    # scheduler stopped dispatching and nothing recorded that it had; the only
    # evidence available afterwards was that tasks had no `lastRunAt`, which
    # is indistinguishable from "never scheduled". A row written on every wake
    # -- whether or not anything was due -- turns "the scheduler is dead" from
    # an inference into a lookup.
    """
    CREATE TABLE schedules (
        job          TEXT PRIMARY KEY,
        spec_path    TEXT NOT NULL,
        every_seconds INTEGER NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        next_due     REAL NOT NULL,
        last_dispatch REAL,
        created_at   REAL NOT NULL
    );

    CREATE TABLE ticks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        at         REAL NOT NULL,
        dispatched INTEGER NOT NULL DEFAULT 0,
        note       TEXT
    );
    CREATE INDEX ticks_at ON ticks (at DESC);
    """,
]


class StoreError(RuntimeError):
    """The history cannot be trusted, so refuse to use it."""


@dataclass(frozen=True)
class Run:
    """One dispatch of one job."""

    id: int
    job: str
    adapter: str
    state: str
    started_at: float
    finished_at: float | None
    heartbeat: float
    detail: str | None
    trigger: str

    @property
    def running(self) -> bool:
        return self.state not in FINAL_STATES

    def stale(self, now: float | None = None) -> bool:
        """Running, but nothing has been heard from it for too long."""
        now = time.time() if now is None else now
        return self.running and (now - self.heartbeat) > STALE_AFTER_SECONDS

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "job": self.job,
            "adapter": self.adapter,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat": self.heartbeat,
            "detail": self.detail,
            "trigger": self.trigger,
            "stale": self.stale(),
        }


@dataclass(frozen=True)
class Attempt:
    """One try at a job. Agents are stochastic, so a job may have several."""

    id: int
    run_id: int
    ordinal: int
    branch: str | None
    state: str
    started_at: float
    finished_at: float | None
    wall_seconds: float | None
    tokens: int | None
    files_changed: int | None
    diff_lines: int | None
    detail: str | None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "ordinal": self.ordinal,
            "branch": self.branch,
            "state": self.state,
            "wall_seconds": self.wall_seconds,
            "tokens": self.tokens,
            "files_changed": self.files_changed,
            "diff_lines": self.diff_lines,
            "detail": self.detail,
        }


class Store:
    """Run history for one repository."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        # Durability matters more than speed here: the whole point is that a
        # row survives the process that wrote it dying.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def for_repository(cls, root: str) -> Store:
        return cls(os.path.join(root, ".runproof", "runs.db"))

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by a newer runproof (schema {version}, "
                f"this build understands {SCHEMA_VERSION}). Refusing to open it "
                "rather than misreading columns that may have moved."
            )
        for index in range(version, SCHEMA_VERSION):
            self._connection.executescript(_MIGRATIONS[index])
        if version < SCHEMA_VERSION:
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.commit()

    # -- writing -----------------------------------------------------------

    def start_run(self, job, trigger: str = "manual", now: float | None = None) -> int:
        """Record a dispatch **before** any work happens.

        The ordering is the point. A row written after the work would be
        missing for exactly the runs that died, which are the ones worth
        knowing about.
        """
        now = time.time() if now is None else now
        cursor = self._connection.execute(
            "INSERT INTO runs (job, spec, adapter, state, started_at, heartbeat, trigger)"
            " VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (job.name, json.dumps(job.as_dict()), job.adapter, now, now, trigger),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def heartbeat(self, run_id: int, now: float | None = None) -> None:
        """Say that this run is still alive. Cheap on purpose: call it often."""
        self._connection.execute(
            "UPDATE runs SET heartbeat = ? WHERE id = ?",
            (time.time() if now is None else now, run_id),
        )
        self._connection.commit()

    def finish_run(self, run_id: int, state: str, detail: str = "", now=None) -> None:
        if state not in FINAL_STATES:
            raise StoreError(f"{state!r} is not a final state: {', '.join(FINAL_STATES)}")
        now = time.time() if now is None else now
        self._connection.execute(
            "UPDATE runs SET state = ?, finished_at = ?, heartbeat = ?, detail = ? WHERE id = ?",
            (state, now, now, detail, run_id),
        )
        self._connection.commit()

    def start_attempt(self, run_id: int, ordinal: int, now=None) -> int:
        now = time.time() if now is None else now
        cursor = self._connection.execute(
            "INSERT INTO attempts (run_id, ordinal, state, started_at) VALUES (?, ?, 'running', ?)",
            (run_id, ordinal, now),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        state: str,
        *,
        branch: str | None = None,
        worktree: str | None = None,
        tokens: int | None = None,
        files_changed: int | None = None,
        diff_lines: int | None = None,
        detail: str = "",
        now=None,
    ) -> None:
        now = time.time() if now is None else now
        started = self._connection.execute(
            "SELECT started_at FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if started is None:
            raise StoreError(f"no attempt {attempt_id}")
        self._connection.execute(
            "UPDATE attempts SET state = ?, finished_at = ?, wall_seconds = ?,"
            " branch = ?, worktree = ?, tokens = ?, files_changed = ?,"
            " diff_lines = ?, detail = ? WHERE id = ?",
            (
                state,
                now,
                now - started["started_at"],
                branch,
                worktree,
                tokens,
                files_changed,
                diff_lines,
                detail,
                attempt_id,
            ),
        )
        self._connection.commit()

    def record_check(self, attempt_id: int, kind: str, passed: bool, detail: str, now=None) -> None:
        """Store one check result, with its detail quoted rather than summarised.

        `detail` is the failing test name, the offending path, the actual
        number against the limit. A report saying "tests failed" sends the
        reader back to the logs, which is where they were before the tool.
        """
        self._connection.execute(
            "INSERT INTO check_results (attempt_id, kind, passed, detail, recorded_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (attempt_id, kind, 1 if passed else 0, detail, time.time() if now is None else now),
        )
        self._connection.commit()

    # -- reading -----------------------------------------------------------

    def _run(self, row) -> Run:
        return Run(
            id=row["id"],
            job=row["job"],
            adapter=row["adapter"],
            state=row["state"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            heartbeat=row["heartbeat"],
            detail=row["detail"],
            trigger=row["trigger"],
        )

    def get_run(self, run_id: int) -> Run | None:
        row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def recent_runs(self, limit: int = 20, job: str | None = None) -> list[Run]:
        if job:
            rows = self._connection.execute(
                "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT ?",
                (job, limit),
            )
        else:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        return [self._run(row) for row in rows]

    def stale_runs(self, now: float | None = None) -> list[Run]:
        """Runs that claim to be alive but have stopped saying so.

        This is the query that would have answered the question the tool is
        named after in one line instead of an afternoon.
        """
        return [run for run in self._all_running() if run.stale(now)]

    def _all_running(self) -> list[Run]:
        placeholders = ", ".join("?" for _ in FINAL_STATES)
        rows = self._connection.execute(
            f"SELECT * FROM runs WHERE state NOT IN ({placeholders})", FINAL_STATES
        )
        return [self._run(row) for row in rows]

    def attempts(self, run_id: int) -> list[Attempt]:
        rows = self._connection.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY ordinal", (run_id,)
        )
        return [
            Attempt(
                id=r["id"],
                run_id=r["run_id"],
                ordinal=r["ordinal"],
                branch=r["branch"],
                state=r["state"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                wall_seconds=r["wall_seconds"],
                tokens=r["tokens"],
                files_changed=r["files_changed"],
                diff_lines=r["diff_lines"],
                detail=r["detail"],
            )
            for r in rows
        ]

    def checks(self, attempt_id: int) -> list[dict]:
        rows = self._connection.execute(
            "SELECT kind, passed, detail FROM check_results WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        )
        return [
            {"kind": r["kind"], "passed": bool(r["passed"]), "detail": r["detail"]} for r in rows
        ]

    def pass_rate(self, job: str) -> tuple[int, int]:
        """``(passed, total)`` attempts across every run of ``job``.

        The number that makes a stochastic agent's result mean something. One
        green run is an anecdote; nine out of ten is a property.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN a.state = 'passed' THEN 1 ELSE 0 END) AS passed"
            " FROM attempts a JOIN runs r ON r.id = a.run_id"
            " WHERE r.job = ? AND a.state IN ('passed', 'failed', 'rejected', 'error')",
            (job,),
        ).fetchone()
        return int(row["passed"] or 0), int(row["total"] or 0)
