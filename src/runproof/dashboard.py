"""A single HTML file you can open, mail, or attach to a bug report.

The terminal report answers *what happened in this run*. This answers the
other question, the one that only exists once a tool has been left alone for
a week: **what has been happening, and is anything quietly not happening?**

Three decisions, and the third is the reason the file exists at all.

**One file, no network.** Inline CSS, inline SVG, no script, no CDN. A
dashboard that needs a server is a dashboard nobody opens at 9am, and one that
fetches a stylesheet from somewhere is a dashboard that renders as a wall of
serif text the first time it is opened on a plane. It is also then safe to
attach to an issue, which is the form this information usually needs to travel
in.

**Absence is drawn, not omitted.** A job whose schedule is overdue gets a row
saying so even though it has produced nothing to show. A run whose heartbeat
went stale gets a row even though it never finished. Every dashboard that has
ever misled anybody did it by having nothing to draw and drawing nothing.

**The pass rate is the headline, not the last result.** Per job, over every
attempt ever recorded. A job at 9/10 and a job at 1/10 both "worked last
night"; they are not the same job and they do not call for the same decision.

Written with no templating library, because the alternative is a runtime
dependency for a package whose pitch includes not having any. Escaping goes
through :func:`html.escape` on every interpolated value -- a check's detail is
a quoted pytest failure, which is exactly the sort of string that contains
angle brackets.
"""

from __future__ import annotations

import html
import os
import time

from .schedule import Scheduler, doctor
from .store import Store
from .worktree import repository_root

__all__ = ["build_dashboard", "render_dashboard"]

#: Runs listed in the history table. Enough to cover a week of nightly jobs
#: without turning the page into a log file.
RECENT = 40

#: How many attempts the per-job strip shows, newest last.
STRIP = 30

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 40px 32px 64px; background: #101218; color: #e8eaf0;
       font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; margin: 40px 0 12px; font-weight: 600; }
