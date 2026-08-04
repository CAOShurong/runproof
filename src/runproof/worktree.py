"""Isolated ground for an agent to work on, and the measurements it leaves.

An agent that edits files is a process with write access to your repository.
Letting one loose in the tree you are sitting in means a failed run and a
half-finished refactor are the same event, and the recovery is `git checkout .`
over work you may not have committed.

So every attempt gets its own `git worktree` on its own branch. The user's
checkout is never written to, never checked out, never stashed. If isolation
cannot be obtained -- not a git repository, a branch name already taken, git
missing -- the run **refuses to start**. Degrading to "just run it in place"
would be the single most destructive thing this tool could do, and it is
exactly what a helpful fallback would look like.

Two things follow that are easy to get wrong.

**Cleanup must survive interruption.** A worktree left behind after a crash
holds disk and, worse, holds a branch name, so the next run of the same job
fails to start for a reason that has nothing to do with the job. Removal is
therefore idempotent and forced, and `prune_stale` exists to clear what an
earlier crash left. The branch is kept on purpose -- a rejected attempt you
can still check out is evidence; one that was deleted is a rumour.

**The diff is measured against the base commit, not against the file system.**
An agent that creates a file and deletes it again has changed nothing, and a
tool that counted "files written" would say otherwise. `git diff --numstat`
answers the question actually being asked: what would land if this were
merged.
"""

from __future__ import annotations

import binascii
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

__all__ = ["DiffStat", "Worktree", "WorktreeError", "prune_stale", "repository_root"]

#: Branch names runproof creates. Namespaced so that `git branch --list
#: 'runproof/*'` is a complete answer to "what did this tool make".
BRANCH_PREFIX = "runproof"

#: Seconds to wait for git itself. Git operations are local and fast; if one
#: hangs this long something is wrong (a lock held by a dead process, usually)
#: and waiting longer only delays the report.
GIT_TIMEOUT = 120


class WorktreeError(RuntimeError):
    """Isolation could not be obtained or maintained, so nothing should run."""


@dataclass(frozen=True)
class DiffStat:
    """What an attempt would actually land."""

    files_changed: int
    insertions: int
    deletions: int
    paths: tuple[str, ...]

    @property
    def lines(self) -> int:
        return self.insertions + self.deletions

    def as_dict(self) -> dict:
        return {
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "lines": self.lines,
            "paths": list(self.paths),
        }


