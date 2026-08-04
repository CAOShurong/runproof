"""Tests for job specs and the YAML subset they are written in.

Weighted almost entirely at refusals. A spec that parses into something other
than what its author meant is worse than one that fails, because the failure is
loud and the misreading is silent -- and this parser is hand-rolled, which is
the situation where silent misreading is most likely and least excusable.
"""

import pytest

from runproof.spec import Job, SpecError, load_job, parse_job, parse_yaml

MINIMAL = "name: demo\nprompt: do the thing\nadapter: shell\nchecks:\n  - run: pytest\n"


# -- the parser -------------------------------------------------------------


def test_scalars_are_read_as_the_types_they_look_like():
    parsed = parse_yaml("a: 1\nb: 2.5\nc: yes\nd: no\ne: null\nf: text\ng: 'quoted: colon'\n")
    assert parsed == {
        "a": 1,
        "b": 2.5,
        "c": True,
        "d": False,
        "e": None,
        "f": "text",
        "g": "quoted: colon",
    }


def test_inline_mappings_and_sequences():
    parsed = parse_yaml("limits: {max: 10, min: 2}\npaths: [a.py, 'b c.py']\n")
    assert parsed["limits"] == {"max": 10, "min": 2}
    assert parsed["paths"] == ["a.py", "b c.py"]


def test_block_scalars_keep_their_indentation():
    """Regression. Lines are stripped while pre-parsing and the leading spaces
    were never rebuilt, so every `prompt: |` came back left-aligned. For a tool
    whose entire input is prompts, silently flattening code inside one is not a
    cosmetic bug -- it changes what the agent is asked to do.
    """
    parsed = parse_yaml("prompt: |\n  def f():\n      return 1\n  done\n")
    assert parsed["prompt"] == "def f():\n    return 1\ndone\n"


def test_a_colon_inside_a_block_scalar_is_not_a_key():
    parsed = parse_yaml("prompt: |\n  key: not a key\nadapter: shell\n")
    assert parsed["prompt"] == "key: not a key\n"
    assert parsed["adapter"] == "shell"


def test_comments_and_blank_lines_are_ignored():
    assert parse_yaml("# leading\n\na: 1\n\n# trailing\n") == {"a": 1}


def test_unsupported_yaml_is_refused_rather_than_guessed():
    """The whole justification for a hand-rolled parser is that it has no
    silent path. Anchors, aliases and tags are real YAML that this does not
    implement, and implementing them badly would be worse than refusing."""
    for text in ("a: &anchor 1\n", "a: *alias\n", "a: !!str 1\n"):
        with pytest.raises(SpecError):
            parse_yaml(text)


def test_folded_scalars_are_refused_by_name():
    with pytest.raises(SpecError, match="folded"):
        parse_yaml("prompt: >\n  some text\n")


def test_tabs_are_refused():
    with pytest.raises(SpecError):
        parse_yaml("a:\n\tb: 1\n")


def test_duplicate_keys_are_refused():
    with pytest.raises(SpecError, match="duplicate"):
        parse_yaml("a: 1\na: 2\n")


def test_errors_carry_a_line_number():
    with pytest.raises(SpecError) as caught:
        parse_yaml("a: 1\nthis is not a mapping\n")
    assert caught.value.line == 2


# -- the job ----------------------------------------------------------------


def test_a_minimal_spec_parses():
    job = parse_job(MINIMAL)
    assert isinstance(job, Job)
    assert (job.name, job.adapter, job.attempts) == ("demo", "shell", 1)
    assert [c.kind for c in job.checks] == ["run"]


def test_a_job_with_no_checks_is_refused():
    """The central rule of the whole project. A job nothing can judge cannot
    be accepted or rejected, and treating that as 'passes trivially' is the
    failure this tool exists to prevent."""
    with pytest.raises(SpecError, match="non-empty"):
        parse_job("name: demo\nprompt: hi\nchecks:\n")
    with pytest.raises(SpecError, match="non-empty"):
        parse_job("name: demo\nprompt: hi\n")


def test_an_unknown_check_is_refused_at_parse_time():
    """Before an agent has been paid to do the work, not after."""
    with pytest.raises(SpecError, match="unknown check"):
        parse_job("name: d\nprompt: p\nchecks:\n  - tsets: pytest\n")


def test_an_unknown_top_level_key_is_refused():
    """A typo'd `chekcs:` would otherwise produce a job that verifies nothing
    and reports success."""
    with pytest.raises(SpecError, match="chekcs"):
        parse_job("name: d\nprompt: p\nchekcs:\n  - run: pytest\n")


def test_names_must_be_usable_as_a_branch():
    for bad in ("Demo", "with space", "unicode-名前", "-leading"):
        with pytest.raises(SpecError, match="name"):
            parse_job(MINIMAL.replace("name: demo", f"name: {bad}"))


def test_an_empty_prompt_is_refused():
    with pytest.raises(SpecError, match="empty"):
        parse_job("name: d\nprompt: '   '\nchecks:\n  - run: pytest\n")


def test_an_unknown_adapter_is_refused():
    with pytest.raises(SpecError, match="unknown adapter"):
        parse_job(MINIMAL.replace("adapter: shell", "adapter: telepathy"))


def test_attempts_must_be_a_positive_integer():
    for bad in ("0", "-2", "many"):
        with pytest.raises(SpecError, match="attempts"):
            parse_job(MINIMAL + f"attempts: {bad}\n")


def test_bounds_checks_need_a_min_or_a_max():
    with pytest.raises(SpecError, match="min"):
        parse_job("name: d\nprompt: p\nchecks:\n  - changed_files: {}\n")


def test_path_checks_need_a_list():
    with pytest.raises(SpecError, match="list of path"):
        parse_job("name: d\nprompt: p\nchecks:\n  - must_not_touch: '*.lock'\n")


def test_limits_default_but_can_be_set():
    assert parse_job(MINIMAL).limits.wall_seconds == 1800
    job = parse_job(MINIMAL + "limits:\n  wall_seconds: 60\n  tokens: 1000\n")
    assert (job.limits.wall_seconds, job.limits.tokens) == (60, 1000)


def test_checks_describe_themselves_in_english():
    job = parse_job(
        "name: d\nprompt: p\nchecks:\n"
        "  - run: pytest -q\n"
        "  - changed_files: {max: 5}\n"
        '  - must_not_touch: ["*.lock"]\n'
    )
    described = [c.describe() for c in job.checks]
    assert described[0] == "`pytest -q` exits zero"
    assert "max 5" in described[1]
    assert "*.lock" in described[2]


def test_loading_from_disk_reports_the_filename(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: d\nprompt: p\nchecks:\n  - nope: 1\n", encoding="utf-8")
    with pytest.raises(SpecError, match="job.yaml"):
        load_job(str(path))


def test_a_job_serialises_for_the_store():
    payload = parse_job(MINIMAL).as_dict()
    assert payload["name"] == "demo"
    assert payload["checks"] == [{"kind": "run", "value": "pytest"}]
