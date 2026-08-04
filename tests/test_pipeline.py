"""Worktree isolation, the gate, and the two of them run together.

These are the tests that touch git and subprocesses, so they are the ones
worth distrusting. They are weighted at three things:

* **isolation actually isolating** -- the property whose failure would be
  destructive rather than merely wrong;
* **the gate refusing** -- broken work, forbidden paths, work that did not
  happen at all;
* **the bugs a real run already found**, each named for what went wrong.
"""

import os

import pytest

from conftest import APPEND_SUB, BREAK_TESTS, TOUCH_CI, git, spec_text
from runproof.runner import run_job
from runproof.spec import parse_job
from runproof.store import Store
from runproof.verify import SUMMARY_DETAIL, Verdict, verify
from runproof.worktree import Worktree, WorktreeError, repository_root


def branches(root):
    result = git("branch", "--list", "runproof/*", cwd=root)
    return [line.strip(" *") for line in result.stdout.splitlines() if line.strip()]


def worktrees(root):
    result = git("worktree", "list", cwd=root)
    return [line for line in result.stdout.splitlines() if line.strip()]


# -- isolation --------------------------------------------------------------


def test_a_worktree_is_somewhere_else_entirely(repo):
    with Worktree(repo, "demo") as tree:
        assert os.path.abspath(tree.path) != os.path.abspath(repo)
        assert os.path.isfile(os.path.join(tree.path, "app.py"))


def test_the_users_working_tree_is_never_written_to(repo):
    """The property whose failure would be destructive rather than wrong."""
    before = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    with Worktree(repo, "demo") as tree:
        with open(os.path.join(tree.path, "app.py"), "w", encoding="utf-8") as handle:
            handle.write("wrecked\n")
        with open(os.path.join(tree.path, "brand_new.py"), "w", encoding="utf-8") as handle:
            handle.write("x = 1\n")
    assert open(os.path.join(repo, "app.py"), encoding="utf-8").read() == before
    assert not os.path.exists(os.path.join(repo, "brand_new.py"))


def test_isolation_is_refused_rather_than_degraded(tmp_path):
    """A fallback to 'just run it in place' is what a helpful version of this
    would do, and it is the worst thing the tool could do."""
    with pytest.raises(WorktreeError, match="not inside a git repository"):
        Worktree(str(tmp_path), "demo").create()


def test_a_file_created_and_deleted_counts_as_nothing(repo):
    """The diff is measured against the base commit, not against what the
    agent wrote. A tool counting 'files written' would disagree."""
    with Worktree(repo, "demo") as tree:
        scratch = os.path.join(tree.path, "scratch.tmp")
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write("temporary")
        os.remove(scratch)
        assert tree.diff().files_changed == 0


