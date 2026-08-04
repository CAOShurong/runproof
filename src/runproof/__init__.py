"""Run agent work unattended, and prove it actually worked.

Describe a job, and runproof executes it in an isolated git worktree, then
refuses to accept the result unless it passes the checks you declared. What
survives is a branch you can merge. What does not is a report saying exactly
where it failed.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
