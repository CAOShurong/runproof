"""What a job is, and refusing the ones that cannot be judged.

A job says what an agent should do and, more importantly, **how anyone will
know whether it worked**. Those two halves are not equal partners here. The
prompt is a wish. The checks are the product.

So the central rule of this module is that a job with no checks is a
*validation error*, not a job that trivially passes. Accepting unverifiable
work is the exact failure this whole tool exists to prevent, and the most
likely way for it to creep back in is somebody writing a spec in a hurry and
the parser being accommodating about it.

**On parsing YAML without a YAML library.** There is no YAML in the standard
library, and this package wants no runtime dependencies. So it parses a
deliberately small subset -- nested mappings, block and inline sequences,
scalars, and ``|`` block strings -- and **raises on anything it does not
understand rather than guessing**. Hand-rolled YAML is a well-earned smell,
and the thing that makes it dangerous is silent misinterpretation: a parser
that quietly reads ``on: no`` as a boolean, or drops a key it did not expect.
This one has no silent path. Every construct is either supported and tested,
or it is a :class:`SpecError` naming the line.

That trade is worth stating plainly: you get zero dependencies and a parser
that refuses real YAML files. For job specs, which are a dozen lines of
mapping, that is the right side of the trade. For anything else it would not
be.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

__all__ = ["Check", "Job", "Limits", "SpecError", "load_job", "parse_job", "parse_yaml"]

#: Checks a job may declare. Each maps to a verifier in :mod:`runproof.verify`.
#: Listed here rather than there so that a typo in a spec fails at parse time,
#: before an agent has been paid to do the work.
CHECK_KINDS = (
    "run",  # a command that must exit zero
    "changed_files",  # bounds on how many files the run may touch
    "diff_lines",  # bounds on how large the diff may be
    "must_not_touch",  # paths the run is forbidden to modify
    "must_touch",  # paths the run is required to modify
    "file_contains",  # a file that must contain a string when the run ends
)

#: Adapters the runner knows how to drive.
ADAPTERS = ("shell", "claude", "codex")

#: A job that never terminates is worse than one that fails, because it holds
#: a worktree and a slot forever. Every job gets a wall clock ceiling whether
#: it asks for one or not.
DEFAULT_WALL_SECONDS = 1800

#: Attempts default to one, but the number is where honesty about stochastic
#: agents lives -- see :attr:`Job.attempts`.
DEFAULT_ATTEMPTS = 1


class SpecError(ValueError):
    """A spec that cannot be trusted to mean one thing.

    Carries the line number when there is one. A parse error without a
    location turns a ten-line file into a guessing game.
    """

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


# --------------------------------------------------------------------------
# The YAML subset
# --------------------------------------------------------------------------

_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<rest>.*)$")
_ITEM = re.compile(r"^-\s*(?P<rest>.*)$")
_INLINE_MAP = re.compile(r"^\{(?P<body>.*)\}$")
_INLINE_SEQ = re.compile(r"^\[(?P<body>.*)\]$")


def _scalar(raw: str, line: int):
    """One scalar value, or a :class:`SpecError` if it is ambiguous."""
    text = raw.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.startswith(("&", "*", "!", "%", "@", "`")):
        # Anchors, aliases, tags and directives. Supporting them badly is
        # worse than not supporting them.
        raise SpecError(f"unsupported YAML syntax {text[0]!r}", line)
    return text


def _split_commas(body: str, line: int) -> list[str]:
    """Split on commas that are not inside quotes or brackets."""
    parts, depth, quote, current = [], 0, "", ""
    for char in body:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
        elif char in "[{":
            depth += 1
            current += char
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise SpecError("unbalanced bracket", line)
            current += char
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if quote:
        raise SpecError("unterminated quote", line)
    if depth:
        raise SpecError("unbalanced bracket", line)
    if current.strip():
        parts.append(current)
    return parts


def _inline(text: str, line: int):
    """An inline ``{a: 1}`` mapping or ``[a, b]`` sequence."""
    mapping = _INLINE_MAP.match(text)
    if mapping:
        result = {}
        for part in _split_commas(mapping.group("body"), line):
            if ":" not in part:
                raise SpecError(f"inline mapping entry has no colon: {part.strip()!r}", line)
            key, _, value = part.partition(":")
            result[key.strip()] = _value(value.strip(), line)
        return result
    sequence = _INLINE_SEQ.match(text)
    if sequence:
        return [_value(p.strip(), line) for p in _split_commas(sequence.group("body"), line)]
    return None


def _value(text: str, line: int):
    inline = _inline(text, line) if text[:1] in "{[" else None
    return inline if inline is not None else _scalar(text, line)


def _indent(raw: str) -> int:
    stripped = raw.lstrip(" ")
    if raw[: len(raw) - len(stripped)].count("\t"):
        raise SpecError("tabs are not valid indentation in YAML")
    return len(raw) - len(stripped)


def parse_yaml(text: str):
    """Parse the supported subset, or raise :class:`SpecError`.

    Not a YAML implementation and does not pretend to be one. It exists so
    that a job spec can be a dozen readable lines without the package growing
    a dependency, and it fails loudly on everything outside that.
    """
    lines: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        without_comment = raw
        if raw.lstrip().startswith("#"):
            continue
        if not raw.strip():
            continue
        lines.append((number, _indent(without_comment), without_comment.strip()))

    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise SpecError("could not parse to the end of the document", lines[index][0])
    return value


def _parse_block(lines, index: int, indent: int):
    if index >= len(lines):
        return None, index
    if _ITEM.match(lines[index][2]):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_sequence(lines, index: int, indent: int):
    items = []
    while index < len(lines):
        number, own_indent, content = lines[index]
        if own_indent < indent:
            break
        item = _ITEM.match(content)
        if not item:
            if own_indent > indent:
                raise SpecError("unexpected indentation inside a sequence", number)
            break
        rest = item.group("rest").strip()
        index += 1
        if not rest:
            nested, index = _parse_block(lines, index, own_indent + 1)
            items.append(nested)
        elif _KEY.match(rest) and not rest.startswith(("{", "[")):
            # `- run: pytest` -- a mapping that begins on the dash line.
            synthetic = [(number, own_indent + 2, rest)]
            while index < len(lines) and lines[index][1] > own_indent:
                synthetic.append(lines[index])
                index += 1
            mapping, consumed = _parse_mapping(synthetic, 0, own_indent + 2)
            if consumed != len(synthetic):
                raise SpecError("could not parse sequence item", number)
            items.append(mapping)
        else:
            items.append(_value(rest, number))
    return items, index


def _parse_mapping(lines, index: int, indent: int):
    result: dict = {}
    while index < len(lines):
        number, own_indent, content = lines[index]
        if own_indent < indent:
            break
        if own_indent > indent and result:
            raise SpecError("unexpected indentation", number)
        match = _KEY.match(content)
        if not match:
            if _ITEM.match(content):
                break
            raise SpecError(f"expected `key: value`, got {content!r}", number)
        key = match.group("key")
        if key in result:
            raise SpecError(f"duplicate key {key!r}", number)
        rest = match.group("rest").strip()
        index += 1

        if rest in ("|", "|-", ">"):
            if rest == ">":
                raise SpecError("folded scalars (`>`) are not supported; use `|`", number)
            block, index = _parse_block_scalar(lines, index, own_indent)
            result[key] = block if rest == "|" else block.rstrip("\n")
        elif rest:
            result[key] = _value(rest, number)
        else:
            nested, index = _parse_block(lines, index, own_indent + 1)
            result[key] = nested
    return result, index


def _parse_block_scalar(lines, index: int, indent: int) -> tuple[str, int]:
    """Collect a ``|`` block, preserving indentation *relative to the block*.

    Lines are stripped during pre-parsing, so the original leading spaces have
    to be rebuilt from the recorded indent. Missing that made every block
    scalar left-align, which silently destroyed any prompt containing code --
    found by writing a prompt with an indented function body and watching it
    come back flat. For a tool whose entire input is prompts, that is not a
    cosmetic bug.
    """
    collected: list[tuple[int, str]] = []
    while index < len(lines):
        _, own_indent, content = lines[index]
        if own_indent <= indent:
            break
        collected.append((own_indent, content))
        index += 1
    if not collected:
        return "", index
    base = min(depth for depth, _ in collected)
    body = "\n".join(" " * (depth - base) + content for depth, content in collected)
    return body + "\n", index


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One thing that must be true for a run to count."""

    kind: str
    value: object

    def describe(self) -> str:
        if self.kind == "run":
            return f"`{self.value}` exits zero"
        if self.kind in ("changed_files", "diff_lines"):
            bounds = ", ".join(f"{k} {v}" for k, v in sorted(dict(self.value).items()))
            return f"{self.kind.replace('_', ' ')} within {bounds}"
        if self.kind in ("must_not_touch", "must_touch"):
            verb = "does not modify" if self.kind == "must_not_touch" else "modifies"
            return f"the run {verb} {', '.join(self.value)}"
        return f"{self.kind}: {self.value}"


