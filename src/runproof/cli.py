"""The command line.

Three decisions worth stating, all of them about refusing to be convenient.

**Exit codes mean something specific.** `0` accepted, `1` rejected, `2` could
not run at all. The middle one is the interesting case: a job whose agent
produced work that failed the checks is *not* an error -- the tool did exactly
what it was asked -- but it must not look like success to a shell script
either. `runproof doctor` follows the same shape, so a monitoring job is one
line.

**`tick` is the whole scheduler.** There is no daemon. Point cron, a systemd
timer or Windows Task Scheduler at `runproof tick` and the dispatcher inherits
their reliability instead of inventing its own -- and, more to the point,
inherits their visibility when they stop. A background process that owns its
own wake-up is a background process that can die quietly, which is the failure
this entire package was written after.

**Nothing merges.** Every accepted run leaves a branch. The tool proves work is
acceptable; deciding to take it stays a human act, and one `git merge` is a
small price for never having to ask what an agent did to your main branch.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .report import render_doctor, render_run, render_status, supports_colour, supports_unicode
from .schedule import Scheduler, doctor
from .spec import SpecError, load_job
from .store import Store
from .worktree import WorktreeError, prune_stale, repository_root

__all__ = ["main"]

#: `1` is "ran fine, the work was not acceptable". Distinct from `2`, which is
#: "could not run", because a CI job wants to treat those differently.
EXIT_OK, EXIT_REJECTED, EXIT_ERROR = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runproof",
        description="Run agent work unattended, and prove it actually worked.",
        epilog="Nothing is ever merged. Accepted work is left on its own branch.",
    )
    parser.add_argument("--version", action="version", version=f"runproof {__version__}")
    parser.add_argument("-C", "--directory", default=".", help="repository to work in")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-color", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a job now and verify the result")
    run.add_argument("spec", help="path to a job spec (YAML)")
    run.add_argument("--attempts", type=int, help="override the spec's attempt count")

    sub.add_parser("status", help="recent runs").add_argument("--limit", type=int, default=15)
    sub.add_parser("doctor", help="is the scheduler alive, and what is late")
    sub.add_parser("tick", help="dispatch anything due (point cron at this)")

    schedule = sub.add_parser("schedule", help="manage timers").add_subparsers(
        dest="schedule_command", required=True
    )
    add = schedule.add_parser("add", help="run a job every N seconds")
    add.add_argument("spec")
    add.add_argument("--every", type=int, required=True, help="interval in seconds")
    schedule.add_parser("list", help="show schedules")
    schedule.add_parser("remove", help="stop a schedule").add_argument("job")

    show = sub.add_parser("show", help="everything about one run")
    show.add_argument("run_id", type=int)

    sub.add_parser("prune", help="clear worktrees an interrupted run left behind")

    board = sub.add_parser("dashboard", help="write a self-contained HTML summary")
    board.add_argument("--output", help="where to write it (default .runproof/dashboard.html)")
    return parser


def _emit(payload, text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, default=str) if as_json else text)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    colour = supports_colour() and not args.no_color
    unicode_ok = supports_unicode()
    style = {"colour": colour, "unicode_ok": unicode_ok}

    try:
        root = repository_root(args.directory)
    except WorktreeError as error:
        print(f"runproof: {error}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return _dispatch(args, root, style)
    except SpecError as error:
        print(f"runproof: {error}", file=sys.stderr)
        return EXIT_ERROR
    except WorktreeError as error:
        print(f"runproof: {error}", file=sys.stderr)
        return EXIT_ERROR


def _dispatch(args, root: str, style: dict) -> int:
    if args.command == "run":
        from .runner import run_job

        job = load_job(args.spec)
        if args.attempts:
            job = type(job)(**{**job.__dict__, "attempts": args.attempts})
        outcome = run_job(job, root=root)
        _emit(outcome.as_dict(), render_run(outcome, **style), args.json)
        return EXIT_OK if outcome.passed else EXIT_REJECTED

    if args.command == "status":
        with Store.for_repository(root) as store:
            runs = store.recent_runs(limit=args.limit)
        _emit([r.as_dict() for r in runs], render_status(runs, **style), args.json)
        return EXIT_OK

    if args.command == "doctor":
        health = doctor(root)
        _emit(health.as_dict(), render_doctor(health, **style), args.json)
        # A dead dispatcher or a run that went quiet is a problem worth an
        # exit code, so `runproof doctor --json` is a monitoring check.
        return EXIT_OK if (health.alive and not health.stale_runs) else EXIT_REJECTED

    if args.command == "tick":
        with Scheduler(root) as scheduler:
            outcomes = scheduler.tick()
        if args.json:
            print(json.dumps([o.as_dict() for o in outcomes], indent=2, default=str))
        elif not outcomes:
            print("Nothing was due. The tick was recorded.")
        else:
            for outcome in outcomes:
                print(render_run(outcome, **style))
        return EXIT_OK if all(o.passed for o in outcomes) else EXIT_REJECTED

    if args.command == "schedule":
        return _schedules(args, root, style)

    if args.command == "show":
        with Store.for_repository(root) as store:
            run = store.get_run(args.run_id)
            if run is None:
                print(f"runproof: no run {args.run_id}", file=sys.stderr)
                return EXIT_ERROR
            attempts = store.attempts(run.id)
            payload = {
                **run.as_dict(),
                "attempts": [{**a.as_dict(), "checks": store.checks(a.id)} for a in attempts],
            }
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK if run.state == "passed" else EXIT_REJECTED

    if args.command == "dashboard":
        from .dashboard import build_dashboard

        path = build_dashboard(root, output=args.output)
        _emit(
            {"path": path},
            f"Wrote {path}\nOne file, no network. Open it, or attach it to a bug report.",
            args.json,
        )
        return EXIT_OK

    if args.command == "prune":
        removed = prune_stale(root)
        _emit(
            {"removed": removed},
            f"Removed {len(removed)} leftover worktree(s)." if removed else "Nothing to prune.",
            args.json,
        )
        return EXIT_OK

    return EXIT_ERROR


def _schedules(args, root: str, style: dict) -> int:
    with Scheduler(root) as scheduler:
        if args.schedule_command == "add":
            schedule = scheduler.add(args.spec, every_seconds=args.every)
            _emit(
                schedule.as_dict(),
                f"Scheduled {schedule.job} every {schedule.every_seconds}s.\n"
                "Nothing dispatches until something calls `runproof tick` --\n"
                "point cron, a systemd timer or Task Scheduler at it.",
                args.json,
            )
            return EXIT_OK
        if args.schedule_command == "remove":
            scheduler.remove(args.job)
            _emit({"removed": args.job}, f"Removed schedule {args.job}.", args.json)
            return EXIT_OK

        schedules = scheduler.all()
        if args.json:
            print(json.dumps([s.as_dict() for s in schedules], indent=2, default=str))
        elif not schedules:
            print("No schedules.")
        else:
            for schedule in schedules:
                state = "enabled" if schedule.enabled else "disabled"
                late = schedule.overdue_by()
                suffix = f"  overdue by {late / 60:.0f}m" if late else ""
                print(f"  {schedule.job:<24} every {schedule.every_seconds}s  {state}{suffix}")
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
