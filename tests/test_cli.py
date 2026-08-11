"""Tests for the command line, weighted at the exit codes.

The exit code is the part other software depends on, and this tool draws a
distinction most do not: `1` means "ran fine, the work was not acceptable",
which is a normal outcome rather than an error. Collapsing that into `2` would
make every rejected agent run look like a crashed tool, and collapsing it into
`0` would make a CI check useless. So the codes get more attention here than
the rendering does.
"""

import json
import os

from conftest import APPEND_SUB, BREAK_TESTS, spec_text
from runproof.cli import EXIT_ERROR, EXIT_OK, EXIT_REJECTED, main


def write_spec(repo, name, prompt, extra=""):
    path = os.path.join(repo, f"{name}.yaml")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(spec_text(name, prompt, extra_checks=extra))
    return path


def test_accepted_work_exits_zero(repo, capsys):
    spec = write_spec(repo, "good", APPEND_SUB, '  - must_touch: ["app.py"]\n')
    assert main(["-C", repo, "--no-color", "run", spec]) == EXIT_OK
    assert "PASSED" in capsys.readouterr().out


def test_rejected_work_exits_one_and_says_why(repo, capsys):
    """Not an error -- the tool did exactly what it was asked. But it must not
    look like success to a shell script either."""
    spec = write_spec(repo, "bad", BREAK_TESTS)
    assert main(["-C", repo, "--no-color", "run", spec]) == EXIT_REJECTED
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "test_add" in out, "the report must name the failing test, not just say it failed"
    assert "Nothing was merged" in out


def test_a_broken_spec_exits_two_rather_than_one(repo, capsys):
    path = os.path.join(repo, "broken.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("name: x\nprompt: p\nchekcs:\n  - run: pytest\n")
    assert main(["-C", repo, "--no-color", "run", path]) == EXIT_ERROR
    assert "chekcs" in capsys.readouterr().err


def test_an_agent_process_failure_exits_two_even_when_repo_checks_pass(repo, capsys):
    spec = write_spec(repo, "agent-error", "runproof-command-that-does-not-exist")

    assert main(["-C", repo, "--no-color", "run", spec]) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "ERROR attempt" in out
    assert "exit" in out


def test_running_outside_a_repository_exits_two(tmp_path, capsys):
    assert main(["-C", str(tmp_path), "run", "nope.yaml"]) == EXIT_ERROR
    assert "not inside a git repository" in capsys.readouterr().err


def test_json_output_is_valid_and_carries_the_evidence(repo, capsys):
    spec = write_spec(repo, "bad", BREAK_TESTS)
    main(["-C", repo, "--json", "run", spec])
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["rate"] == "0/1"
    checks = payload["attempts"][0]["verdict"]["results"]
    assert any(not c["passed"] and "test_add" in c["detail"] for c in checks)


def test_status_lists_runs_newest_first(repo, capsys):
    main(["-C", repo, "--no-color", "run", write_spec(repo, "good", APPEND_SUB)])
    main(["-C", repo, "--no-color", "run", write_spec(repo, "bad", BREAK_TESTS)])
    capsys.readouterr()
    assert main(["-C", repo, "--no-color", "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.index("bad") < out.index("good")


def test_doctor_exits_nonzero_when_nothing_is_dispatching(repo, capsys):
    """So that `runproof doctor` is a one-line monitoring check."""
    assert main(["-C", repo, "--no-color", "doctor"]) == EXIT_REJECTED
    assert "never ticked" in capsys.readouterr().out


def test_doctor_is_green_once_the_dispatcher_has_run(repo, capsys):
    main(["-C", repo, "--no-color", "tick"])
    capsys.readouterr()
    assert main(["-C", repo, "--no-color", "doctor"]) == EXIT_OK
    assert "alive" in capsys.readouterr().out


def test_tick_with_nothing_due_says_so_and_still_counts(repo, capsys):
    assert main(["-C", repo, "--no-color", "tick"]) == EXIT_OK
    assert "Nothing was due" in capsys.readouterr().out


def test_scheduling_explains_that_nothing_dispatches_by_itself(repo, capsys):
    """There is no daemon, and a user who assumes otherwise gets the failure
    this package was written after."""
    spec = write_spec(repo, "good", APPEND_SUB)
    assert main(["-C", repo, "--no-color", "schedule", "add", spec, "--every", "3600"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "runproof tick" in out
    assert "cron" in out


def test_schedules_can_be_listed_and_removed(repo, capsys):
    spec = write_spec(repo, "good", APPEND_SUB)
    main(["-C", repo, "--no-color", "schedule", "add", spec, "--every", "3600"])
    capsys.readouterr()
    main(["-C", repo, "--no-color", "schedule", "list"])
    assert "good" in capsys.readouterr().out

    main(["-C", repo, "--no-color", "schedule", "remove", "good"])
    capsys.readouterr()
    main(["-C", repo, "--no-color", "schedule", "list"])
    assert "No schedules" in capsys.readouterr().out


def test_show_returns_the_full_record_of_one_run(repo, capsys):
    main(["-C", repo, "--no-color", "run", write_spec(repo, "bad", BREAK_TESTS)])
    capsys.readouterr()
    assert main(["-C", repo, "--json", "show", "1"]) == EXIT_REJECTED
    payload = json.loads(capsys.readouterr().out)
    assert payload["job"] == "bad"
    assert payload["attempts"][0]["checks"]


def test_show_of_a_missing_run_is_an_error(repo, capsys):
    assert main(["-C", repo, "show", "999"]) == EXIT_ERROR
    assert "no run 999" in capsys.readouterr().err


def test_prune_is_safe_to_run_when_there_is_nothing_to_prune(repo, capsys):
    assert main(["-C", repo, "--no-color", "prune"]) == EXIT_OK
    assert "Nothing to prune" in capsys.readouterr().out


def test_a_single_attempt_is_not_dressed_up_as_more(repo, capsys):
    """'all 1 attempts passed' both reads badly and overstates: one green
    attempt is the weakest evidence this tool produces."""
    main(["-C", repo, "--no-color", "run", write_spec(repo, "good", APPEND_SUB)])
    out = capsys.readouterr().out
    assert "the single attempt passed" in out
    assert "all 1 attempts" not in out
