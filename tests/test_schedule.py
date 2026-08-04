"""Tests for dispatch, and for being able to prove the dispatcher is alive.

This module exists because of a night when thirteen scheduled agent runs did
not fire and nothing recorded that they had not. So the tests here are less
about "does it dispatch" -- that part is easy -- and more about whether the
three states a scheduler can be in stay *distinguishable*:

* never woken up,
* awake with nothing due,
* dead while work piles up.

The first two look identical from the job list, which is exactly how the
original failure hid.
"""

import os
import time

import pytest

from conftest import APPEND_SUB, spec_text
from runproof.schedule import MIN_INTERVAL_SECONDS, TICK_STALE_SECONDS, Scheduler, doctor


@pytest.fixture
def scheduled(repo):
    """A repository with one valid job spec written to disk."""
    path = os.path.join(repo, "job.yaml")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(spec_text("nightly", APPEND_SUB, extra_checks='  - must_touch: ["app.py"]\n'))
    return repo, path


def test_a_schedule_can_be_added_and_read_back(scheduled):
    repo, spec = scheduled
    with Scheduler(repo) as scheduler:
        added = scheduler.add(spec, every_seconds=3600)
        assert added.job == "nightly"
        assert added.enabled
        assert scheduler.get("nightly").every_seconds == 3600


def test_an_invalid_spec_is_refused_when_scheduled_not_at_3am(scheduled, tmp_path):
    """A spec that cannot be parsed should fail while somebody is watching."""
    repo, _ = scheduled
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: x\nprompt: p\nchekcs:\n  - run: pytest\n", encoding="utf-8")
    with Scheduler(repo) as scheduler, pytest.raises(Exception, match="chekcs"):
        scheduler.add(str(broken), every_seconds=3600)


def test_an_absurdly_short_interval_is_refused(scheduled):
    """A runaway schedule spawns agents in a loop, which is expensive in a way
    that a typo should not be."""
    repo, spec = scheduled
    with Scheduler(repo) as scheduler, pytest.raises(ValueError, match="typo"):
        scheduler.add(spec, every_seconds=MIN_INTERVAL_SECONDS - 1)


def test_nothing_is_due_before_its_time(scheduled):
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        assert scheduler.due(now) == []
        assert len(scheduler.due(now + 3601)) == 1


def test_never_ticked_is_a_different_answer_from_nothing_due(scheduled):
    """The distinction the whole module exists for. From the job list alone
    these are identical, and that is how a dead scheduler hides."""
    repo, spec = scheduled
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600)

    never = doctor(repo)
    assert never.alive is False
    assert never.last_tick is None
    assert "never ticked" in never.summary()

    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.tick(now=now)
    idle = doctor(repo, now=now + 30)
    assert idle.alive is True
    assert "nothing overdue" in idle.summary()


def test_a_tick_is_recorded_even_when_nothing_was_due(scheduled):
    """The cheapest row in the database and the only one that answers the
    question anybody actually asks at 9am."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        assert scheduler.last_tick() is None
        scheduler.tick(now=now)
        assert scheduler.last_tick() == now


def test_due_work_is_dispatched_and_verified(scheduled):
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        outcomes = scheduler.tick(now=now + 3601)
    assert len(outcomes) == 1
    assert outcomes[0].state == "passed"
    assert outcomes[0].job == "nightly"


def test_a_dispatched_run_is_labelled_as_scheduled(scheduled):
    """So that 'did this run because of a timer or because I asked' is
    answerable months later."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        scheduler.tick(now=now + 3601)
        assert scheduler.store.recent_runs()[0].trigger == "schedule"


def test_a_dead_dispatcher_is_reported_with_numbers(scheduled):
    """Not a status light. 'Last tick 9 hours ago, 1 schedule overdue' is a
    diagnosis; a red cross is a rumour."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        scheduler.tick(now=now)

    health = doctor(repo, now=now + TICK_STALE_SECONDS + 3600)
    assert health.alive is False
    assert health.overdue and health.overdue[0].job == "nightly"
    assert "presumed dead" in health.summary()
    assert "overdue" in health.summary()


def test_a_backlog_is_caught_up_once_not_n_times(scheduled):
    """A scheduler down for six hours must not fire six runs the moment a
    laptop wakes: that is a herd of agents against one repository."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        outcomes = scheduler.tick(now=now + 6 * 3600)
        assert len(outcomes) == 1
        # The skipped windows are recorded rather than silently forgotten.
        notes = scheduler._connection.execute("SELECT note FROM ticks WHERE note != ''").fetchall()
        assert any("missed" in row["note"] for row in notes)


def test_a_clock_that_moved_backwards_does_not_read_as_healthy(scheduled):
    """Liveness is a comparison against the newest tick, so a future stamp
    would otherwise make a dead scheduler look permanently fine."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        scheduler.tick(now=now)

    health = doctor(repo, now=now - 3600)
    assert health.clock_skew is True
    assert health.alive is False
    assert "system clock" in health.summary()


def test_a_disabled_schedule_is_neither_due_nor_overdue(scheduled):
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        scheduler.set_enabled("nightly", False)
        assert scheduler.due(now + 99999) == []
        assert scheduler.get("nightly").overdue_by(now + 99999) == 0


def test_removing_a_schedule_stops_it(scheduled):
    repo, spec = scheduled
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600)
        scheduler.remove("nightly")
        assert scheduler.all() == []


def test_the_health_report_counts_in_english(scheduled):
    """One schedule is not '1 schedules', and one second is not '1 seconds'.
    The same class of slip that shipped in a sibling project."""
    repo, spec = scheduled
    now = time.time()
    with Scheduler(repo) as scheduler:
        scheduler.add(spec, every_seconds=3600, now=now)
        scheduler.tick(now=now)

    assert "1 second ago" in doctor(repo, now=now + 1).summary()
    assert "1 minutes" not in doctor(repo, now=now + 60).summary()

    # And on the other side of the liveness threshold, where the sentence
    # counts schedules as well as time.
    dead = doctor(repo, now=now + 4 * 3600).summary()
    assert "1 of 1 schedule is overdue" in dead
    assert "schedules is" not in dead


def test_a_fresh_tick_is_not_reported_as_zero_minutes_ago(scheduled):
    """`last tick 0 minutes ago` printed one line above `0 seconds ago` from
    the report -- two wordings of the same number, and the summary's reads
    like the clock has stopped."""
    repo, _ = scheduled
    with Scheduler(repo) as scheduler:
        scheduler.record_tick(now=time.time())
    summary = doctor(repo).summary()
    assert "0 minutes ago" not in summary
    assert "second" in summary


def test_a_long_gap_climbs_to_hours_and_days(scheduled):
    """`last ticked 2880 minutes ago` is arithmetically perfect and makes the
    reader do the division. At 9am the useful answer is 'two days'."""
    repo, _ = scheduled
    now = time.time()
    # Oldest first: liveness reads MAX(at), so a tick inserted behind the
    # newest one would not move the answer.
    for gap, expected in ((2 * 86400, "2 days ago"), (3 * 3600, "3 hours ago")):
        with Scheduler(repo) as scheduler:
            scheduler.record_tick(now=now - gap)
        assert expected in doctor(repo, now=now).summary()
