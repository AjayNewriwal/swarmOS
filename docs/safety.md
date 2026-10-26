# Execution safety

## What runs, and where

A run creates a temporary directory. The canonical workspace is a copy; coding
branches check out their own subdirectory and the scheduler merges a branch only
after conflict checks. The Verification Agent then runs a command inside the
canonical workspace.

This is **containment**, not a sandbox. It does not provide kernel isolation,
cgroups, a network namespace, or a separate user.

## Controls that are implemented

| Control | Default | Effect |
| --- | --- | --- |
| `SWARMOS_EXECUTION_ENABLED` | `1` | `0` refuses to run any command (exit 126) |
| `SWARMOS_ALLOWED_COMMANDS` | `python,python3,py,pytest` | binary allowlist; no shell is invoked |
| `SWARMOS_VERIFY_TIMEOUT_SECONDS` | `120` | process **tree** is killed on timeout |
| Environment allowlist | fixed list | provider keys are not inherited by test code |
| Path checks | always | absolute paths, `..`, `~`, and drive paths are rejected |
| Symlink handling | always | symlinks are never read into a snapshot or copied |
| `SWARMOS_ALLOW_REPO_CHECKOUT` | `0` | copying a real repository is opt-in |
| `SWARMOS_API_TOKEN` | unset | when set, every `/api` route requires a bearer token |
| `SWARMOS_ALLOW_REMOTE_BIND` | `0` | non-loopback binds are refused by the CLI |
| Snapshot cap | 400k chars | limits what is sent to a model |

The test subprocess receives only `PATH`, `PATHEXT`, `SYSTEMROOT`,
`SYSTEMDRIVE`, `COMSPEC`, `WINDIR`, `TEMP`, `TMP`, `HOME`, `USERPROFILE`,
`LANG`, `LC_ALL`, plus `PYTHONPATH` pointing at the workspace. `OPENAI_COMPAT_*`
and `SWARMOS_*` are not inherited.

## What is not protected

- A malicious test file can read any file the **server user** can read. The
  environment filter only stops environment-variable leakage.
- Network access is not blocked. Test code can make outbound requests.
- There is no CPU or memory quota. A runaway test is stopped by the timeout only.
- The process runs as the same user as the API.
- On Windows the timeout kills the process tree with `taskkill /T`; a detached
  grandchild that leaves the tree is not guaranteed to die.

Therefore: **never expose the API on a public interface.** The CLI refuses a
non-loopback bind unless both `SWARMOS_ALLOW_REMOTE_BIND=1` and
`SWARMOS_API_TOKEN` are set, and it prints a warning when it does. Put an
authenticating proxy in front if remote access is genuinely required, and run the
service in a container or VM with no secrets mounted.

## Repository workflow

There is no live-repository workflow yet: no clone, branch, commit, push, or PR.
`Workspace.from_repository` copies a directory for a future workflow and is
disabled by default. When enabled it skips `.git`, virtualenvs, caches, `.env*`,
key material, credentials files, and databases, and it does not follow symlinks.
The copy still has no git history, so nothing can be pushed back.

## Agent and budget limits

- `max_agent_calls` and `max_wall_seconds` are enforced by the ledger before each
  call and always apply.
- `max_retries`, `max_revisions`, and `max_escalations` bound the loops.
- USD limits apply **only** when pricing is configured, and they are a soft cap
  rather than a hard limit: parallel in-flight calls can collectively exceed the
  configured amount. See [providers.md](providers.md).
- A coding branch cannot report a step complete: only the serial merge node can,
  after conflict validation.

## Reporting a concern

If you find a way to escape the workspace, leak a credential into a snapshot, or
exceed a limit, treat it as a security issue and do not open a public issue with
a working exploit.
