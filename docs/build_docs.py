"""Generate the README figures by actually running the tool, and check them.

Two rules, carried over from a sibling project where breaking either one was
the most embarrassing defect in the release.

**Nothing here is typed by hand.** Every number and every verdict in the
generated section of the README comes from running `runproof` against a
repository built in a temporary directory. A README quoting a result the code
no longer produces is precisely the failure this project argues against, and
it is the easiest one to commit by accident. `--check` rebuilds everything and
fails if the committed README differs, so CI catches a stale claim the same
way it catches a failing test.

**The example repository is generated, not committed.** It is a real git
repository with a real passing test suite, created, run against, and thrown
away. Four jobs are dispatched at it and exactly one is accepted -- and the
three rejections are the point, so they are rendered in full.

The scheduler figure is the one place with synthetic input: the tick
timestamps are supplied rather than waited for, because a figure that took
thirteen hours to build would never be regenerated. Everything acting on those
timestamps -- the catch-up arithmetic, the liveness test, `doctor` -- is the
shipped code path, unmodified.

Usage:
    python docs/build_docs.py           # regenerate figures and README
    python docs/build_docs.py --check   # fail if anything is out of date
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from runproof.runner import run_job  # noqa: E402
from runproof.schedule import Scheduler, doctor  # noqa: E402
from runproof.spec import parse_job  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: House palette, shared with the sibling projects. Dark, so a figure sits
#: next to terminal output in a README without either looking pasted in.
INK = (16, 18, 24)
PAPER = (232, 234, 240)
MUTED = (120, 126, 142)
GRID = (44, 48, 60)
FAIL = (232, 118, 92)
PASS = (124, 196, 140)
LIVE = (104, 172, 216)

BEGIN = "<!-- BEGIN GENERATED -->"
END = "<!-- END GENERATED -->"

APP = "def add(a, b):\n    return a + b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


# --------------------------------------------------------------------------
# A real repository, and four real jobs
# --------------------------------------------------------------------------


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def example_repository(root: str) -> None:
    """A git repository with a passing test suite and nothing else.

    Small on purpose. The argument is not that runproof copes with a large
    codebase; it is that on the smallest possible one, three of four plausible
    agent outcomes are still unacceptable and only checks can tell you which.
    """
    os.makedirs(root, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
    _git("config", "user.email", "docs@example.com", cwd=root)
    _git("config", "user.name", "Docs", cwd=root)
    for name, body in (("app.py", APP), ("test_app.py", TEST), (".gitignore", ".runproof/\n")):
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "initial", cwd=root)


def _py(body: str) -> str:
    """A shell one-liner, written without a backslash in sight.

    The shell adapter stands in for an agent here. Substituting a real one
    would make the figures depend on a paid, non-deterministic third party --
    and would prove nothing extra, because the gate never learns which adapter
    produced the diff.
    """
    return f'python -c "{body}"'


#: Four jobs, in the order a person would think of them. The first is what
#: everybody imagines happens; the other three are what actually happens.
JOBS = [
    (
        "add-subtract",
        "Add a `sub` function next to `add`, and keep the suite green.",
        _py(
            "import io; io.open('app.py','a').write("
            "chr(10) + 'def sub(a, b):' + chr(10) + '    return a - b' + chr(10))"
        ),
        '  - must_touch: ["app.py"]\n',
    ),
    (
        "break-tests",
        "The agent 'simplified' `add` and did not run the suite.",
        _py("import io; io.open('app.py','w').write('def add(a, b): return a * b' + chr(10))"),
        "",
    ),
    (
        "touch-ci",
        "The agent decided the build configuration was the problem.",
        _py(
            "import io, os; os.makedirs('.github', exist_ok=True); "
            "io.open('.github/ci.yml','w').write('# adjusted' + chr(10))"
        ),
        '  - must_not_touch: [".github/**"]\n',
    ),
    (
        "sprawl",
        "The agent refactored the whole package while it was in there.",
        _py(
            "import io"
            + "".join(f"; io.open('mod{n}.py','w').write('x = {n}' + chr(10))" for n in range(9))
        ),
        "",
    ),
]


def spec_text(name: str, prompt: str, extra_checks: str) -> str:
    return (
        f"name: {name}\n"
        "prompt: |\n"
        f"  {prompt}\n"
        "adapter: shell\n"
        "checks:\n"
        "  - run: python -m pytest -q\n"
        "  - changed_files: {max: 3}\n"
        f"{extra_checks}"
    )


def run_the_jobs(root: str) -> list:
    outcomes = []
    for name, _, command, extra in JOBS:
        job = parse_job(spec_text(name, command, extra), source=f"{name}.yaml")
        outcomes.append(run_job(job, root=root))
    return outcomes


def deciding_check(outcome):
    """The check that settled it, and its own words.

    For a rejection this is the first failure -- the ordering in `verify` puts
    structural checks first for exactly this reason, so the answer is the
    cheapest true one rather than whichever ran last.
    """
    verdict = outcome.attempts[0].verdict
    if verdict is None:
        return "run", outcome.attempts[0].error or "no verdict"
    if verdict.failures:
        first = verdict.failures[0]
        return first.kind, first.detail
    return "all", f"{len(verdict.results)} of {len(verdict.results)} checks passed"


# --------------------------------------------------------------------------
# A scheduler that dies, and is caught doing it
# --------------------------------------------------------------------------

HOUR = 3600.0
#: An arbitrary fixed origin. Only differences are ever displayed, so the
#: figure does not depend on a timezone or on when it was built.
EVENING = 1_000_000.0
EVERY = 1800


def _clock(when: float) -> str:
    """A wall-clock label for a synthetic timestamp, with `EVENING` at 20:00.

    Deliberately not `time.localtime`: the figure would then depend on the
    timezone of whichever machine last regenerated it.
    """
    minutes = int(round((when - EVENING) / 60)) + 20 * 60
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


class _StubRun:
    """Stands in for a dispatched run in the scheduler figure.

    `tick()` is the code under illustration; what it hands the work to is not.
    Dispatching four real agent runs to draw a timeline would make the figure
    cost more than the tool.
    """

    passed = True

    def __init__(self, *_, **__):
        pass


def heartbeat_history(root: str):
    """Ten ticks, an eight-and-a-half hour hole, and what `doctor` says.

    This is the night the project is named after, replayed through the shipped
    code with the clock supplied rather than waited for.
    """
    spec = os.path.join(root, "nightly.yaml")
    with open(spec, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(spec_text("nightly", 'python -c "pass"', ""))

    ticks = []
    with Scheduler(root) as scheduler:
        scheduler.add(spec, every_seconds=EVERY, now=EVENING)
        # 20:30 through 00:30, every half hour, exactly as cron would.
        for index in range(1, 11):
            at = EVENING + index * EVERY
            scheduler.tick(now=at, runner=_StubRun)
            ticks.append(at)

    # 09:00. Nine hours after the last tick, which is when somebody looks.
    morning = EVENING + 13 * HOUR
    dead = doctor(root, now=morning)

    with Scheduler(root) as scheduler:
        scheduler.tick(now=morning, runner=_StubRun)
        catch_up = scheduler._connection.execute(
            "SELECT note FROM ticks WHERE note != '' ORDER BY id DESC LIMIT 1"
        ).fetchone()["note"]
    alive = doctor(root, now=morning + 60)

    return {
        "ticks": ticks,
        "gap_start": ticks[-1],
        "morning": morning,
        "dead": dead,
        "alive": alive,
        "catch_up": catch_up,
    }


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------


def dashboard_sample(root: str) -> str:
    """A deterministic dashboard, written to `docs/dashboard.html`.

    Every timestamp is supplied rather than taken from the clock, and the
    repository path is normalised afterwards, so the committed file is
    byte-stable and `--check` can hold it to the same standard as the README.
    Without that it would drift on every regeneration and the check would be
    trained away within a week.

    The shape it illustrates is the one worth illustrating: a job that passes
    most of the time but not always, a job that has never passed, and a
    schedule that stopped firing -- which is the state a dashboard exists to
    make visible and the one it is most tempted to render as an empty page.
    """
    from runproof.dashboard import render_dashboard
    from runproof.store import Store

    day = 86400.0
    now = EVENING + 3 * day
    # (job, trigger, results oldest-first, when the newest one ran)
    fixture = [
        (
            "nightly-typing",
            "schedule",
            [True, True, False, True, True, True, True, False, True],
            now - day,
        ),
        ("upgrade-requests", "manual", [False, False, False], now - 3 * HOUR),
    ]

    spec = os.path.join(root, "nightly.yaml")
    with open(spec, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(spec_text("nightly-typing", 'python -c "pass"', ""))
    job = parse_job(spec_text("nightly-typing", 'python -c "pass"', ""))

    with Store.for_repository(root) as store:
        for name, trigger, results, newest in fixture:
            # Spaced backwards from the newest, so nothing lands in the future
            # -- the first version ran the second job forward from where the
            # first stopped and produced four runs stamped "0s ago" that were
            # actually tomorrow.
            step = day if trigger == "schedule" else HOUR
            clock = newest - (len(results) - 1) * step - step
            for passed in results:
                clock += step
                run_job_row = store.start_run(
                    type(job)(**{**job.__dict__, "name": name}), trigger=trigger, now=clock
                )
                attempt = store.start_attempt(run_job_row, 1, now=clock)
                store.record_check(attempt, "run", passed, "`python -m pytest -q`", now=clock)
                store.finish_attempt(
                    attempt,
                    "passed" if passed else "rejected",
                    branch=f"runproof/{name}-1-abc123",
                    files_changed=2,
                    diff_lines=48,
                    detail="1 of 1 checks passed"
                    if passed
                    else "1 of 1 checks failed: `python -m pytest -q` exit 1: "
                    "FAILED tests/test_client.py::test_retry",
                    now=clock + 90,
                )
                store.finish_run(
                    run_job_row,
                    "passed" if passed else "failed",
                    "the single attempt passed"
                    if passed
                    else "the single attempt was rejected: 1 of 1 checks failed",
                    now=clock + 90,
                )

    # A schedule that stopped firing: registered, ticked once, then silence.
    with Scheduler(root) as scheduler:
        scheduler.add(spec, every_seconds=int(day), now=now - 2 * day)
        scheduler.record_tick(now=now - 2 * day)

    page = render_dashboard(root, now=now)
    # The only substitution. The real path is a temporary directory whose name
    # changes every run, and a sample that changes every run is a sample the
    # check cannot hold to anything.
    return page.replace(_e_path(root), "~/work/example")


def _e_path(root: str) -> str:
    import html as _html

    return _html.escape(root, quote=True)


def _font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "arial.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def figure_gate(outcomes, path: str) -> None:
    """Four jobs, one accepted, and the check that decided each one."""
    from PIL import Image, ImageDraw

    width, row, top = 980, 88, 108
    height = top + row * len(outcomes) + 34
    image = Image.new("RGB", (width, height), INK)
    draw = ImageDraw.Draw(image)
    title, label, mono, small = _font(24), _font(18), _font(15), _font(14)

    accepted = sum(1 for o in outcomes if o.passed)
    draw.text((34, 28), "What the gate does with four plausible outcomes", font=title, fill=PAPER)
    draw.text(
        (34, 64),
        f"{len(outcomes)} jobs run in isolated worktrees, {accepted} accepted. "
        "Every rejection names the check that made it.",
        font=small,
        fill=MUTED,
    )

    for index, outcome in enumerate(outcomes):
        y = top + index * row
        kind, detail = deciding_check(outcome)
        colour = PASS if outcome.passed else FAIL

        lines = _wrap(draw, detail, mono, width - 260)[:2]
        draw.rectangle([34, y, 38, y + 34 + 21 * len(lines)], fill=colour)
        draw.text((56, y + 2), outcome.job, font=label, fill=PAPER)

        badge = "ACCEPTED" if outcome.passed else "REJECTED"
        badge_width = draw.textlength(badge, font=small) + 20
        draw.rectangle([width - 34 - badge_width, y + 2, width - 34, y + 24], fill=colour)
        draw.text((width - 24 - badge_width, y + 5), badge, font=small, fill=INK)

        draw.text((56, y + 32), kind, font=mono, fill=colour)
        for line_index, line in enumerate(lines):
            draw.text((190, y + 32 + line_index * 21), line, font=mono, fill=MUTED)

    draw.text(
        (34, height - 26),
        "Nothing was merged. Every attempt, accepted or not, is left on its own branch.",
        font=small,
        fill=MUTED,
    )
    image.save(path)


def figure_heartbeat(history, path: str) -> None:
    """The night nothing ran, drawn from the rows the scheduler wrote."""
    from PIL import Image, ImageDraw

    width, height = 980, 300
    image = Image.new("RGB", (width, height), INK)
    draw = ImageDraw.Draw(image)
    title, label, small = _font(24), _font(17), _font(14)

    draw.text((34, 28), "The night nothing ran", font=title, fill=PAPER)
    draw.text(
        (34, 62),
        "Every wake-up writes a row, including the ones with no work. "
        "That single row is what makes the hole visible.",
        font=small,
        fill=MUTED,
    )

    left, right, axis = 60, width - 60, 150
    span = (history["morning"] + HOUR) - history["ticks"][0]

    def x_of(when: float) -> float:
        return left + (right - left) * (when - history["ticks"][0]) / span

    draw.rectangle([left, axis - 1, right, axis + 1], fill=GRID)

    gap_left, gap_right = x_of(history["gap_start"]), x_of(history["morning"])
    draw.rectangle([gap_left, axis - 26, gap_right, axis + 26], fill=(46, 26, 26))
    draw.text(
        (gap_left + 14, axis - 20),
        f"no tick for {round(history['dead'].seconds_since_tick / 60)} minutes",
        font=label,
        fill=FAIL,
    )

    for when in history["ticks"]:
        x = x_of(when)
        draw.rectangle([x - 3, axis - 13, x + 3, axis + 13], fill=LIVE)
    recovery = x_of(history["morning"])
    draw.rectangle([recovery - 4, axis - 17, recovery + 4, axis + 17], fill=PASS)

    # Derived, not typed. The tick times moved once while this file was being
    # written and the axis labels did not, which is the same class of mistake
    # `--check` exists to catch in the README.
    draw.text((left - 6, axis + 34), _clock(history["ticks"][0]), font=small, fill=MUTED)
    draw.text((gap_left - 20, axis + 34), _clock(history["gap_start"]), font=small, fill=MUTED)
    draw.text((recovery - 20, axis + 34), _clock(history["morning"]), font=small, fill=MUTED)

    for offset, (mark, colour, text) in enumerate(
        (
            ("doctor at 09:00", FAIL, history["dead"].summary()),
            ("after one tick", PASS, history["alive"].summary()),
        )
    ):
        y = 208 + offset * 44
        draw.text((34, y), mark, font=small, fill=colour)
        for line_index, line in enumerate(_wrap(draw, text, small, width - 240)):
            if line_index > 1:
                break
            draw.text((190, y + line_index * 19), line, font=small, fill=MUTED)

    image.save(path)


# --------------------------------------------------------------------------
# The generated section
# --------------------------------------------------------------------------


def _cell(text: str, width: int = 96) -> str:
    """One markdown table cell.

    The pipe is load-bearing: a quoted pytest failure contains `|` where the
    reporter joined lines, and an unescaped one silently splits the row into
    extra columns. The first version of this file shipped a four-column table
    that rendered as seven.
    """
    text = text if len(text) <= width else text[:width].rstrip() + " …"
    return text.replace("|", "\\|")


def generated_section(outcomes, history) -> str:
    rows = []
    for outcome, (_, story, _, _) in zip(outcomes, JOBS):
        kind, detail = deciding_check(outcome)
        verdict = "accepted" if outcome.passed else f"**rejected** by `{kind}`"
        rows.append(f"| `{outcome.job}` | {story} | {verdict} | {_cell(detail)} |")

    accepted = sum(1 for o in outcomes if o.passed)
    missed = int(re.search(r"skipped (\d+)", history["catch_up"]).group(1))
    return f"""{BEGIN}