@dataclass(frozen=True)
class Limits:
    """Ceilings that stop a run rather than judge it."""

    wall_seconds: int = DEFAULT_WALL_SECONDS
    tokens: int | None = None

    def as_dict(self) -> dict:
        return {"wall_seconds": self.wall_seconds, "tokens": self.tokens}


@dataclass(frozen=True)
class Job:
    """A unit of unattended work, and the proof it must produce."""

    name: str
    prompt: str
    checks: tuple[Check, ...]
    adapter: str = "claude"
    #: How many independent attempts to run. Agents are stochastic, so one
    #: success is weak evidence; this is what turns "it worked" into a pass
    #: rate. Kept in the spec rather than the CLI so the claim travels with
    #: the job.
    attempts: int = DEFAULT_ATTEMPTS
    limits: Limits = field(default_factory=Limits)
    #: Where the job came from, for error messages and reports.
    source: str = "<memory>"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "adapter": self.adapter,
            "attempts": self.attempts,
            "checks": [{"kind": c.kind, "value": c.value} for c in self.checks],
            "limits": self.limits.as_dict(),
            "source": self.source,
        }


_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _require(mapping: dict, key: str, kinds: tuple, where: str):
    if key not in mapping or mapping[key] is None:
        raise SpecError(f"{where} is missing required key {key!r}")
    if not isinstance(mapping[key], kinds):
        got = type(mapping[key]).__name__
        raise SpecError(f"{where}: {key!r} should be {kinds[0].__name__}, got {got}")
    return mapping[key]