.sub { color: #787e8e; font-size: 14px; margin: 0; }
.card { background: #171a23; border: 1px solid #252a36; border-radius: 10px;
        padding: 18px 20px; margin-top: 12px; }
.verdict { display: flex; gap: 14px; align-items: baseline; }
.verdict .mark { font-size: 20px; line-height: 1; }
.ok { color: #7cc48c; } .bad { color: #e8765c; } .warn { color: #d8b45c; }
.muted { color: #787e8e; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }
th { text-align: left; font-weight: 600; color: #787e8e; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.06em; padding: 6px 10px; }
td { padding: 8px 10px; border-top: 1px solid #222733; vertical-align: top; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code { font: 13px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace;
       color: #b9c0d0; word-break: break-word; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
        font-size: 12px; font-weight: 600; }
.pill.ok { background: #1d3323; color: #8fd39d; }
.pill.bad { background: #35211c; color: #f0907a; }
.pill.warn { background: #332c17; color: #e0c073; }
.strip { display: flex; gap: 3px; align-items: flex-end; }
.strip i { width: 7px; height: 18px; border-radius: 2px; display: block; }
.empty { color: #787e8e; font-style: italic; padding: 14px 10px; }
footer { margin-top: 44px; color: #4e5464; font-size: 13px; }
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _duration(seconds: float) -> str:
    """A span in a unit that is not absurd for its size.

    `late by 1440m` is arithmetically correct and useless: nobody converts
    that in their head, and the number a person needs from an overdue
    schedule is "about a day", not four significant figures of minutes.
    """
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 172800:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"


def _ago(when: float | None, now: float) -> str:
    if when is None:
        return "never"
    return _duration(now - when) + " ago"


def _plural(count: int, word: str) -> str:
    """`1 schedule`, not `1 schedule(s)`.

    The parenthesised plural is the lazy version of the same slip this
    project has now made four times, and it reads as a form letter.
    """
    return f"{count} {word}" + ("" if count == 1 else "s")


def _pill(state: str) -> str:
    kind = {"passed": "ok", "failed": "bad", "error": "bad", "rejected": "bad"}.get(state, "warn")
    return f'<span class="pill {kind}">{_e(state)}</span>'


def _strip(states: list) -> str:
    """One coloured tick per attempt, oldest first.

    Deliberately not a line chart. There is no continuous quantity here --
    each attempt either passed or it did not -- and drawing a line through
    booleans invents a trend between two points that have nothing between
    them.
    """
    if not states:
        return '<span class="muted">no attempts yet</span>'
    colours = {"passed": "#7cc48c", "rejected": "#e8765c", "failed": "#e8765c"}
    ticks = "".join(
        f'<i style="background:{colours.get(state, "#5a6072")}" title="{_e(state)}"></i>'
        for state in states[-STRIP:]
    )
    return f'<div class="strip">{ticks}</div>'


def _health_section(health, now: float) -> str:
    glyph, kind = ("&#10003;", "ok") if health.alive else ("&#10007;", "bad")
    mark = f'<span class="mark {kind}">{glyph}</span>'
    rows = ""
    for schedule in health.overdue:
        rows += (
            f"<tr><td><code>{_e(schedule.job)}</code></td>"
            f"<td class='muted'>every {schedule.every_seconds}s</td>"
            f"<td class='num bad'>late by {_e(_duration(schedule.overdue_by(now)))}</td></tr>"
        )
    for run in health.stale_runs:
        rows += (
            f"<tr><td><code>{_e(run.job)}</code> <span class='muted'>run {run.id}</span></td>"
            f"<td class='muted'>dispatched, then went quiet</td>"
            f"<td class='num bad'>heartbeat {_e(_ago(run.heartbeat, now))}</td></tr>"
        )
    table = (
        f"<table><thead><tr><th>what</th><th>expected</th><th>how late</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else "<p class='muted' style='margin:10px 0 0'>"
        "Nothing is late and no run has gone quiet.</p>"
    )
    return (
        "<h2>Is anything not happening?</h2>"
        f"<div class='card'><div class='verdict'>{mark}<div>"
        f"<div>{_e(health.summary())}</div>"
        f"<p class='sub'>last tick {_e(_ago(health.last_tick, now))} &middot; "
        f"{_e(_plural(health.total_schedules, 'schedule'))} registered</p></div></div>"
        f"{table}</div>"
    )


def _jobs_section(store: Store, runs: list, now: float) -> str:
    jobs = []
    for name in sorted({run.job for run in runs}):
        passed, total = store.pass_rate(name)
        states = [
            attempt.state
            for run in sorted([r for r in runs if r.job == name], key=lambda r: r.started_at)
            for attempt in store.attempts(run.id)
        ]
        jobs.append((name, passed, total, states))

    if not jobs:
        return "<h2>Jobs</h2><div class='card'><p class='empty'>No runs recorded yet.</p></div>"

    rows = ""
    for name, passed, total, states in jobs:
        share = f"{passed / total:.0%}" if total else "--"
        kind = "ok" if total and passed == total else ("bad" if passed == 0 else "warn")
        rows += (
            f"<tr><td><code>{_e(name)}</code></td>"
            f"<td class='num'><span class='{kind}'>{passed}/{total}</span></td>"
            f"<td class='num muted'>{share}</td>"
            f"<td>{_strip(states)}</td></tr>"
        )
    return (
        "<h2>Jobs</h2>"
        "<p class='sub'>Pass rate over every attempt ever recorded, not the last result. "
        "A job at 9/10 and a job at 1/10 both worked last night.</p>"
        f"<div class='card'><table><thead><tr><th>job</th><th class='num'>attempts passed</th>"
        f"<th class='num'>rate</th><th>history</th></tr></thead><tbody>{rows}</tbody>"
        "</table></div>"
    )


def _runs_section(store: Store, runs: list, now: float) -> str:
    if not runs:
        return ""
    rows = ""
    for run in runs:
        detail = run.detail or ""
        if len(detail) > 160:
            detail = detail[:160].rstrip() + " ..."
        flag = " <span class='pill warn'>stale</span>" if run.stale(now) else ""
        rows += (
            f"<tr><td class='num muted'>{run.id}</td>"
            f"<td><code>{_e(run.job)}</code></td>"
            f"<td>{_pill(run.state)}{flag}</td>"
            f"<td class='muted'>{_e(run.trigger)}</td>"
            f"<td class='muted'>{_e(_ago(run.started_at, now))}</td>"
            f"<td><code>{_e(detail)}</code></td></tr>"
        )
    return (
        f"<h2>Recent runs</h2><div class='card'><table><thead><tr><th></th><th>job</th>"
        f"<th>state</th><th>trigger</th><th>started</th><th>what happened</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def render_dashboard(root: str, now: float | None = None) -> str:
    """The whole dashboard as one self-contained HTML string."""
    now = time.time() if now is None else now
    health = doctor(root, now=now)
    with Store.for_repository(root) as store:
        runs = store.recent_runs(limit=RECENT)
        with Scheduler(root, store=store):
            pass
        body = (
            _health_section(health, now)
            + _jobs_section(store, runs, now)
            + _runs_section(store, runs, now)
        )

    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>runproof &mdash; {_e(os.path.basename(root))}</title>"
        f"<style>{_CSS}</style></head><body><main>"
        f"<h1>runproof</h1><p class='sub'><code>{_e(root)}</code></p>"
        f"{body}"
        "<footer>One file, no network, no script. Generated by "
        "<code>runproof dashboard</code>. Nothing here is ever merged &mdash; "
        "accepted work is left on its own branch.</footer>"
        "</main></body></html>\n"
    )


def build_dashboard(root: str, output: str | None = None, now: float | None = None) -> str:
    """Write the dashboard and return the path it was written to."""
    root = repository_root(root)
    output = output or os.path.join(root, ".runproof", "dashboard.html")
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_dashboard(root, now=now))
    return output