### Four plausible outcomes, one of them acceptable

Built by `docs/build_docs.py`, which creates a git repository with a passing
test suite, dispatches the four jobs below at it, and renders whatever comes
back. Nothing in this section is typed by hand; `--check` fails the build if
any of it drifts from what the code produces.

![What the gate does with four plausible outcomes](docs/gate.png)

| job | what the agent did | verdict | the check that decided it |
| --- | --- | --- | --- |
{chr(10).join(rows)}

**{accepted} of {len(outcomes)} accepted.** Every one of the
{len(outcomes) - accepted} rejections leaves a green-looking agent transcript
behind it — the command exited zero, the files were written, the summary would
have read well. The diff is the only thing that was consulted.

### The failure this was written after

![The night nothing ran](docs/heartbeat.png)

A scheduler that dispatches nothing looks exactly like a scheduler with
nothing to dispatch. So every wake-up writes a row whether or not there was
work, and `runproof doctor` reads it back:

```console
$ runproof doctor
  ✗ {history["dead"].summary()}
```

One tick later, the same command, and the missed windows accounted for rather
than fired:

```console
$ runproof tick && runproof doctor
  ✓ {history["alive"].summary()}
```

```text
{history["catch_up"]}
```

{missed} missed windows, **one** catch-up run. A scheduler that fired the whole
backlog on waking would put {missed} agents on one repository at once, which is
a worse morning than the one it was recovering from.

