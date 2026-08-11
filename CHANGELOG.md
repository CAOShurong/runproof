# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-08-11

### Security

- Replace the YAML-subset parser's overlapping regular-expression repeats with
  bounded string parsing, removing CodeQL's polynomial-ReDoS findings on job
  specifications without adding a runtime dependency.
- Replace the README test-count regular expression with a linear suffix scan. A
  50,000-digit near-match that previously took more than 25 seconds is covered by
  a regression test.
- Pin every GitHub Action in CI and the release pipeline to an immutable commit,
  including the PyPI publisher and GitHub release action.

### Maintenance

- Add weekly Dependabot updates for pinned GitHub Actions.

## [0.2.0] - 2026-08-11

### Security

- Replace Claude Code's `bypassPermissions` default with fail-closed `dontAsk`
  permissions, a fixed repository-scoped read/edit tool surface, disabled settings
  inheritance, strict MCP configuration, no session persistence, no browser
  integration, and no slash-command skills.
- Run Codex with an explicit `workspace-write` sandbox and ephemeral session while
  ignoring user configuration and execution-policy rules.
- Pass agent tasks through standard input and resolve Windows npm command shims to
  their native executable without `cmd.exe`, preventing task text or arguments from
  becoming shell syntax.

### Fixed

- Treat a failed, timed-out, or unauthenticated adapter process as an error even if
  the unchanged repository still passes every declared check.
- Require every requested attempt to pass before the run returns success; a partial
  pass rate remains evidence of stochastic behavior, not an accepted run.
- Preserve the verifier's evidence after an adapter error while returning exit code
  `2`, so automation can distinguish infrastructure failure from rejected work.
- Include errored attempts in dashboard pass-rate totals instead of silently
  dropping unavailable or crashed runs from the denominator.

### Documentation

- Document the worktree, host-sandbox, executable-spec, and hostile-repository
  boundaries in `SECURITY.md` and the README.

## [0.1.0] — 2026-08-04

First release.

### Added

- `runproof run <spec>` executes a job in an isolated `git worktree` and
  accepts the result **only** if every declared check passes. The agent's own
  summary of its work is recorded for humans and never consulted by the gate.
- Six check kinds — `run`, `changed_files`, `diff_lines`, `must_not_touch`,
  `must_touch`, `file_contains` — each quoting its evidence rather than
  summarising it, so a rejection can be acted on without opening a log.
- `attempts: N` runs a job independently N times and reports the **pass rate**.
  Agents are stochastic; reporting the last attempt turns a coin flip into a
  fact.
- Isolation that refuses to degrade. Every attempt gets its own worktree on its
  own branch off `HEAD`; if isolation cannot be obtained the run does not start.
  Nothing is ever merged, and the branch outlives the worktree so a rejected
  attempt can still be checked out.
- The diff is measured with `git diff --numstat` against the base commit rather
  than by counting files written, so an agent that creates a file and deletes
  it again is correctly reported as having changed nothing.
- Adapters for the `claude` CLI, the `codex` CLI, and a plain shell command.
  The core treats all three as "something that edits a worktree", so comparing
  two agents on one job is a configuration change rather than a fork.
- Append-only SQLite history in `.runproof/runs.db`, with a heartbeat written
  before an agent starts. A process that dies leaves a `running` row with a
  stale timestamp, which `runproof doctor` finds.
- A scheduler with no daemon. `runproof tick` dispatches whatever is due and
  **writes a row even when nothing was** — the single decision that separates
  "the scheduler is alive and idle" from "the scheduler is dead". Missed
  windows are caught up once, not N times, and the skipped ones are recorded
  with a reason.
- `runproof doctor`, which answers "did the overnight work run, and if not, why
  not?" in facts with numbers attached, and exits non-zero so it can be a
  one-line monitoring check.
- `runproof dashboard`, a self-contained HTML summary — inline CSS, no script,
  nothing fetched — that leads with **what is not happening**: schedules that
  stopped firing and runs that went quiet appear even though they have nothing
  to put in a table. Pass rate per job is over every attempt ever recorded, not
  the last result.
- `status`, `show`, `prune`, `schedule add|list|remove`, and `--json` on all of
  them.
- Exit codes that distinguish `1` (ran fine, work not acceptable) from `2`
  (could not run). Folding them together makes every rejected agent run look
  like a crashed tool.
- A restricted YAML parser with **no silent path**: nested mappings, sequences,
  scalars and `|` blocks are supported and tested; anchors, aliases, tags,
  folded scalars and tabs raise a `SpecError` naming the line. This is what
  buys zero runtime dependencies.
- Terminal report with an ASCII fallback for consoles that cannot encode the
  drawing characters, chosen by asking the stream rather than guessing from the
  platform.

### Deliberately absent

- **No merge, ever.** Not with a flag. Accepted work is left on a branch and
  taking it is one `git merge` that a human types.
- **No sandbox.** A worktree isolates the agent from your working tree, not
  from your machine. Run runproof inside the container you were going to use
  anyway if that matters.
- **No daemon.** `tick` inherits cron's reliability, and more to the point its
  visibility when it stops.

### Known limitations

- The gate is exactly as good as the spec. A job whose only check is
  `run: pytest` will accept a diff that deletes the feature and its test
  together. `must_touch` and `changed_files: {min: N}` exist for this reason.
- The YAML subset refuses real YAML files. For job specs — a dozen lines of
  mapping — that is the right side of the trade. For anything larger it would
  not be.
- The `codex` adapter is thin. It exists so that the core has more than one
  real agent to prove it plays no favourites; it has had far less use than the
  `claude` and `shell` paths.