def _git(args: list[str], cwd: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def repository_root(path: str = ".") -> str:
    """The top of the working tree containing ``path``.

    Raises rather than returning None: every caller needs a repository, and a
    None that flows onward becomes a confusing failure three frames later.
    """
    try:
        result = _git(["rev-parse", "--show-toplevel"], cwd=path)
    except FileNotFoundError as error:
        raise WorktreeError("git is not on PATH; runproof cannot isolate anything") from error
    except subprocess.TimeoutExpired as error:
        raise WorktreeError("git did not respond; is an index lock held?") from error
    if result.returncode != 0:
        raise WorktreeError(f"{os.path.abspath(path)} is not inside a git repository")
    return os.path.normpath(result.stdout.strip())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "job"


class Worktree:
    """A throwaway checkout on its own branch.

    Used as a context manager. The branch outlives the worktree so that a
    rejected attempt can be inspected; the directory does not.
    """

    def __init__(self, root: str, job_name: str, ordinal: int = 1, base: str = "HEAD"):
        self.root = repository_root(root)
        self.job_name = _slug(job_name)
        self.ordinal = ordinal
        self.base = base
        # A short random suffix, not just the clock. int(time.time()) has
        # one-second resolution, so `attempts: 3` running back to back built
        # the same branch name three times and the second attempt refused to
        # start. Found the first time this was run for real.
        unique = binascii.hexlify(os.urandom(3)).decode()
        self.branch = f"{BRANCH_PREFIX}/{self.job_name}-{ordinal}-{unique}"
        self.path: str | None = None
        self._base_commit: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def create(self) -> str:
        if self.path is not None:
            raise WorktreeError("worktree already created")

        head = _git(["rev-parse", self.base], cwd=self.root)
        if head.returncode != 0:
            raise WorktreeError(
                f"cannot resolve base {self.base!r}: {head.stderr.strip()}. "
                "An empty repository with no commits cannot be branched from."
            )
        self._base_commit = head.stdout.strip()

        existing = _git(["rev-parse", "--verify", "--quiet", self.branch], cwd=self.root)
        if existing.returncode == 0:
            raise WorktreeError(f"branch {self.branch} already exists; refusing to reuse it")

        path = os.path.join(tempfile.mkdtemp(prefix="runproof-"), self.job_name)
        created = _git(
            ["worktree", "add", "--quiet", "-b", self.branch, path, self._base_commit],
            cwd=self.root,
        )
        if created.returncode != 0:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
            raise WorktreeError(f"git worktree add failed: {created.stderr.strip()}")
        self.path = path
        return path

    def remove(self, keep_branch: bool = True) -> None:
        """Idempotent, and forced. A leftover worktree holds a branch name."""
        if self.path is None:
            return
        parent = os.path.dirname(self.path)
        _git(["worktree", "remove", "--force", self.path], cwd=self.root)
        shutil.rmtree(parent, ignore_errors=True)
        _git(["worktree", "prune"], cwd=self.root)
        if not keep_branch:
            _git(["branch", "-D", self.branch], cwd=self.root)
        self.path = None

    def __enter__(self) -> "Worktree":
        self.create()
        return self

    def __exit__(self, *_) -> None:
        self.remove()

    # -- using it ----------------------------------------------------------

    def run(self, command: str, timeout: int | None = None) -> subprocess.CompletedProcess:
        """Run a shell command inside the worktree.

        `shell=True` because checks are written by the repository's owner in
        their own spec file, in their own repository. Treating that as
        untrusted input would mean inventing an argv syntax for something that
        is already a command line.
        """
        if self.path is None:
            raise WorktreeError("worktree is not created")
        return subprocess.run(
            command,
            shell=True,
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

    def stage_all(self) -> None:
        """Stage everything, so that new files count as changes.

        Without this, `git diff` ignores untracked files and an agent that
        created ten new modules would be reported as having changed nothing.
        """
        if self.path is None:
            raise WorktreeError("worktree is not created")
        _git(["add", "-A"], cwd=self.path)

    def diff(self) -> DiffStat:
        """What this attempt would land, measured against the base commit."""
        if self.path is None:
            raise WorktreeError("worktree is not created")
        self.stage_all()
        result = _git(["diff", "--numstat", "--cached", self._base_commit], cwd=self.path)
        if result.returncode != 0:
            raise WorktreeError(f"git diff failed: {result.stderr.strip()}")

        insertions = deletions = 0
        paths: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            # "-" means binary; it is a real change with no line count.
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
            paths.append(path)
        return DiffStat(len(paths), insertions, deletions, tuple(sorted(paths)))

    def commit(self, message: str) -> str | None:
        """Commit the attempt's work, returning the new sha or None if empty."""
        if self.path is None:
            raise WorktreeError("worktree is not created")
        self.stage_all()
        if not self.diff().files_changed:
            return None
        committed = _git(["commit", "--quiet", "-m", message], cwd=self.path)
        if committed.returncode != 0:
            raise WorktreeError(f"commit failed: {committed.stderr.strip()}")
        return _git(["rev-parse", "HEAD"], cwd=self.path).stdout.strip()


def prune_stale(root: str, older_than_seconds: int = 86400) -> list[str]:
    """Remove runproof worktrees an earlier crash left behind.

    Branches are kept. The point is to free the *names* and the disk, not to
    destroy evidence -- a run that died is exactly the one somebody will want
    to look at.
    """
    root = repository_root(root)
    _git(["worktree", "prune"], cwd=root)
    listing = _git(["worktree", "list", "--porcelain"], cwd=root)
    removed: list[str] = []
    cutoff = time.time() - older_than_seconds
    for line in listing.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree ") :].strip()
        if "runproof-" not in path.replace("\\", "/"):
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            pass
        _git(["worktree", "remove", "--force", path], cwd=root)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        removed.append(path)
    _git(["worktree", "prune"], cwd=root)
    return removed