{END}"""


def _write_or_compare(path: str, content: str, check: bool) -> str | None:
    """Write a generated file, or report that the committed one has drifted."""
    if not check:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return None
    if not os.path.isfile(path):
        return f"{os.path.basename(path)} is missing. Run: python docs/build_docs.py"
    with open(path, encoding="utf-8") as handle:
        if handle.read() != content:
            return f"{os.path.basename(path)} is out of date. Run: python docs/build_docs.py"
    return None


def check_test_count() -> str | None:
    """Whether the test count quoted in the README is still true.

    Counting `def test_` rather than running pytest keeps `--check` fast, and
    the two agree because every test here is a plain function. A README
    quoting a test count the suite no longer has is a small version of exactly
    what this project complains about.
    """
    import glob

    actual = 0
    for path in glob.glob(os.path.join(ROOT, "tests", "test_*.py")):
        with open(path, encoding="utf-8") as handle:
            actual += sum(1 for line in handle if line.startswith("def test_"))

    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()

    stated = [int(n) for n in re.findall(r"(\d+) tests", readme)]
    wrong = [str(n) for n in stated if n != actual]
    if wrong:
        return f"README says {', '.join(wrong)} tests; there are {actual}"
    return None


def build(check: bool) -> int:
    with tempfile.TemporaryDirectory() as workspace:
        repository = os.path.join(workspace, "example")
        example_repository(repository)
        outcomes = run_the_jobs(repository)

        clock = os.path.join(workspace, "clock")
        example_repository(clock)
        history = heartbeat_history(clock)

        board = os.path.join(workspace, "board")
        example_repository(board)
        page = dashboard_sample(board)

        target = workspace if check else HERE
        figure_gate(outcomes, os.path.join(target, "gate.png"))
        figure_heartbeat(history, os.path.join(target, "heartbeat.png"))
        section = generated_section(outcomes, history)

    stale_dashboard = _write_or_compare(os.path.join(HERE, "dashboard.html"), page, check)

    readme_path = os.path.join(ROOT, "README.md")
    with open(readme_path, encoding="utf-8") as handle:
        readme = handle.read()

    if BEGIN not in readme or END not in readme:
        print("README.md is missing the generated markers", file=sys.stderr)
        return 1

    head, _, rest = readme.partition(BEGIN)
    _, _, tail = rest.partition(END)
    rebuilt = head + section + tail

    if check:
        if rebuilt != readme:
            print("README.md is out of date. Run: python docs/build_docs.py", file=sys.stderr)
            return 1
        for stale in (stale_dashboard, check_test_count()):
            if stale:
                print(stale, file=sys.stderr)
                return 1
        print("README.md and docs/dashboard.html are up to date.")
        return 0

    with open(readme_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rebuilt)
    accepted = sum(1 for o in outcomes if o.passed)
    print(
        f"Wrote README.md, docs/gate.png and docs/heartbeat.png "
        f"({accepted} of {len(outcomes)} jobs accepted)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(build("--check" in sys.argv))