def _parse_checks(raw, where: str) -> tuple[Check, ...]:
    if not isinstance(raw, list) or not raw:
        raise SpecError(
            f"{where}: `checks` must be a non-empty list. A job with nothing to "
            "verify cannot be accepted or rejected, and accepting unverifiable "
            "work is the failure this tool exists to prevent."
        )
    checks = []
    for entry in raw:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise SpecError(f"{where}: each check must be one `kind: value` pair, got {entry!r}")
        kind, value = next(iter(entry.items()))
        if kind not in CHECK_KINDS:
            known = ", ".join(CHECK_KINDS)
            raise SpecError(f"{where}: unknown check {kind!r}. Known checks: {known}")
        if kind in ("must_not_touch", "must_touch") and not isinstance(value, list):
            raise SpecError(f"{where}: {kind!r} takes a list of path patterns")
        if kind in ("changed_files", "diff_lines"):
            if not isinstance(value, dict) or not {"min", "max"} & set(value):
                raise SpecError(f"{where}: {kind!r} takes {{min: N}} or {{max: N}}")
        checks.append(Check(kind, value))
    return tuple(checks)


def parse_job(text: str, source: str = "<memory>") -> Job:
    """Parse and validate one job spec."""
    data = parse_yaml(text)
    if not isinstance(data, dict):
        raise SpecError(f"{source}: a job spec must be a mapping")

    name = _require(data, "name", (str,), source)
    if not _NAME.match(name):
        raise SpecError(
            f"{source}: name {name!r} must be lowercase letters, digits and "
            "hyphens -- it becomes a git branch and a directory"
        )
    prompt = _require(data, "prompt", (str,), source)
    if not prompt.strip():
        raise SpecError(f"{source}: `prompt` is empty")

    adapter = data.get("adapter", "claude")
    if adapter not in ADAPTERS:
        raise SpecError(f"{source}: unknown adapter {adapter!r}. Known: {', '.join(ADAPTERS)}")

    attempts = data.get("attempts", DEFAULT_ATTEMPTS)
    if not isinstance(attempts, int) or attempts < 1:
        raise SpecError(f"{source}: `attempts` must be a positive integer, got {attempts!r}")

    raw_limits = data.get("limits") or {}
    if not isinstance(raw_limits, dict):
        raise SpecError(f"{source}: `limits` must be a mapping")
    wall = raw_limits.get("wall_seconds", DEFAULT_WALL_SECONDS)
    if not isinstance(wall, int) or wall < 1:
        raise SpecError(f"{source}: `wall_seconds` must be a positive integer")
    tokens = raw_limits.get("tokens")
    if tokens is not None and (not isinstance(tokens, int) or tokens < 1):
        raise SpecError(f"{source}: `tokens` must be a positive integer or absent")

    unknown = set(data) - {"name", "prompt", "adapter", "attempts", "checks", "limits"}
    if unknown:
        # Refusing unknown keys rather than ignoring them: a typo'd `check:`
        # would otherwise produce a job that silently verifies nothing.
        raise SpecError(f"{source}: unknown keys: {', '.join(sorted(unknown))}")

    return Job(
        name=name,
        prompt=prompt,
        checks=_parse_checks(data.get("checks"), source),
        adapter=adapter,
        attempts=attempts,
        limits=Limits(wall_seconds=wall, tokens=tokens),
        source=source,
    )


def load_job(path: str) -> Job:
    """Read and validate a job spec from disk."""
    with open(path, encoding="utf-8") as handle:
        return parse_job(handle.read(), source=os.path.basename(path))