def test_new_files_are_counted(repo):
    with Worktree(repo, "demo") as tree:
        with open(os.path.join(tree.path, "added.py"), "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        stat = tree.diff()
        assert stat.files_changed == 1
        assert stat.paths == ("added.py",)


def test_two_attempts_do_not_collide(repo):
    """Regression. Branch names used int(time.time()), which has one-second
    resolution, so `attempts: 3` back to back built the same name three times
    and the second attempt refused to start. Found on the first real run."""
    with Worktree(repo, "demo", ordinal=1) as first:
        with Worktree(repo, "demo", ordinal=2) as second:
            assert first.branch != second.branch


def test_the_branch_outlives_the_worktree(repo):
    """A rejected attempt you can check out is evidence; a deleted one is a
    rumour."""
    tree = Worktree(repo, "demo")
    tree.create()
    name = tree.branch
    tree.remove()
    assert name in branches(repo)
    assert len(worktrees(repo)) == 1


def test_removal_is_idempotent(repo):
    tree = Worktree(repo, "demo")
    tree.create()
    tree.remove()
    tree.remove()
    assert len(worktrees(repo)) == 1


# -- the gate ---------------------------------------------------------------


def gate(repo, prompt, extra_checks=""):
    job = parse_job(spec_text("demo", prompt, extra_checks=extra_checks))
    with Worktree(repo, "demo") as tree:
        if prompt:
            tree.run(prompt, timeout=120)
        return verify(job, tree)


def test_work_that_passes_its_checks_is_accepted(repo):
    verdict = gate(repo, APPEND_SUB, '  - must_touch: ["app.py"]\n')
    assert verdict.passed, verdict.summary()


def test_work_that_breaks_the_tests_is_rejected(repo):
    verdict = gate(repo, BREAK_TESTS)
    assert not verdict.passed
    failure = next(r for r in verdict.results if not r.passed)
    assert failure.kind == "run"
    # The detail must be actionable on its own, not "tests failed".
    assert "test_add" in failure.detail


def test_a_forbidden_path_is_rejected_and_named(repo):
    verdict = gate(repo, TOUCH_CI, '  - must_not_touch: [".github/**"]\n')
    assert not verdict.passed
    failure = next(r for r in verdict.results if r.kind == "must_not_touch")
    assert ".github/ci.yml" in failure.detail


def test_a_structural_failure_skips_the_expensive_checks(repo):
    """Cheap rejection should not wait for a test suite to agree."""
    verdict = gate(repo, TOUCH_CI, '  - must_not_touch: [".github/**"]\n')
    run_check = next(r for r in verdict.results if r.kind == "run")
    assert not run_check.passed
    assert "not run" in run_check.detail


def test_doing_nothing_does_not_pass(repo):
    """An agent that changes nothing still leaves a green test suite, so
    without `must_touch` an idle run looks exactly like a successful one."""
    verdict = gate(repo, 'python -c "pass"', '  - must_touch: ["app.py"]\n')
    assert not verdict.passed
    assert any(r.kind == "must_touch" and not r.passed for r in verdict.results)


def test_a_check_that_cannot_run_is_a_failure_not_a_skip(repo):
    job = parse_job(
        "name: d\nprompt: p\nadapter: shell\nchecks:\n  - run: this-command-does-not-exist\n"
    )
    with Worktree(repo, "demo") as tree:
        verdict = verify(job, tree)
    assert not verdict.passed


def test_an_empty_verdict_is_not_a_pass():
    """`spec` refuses jobs with no checks, so an empty result list here means
    verification never ran -- a crash, a timeout. Reporting that as success is
    the hole this whole project exists to close."""
    assert Verdict().passed is False


# -- the runner -------------------------------------------------------------


def test_a_good_job_passes_every_attempt(repo):
    job = parse_job(
        spec_text("good", APPEND_SUB, attempts=2, extra_checks='  - must_touch: ["app.py"]\n')
    )
    outcome = run_job(job, root=repo)
    assert outcome.state == "passed"
    assert outcome.rate == "2/2"
    assert all(a.branch for a in outcome.attempts)


def test_a_broken_job_is_not_accepted(repo):
    outcome = run_job(parse_job(spec_text("bad", BREAK_TESTS)), root=repo)
    assert outcome.state == "failed"
    assert outcome.passed is False
    assert "test_add" in outcome.attempts[0].verdict.summary()


def test_a_partial_pass_rate_is_reported_as_such(repo):
    """Agents are stochastic. Reporting the last attempt turns a coin flip
    into a fact, so the rate is the headline."""
    from runproof.runner import AttemptOutcome, RunOutcome

    outcome = RunOutcome(run_id=1, job="flaky")
    outcome.attempts = [
        AttemptOutcome(1, True, "b1", None, None, 1.0),
        AttemptOutcome(2, False, "b2", None, None, 1.0),
        AttemptOutcome(3, True, "b3", None, None, 1.0),
    ]
    assert outcome.rate == "2/3"
    assert "not the same as working" in outcome.summary()


def test_everything_is_recorded_even_for_a_failure(repo):
    run_job(parse_job(spec_text("bad", BREAK_TESTS)), root=repo)
    with Store.for_repository(repo) as store:
        runs = store.recent_runs()
        assert len(runs) == 1 and runs[0].state == "failed"
        attempts = store.attempts(runs[0].id)
        assert len(attempts) == 1 and attempts[0].state == "rejected"
        assert any(not c["passed"] for c in store.checks(attempts[0].id))


def test_a_run_leaves_no_worktree_behind(repo):
    run_job(parse_job(spec_text("good", APPEND_SUB)), root=repo)
    assert len(worktrees(repo)) == 1


def test_the_repository_the_user_sits_in_is_unchanged_by_a_whole_run(repo):
    before = open(os.path.join(repo, "app.py"), encoding="utf-8").read()
    run_job(parse_job(spec_text("good", APPEND_SUB)), root=repo)
    run_job(parse_job(spec_text("bad", BREAK_TESTS)), root=repo)
    assert open(os.path.join(repo, "app.py"), encoding="utf-8").read() == before


def test_repository_root_finds_the_top_from_a_subdirectory(repo):
    nested = os.path.join(repo, "a", "b")
    os.makedirs(nested)
    assert os.path.normpath(repository_root(nested)) == os.path.normpath(repo)


def test_a_single_rejected_attempt_is_not_reported_as_a_tally(repo):
    """`0 of 1 attempts passed` is the same plural slip as the pass side, and
    it reads as arithmetic when there is nothing to count."""
    outcome = run_job(parse_job(spec_text("bad", BREAK_TESTS)), root=repo)
    assert outcome.summary().startswith("the single attempt was rejected")
    assert "0 of 1 attempts" not in outcome.summary()


def test_the_headline_does_not_recite_the_whole_failure(repo):
    """A rejected run printed the full pytest transcript twice -- once in the
    headline and once under the check that produced it, three lines below."""
    outcome = run_job(parse_job(spec_text("bad", BREAK_TESTS)), root=repo)
    verdict = outcome.attempts[0].verdict
    failure = verdict.failures[0]
    assert len(failure.detail) > SUMMARY_DETAIL, "fixture no longer produces a long failure"
    assert len(verdict.summary()) < len(failure.detail)
    # Still says enough to act on without opening anything.
    assert "test_add" in verdict.summary()
