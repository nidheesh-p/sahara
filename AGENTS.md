# Sahara — agent rules

Python 3.11+, local-first. Run tests with `PYTHONPATH=src` and the repo's `.venv`; lint with `ruff`.
See CONTRIBUTING.md and ARCHITECTURE.md. Priorities live in GitHub issues labelled P0/P1/P2.

## Working with Selfware (this project is built by a factory of agents)
- The lead writes briefs in `.selfware/briefs/`, records them with `selfware log --task <id> --kind planned
  --brief <path>`, dispatches with `selfware run <id> --worktree`, measures with `selfware validate <id>`,
  judges with `selfware inspect <id> --verdict pass|rework --session <lead-session>`, lands with
  `selfware land <id> --merge`, then `selfware verify --all --log`.
- A worker stays inside its worktree and its brief's `owns:` paths, never commits, adds, stashes, resets,
  checks out or pushes (the git guard refuses every git write), makes at most one write per response, and
  reports the real exit status of every command it ran. A claim is not evidence: the gates re-measure it.
- Never weaken, delete, skip or stub a test or a gate to make something pass. Report a rule that seems wrong.
- No secrets in briefs, logs, fixtures or commits.
- Nobody runs `git push`, opens a pull request, publishes a build or sends a message without the Owner's
  explicit OK. `selfware land --merge` only fast-forwards the LOCAL main; it never pushes.
- Gates import Sahara from the worktree, not the editable install: always prefix with `PYTHONPATH=src`.
