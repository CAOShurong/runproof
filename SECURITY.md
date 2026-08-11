# Security policy

## Supported versions

Security fixes are provided for the latest minor release. Upgrade to the newest
published version before reporting a problem.

Versions before 0.2.0 used unsafe unattended-agent defaults and could also
misreport a failed agent process as accepted work when repository checks happened
to stay green. Do not use those versions for unattended agent runs.

## Security boundary

RunProof isolates each attempt in a disposable Git worktree. That protects the
caller's checkout from ordinary edits; it does **not** isolate the host, secrets,
network, other repositories, or tools available to the agent.

The built-in adapters now fail closed by default:

- `claude` uses non-interactive `dontAsk` permissions, a fixed repository-scoped
  read/edit tool surface, no inherited settings, strict MCP configuration, and no
  session persistence, browser integration, or slash-command skills.
- `codex` explicitly uses its `workspace-write` sandbox and an ephemeral session,
  without loading user configuration or execution-policy rules.
- an adapter authentication error, timeout, crash, or non-zero exit can never be
  accepted merely because the unchanged repository still passes its checks.

These controls reduce accidental access; they are not a hostile-code security
boundary. For untrusted repositories or prompts, run RunProof inside a disposable
container or virtual machine with minimal credentials and network access. See the
[Claude Code sandboxing guidance](https://code.claude.com/docs/en/sandboxing),
[Claude Code permissions reference](https://code.claude.com/docs/en/permissions),
and [Codex CLI reference](https://developers.openai.com/codex/cli/reference).

`adapter: shell` deliberately executes the job prompt as a shell command. Every
`run:` check also executes a shell command. Treat job specifications as executable
code: review them, keep them under trusted version control, and never run a spec
received from an untrusted party on your host.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature rather than opening a
public issue. Include the affected version, platform, minimal reproduction, impact,
and whether the behavior requires an untrusted repository or job specification.
Please do not include live credentials or other people's private data.
