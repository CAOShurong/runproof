"""Tests for the HTML dashboard, weighted at the two ways it could lie.

A dashboard fails in a way a terminal report does not: it can look finished
while being wrong, because a page that renders is a page that looks like it
worked. So the tests here are about **what must appear when there is nothing
to show** -- an overdue schedule with no runs, a dispatch that went quiet --
and about escaping, since a check's detail is a quoted pytest failure and
those are full of angle brackets.
"""

import os
import time

from conftest import APPEND_SUB, BREAK_TESTS, spec_text
from runproof.cli import EXIT_OK, main
from runproof.dashboard import build_dashboard, render_dashboard
from runproof.runner import run_job
from runproof.schedule import Scheduler
from runproof.spec import parse_job
from runproof.store import Store


def write_spec(repo, name, prompt):
    path = os.path.join(repo, f"{name}.yaml")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(spec_text(name, prompt))
    return path


def test_an_empty_repository_still_renders_something_honest(repo):
    page = render_dashboard(repo)
    assert page.startswith("<!doctype html>")
    assert "No runs recorded yet" in page
    # The one fact that matters on an empty install, and the one a dashboard
    # with nothing to draw would be tempted to leave out.
    assert "never ticked" in page


def test_a_dead_scheduler_is_on_the_page_not_merely_absent(repo):
    """The failure this project is named after, in the form somebody would
    actually meet it: a dashboard with no runs on it."""
    spec = write_spec(repo, "nightly", APPEND_SUB)
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now - 86400)
        scheduler.record_tick(now=now - 86400)

    page = render_dashboard(repo, now=now)
    assert "presumed dead" in page
    assert "nightly" in page
    assert "late by" in page


def test_the_pass_rate_is_over_every_attempt_not_the_last_one(repo):
    run_job(parse_job(spec_text("flaky", APPEND_SUB)), root=repo)
    run_job(parse_job(spec_text("flaky", BREAK_TESTS)), root=repo)
    page = render_dashboard(repo)
    # One passed, one rejected: the headline must be the rate, not "passed"
    # because the newest run happened to be green, nor "failed" because it
    # happened to be red.
    assert "1/2" in page
    assert "50%" in page


def test_an_adapter_error_is_not_hidden_from_the_pass_rate(repo):
    run_job(parse_job(spec_text("with-error", APPEND_SUB)), root=repo)
    run_job(
        parse_job(spec_text("with-error", "runproof-command-that-does-not-exist")),
        root=repo,
    )

    page = render_dashboard(repo)

    assert "1/2" in page
    assert "50%" in page
    assert "error" in page


def test_quoted_failures_are_escaped(repo):
    """A check's detail is a pytest transcript. `assert 2 == 3` is harmless;
    the same field carrying `<` from a diff or a type name is not."""
    with Store.for_repository(repo) as store:
        run_id = store.start_run(parse_job(spec_text("x", APPEND_SUB)))
        store.finish_run(run_id, "failed", "TypeError: expected <class 'int'> & got <str>")
    page = render_dashboard(repo)
    assert "&lt;class" in page
    assert "<class 'int'>" not in page
    assert "&amp; got" in page


def test_it_is_one_file_with_nothing_fetched(repo):
    """A dashboard that fetches a stylesheet is a dashboard that renders as a
    wall of serif text the first time it is opened without a network."""
    run_job(parse_job(spec_text("good", APPEND_SUB)), root=repo)
    page = render_dashboard(repo)
    for marker in ("http://", "https://", "<script", "<link", "src="):
        assert marker not in page, f"the dashboard reaches outside itself: {marker!r}"


def test_writing_it_creates_the_directory_and_returns_the_path(repo):
    path = build_dashboard(repo)
    assert os.path.isfile(path)
    assert path.endswith("dashboard.html")
    with open(path, encoding="utf-8") as handle:
        assert handle.read().startswith("<!doctype html>")


def test_the_command_writes_where_it_is_told(repo, capsys, tmp_path):
    target = str(tmp_path / "out" / "board.html")
    assert main(["-C", repo, "--no-color", "dashboard", "--output", target]) == EXIT_OK
    assert os.path.isfile(target)
    assert "board.html" in capsys.readouterr().out
