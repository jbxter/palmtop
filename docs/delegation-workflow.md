# Julian delegation workflow (operator runbook)

Julian (pocket-agent on the S21) can delegate work through three paths: **chat** (local + tools), **sovereign engine** (Claude/Gemini cloud orchestration), and **Cursor Cloud Agents** (repo VMs). Deploy tools add **Vercel** and **Railway** for shipping web apps.

## Quick reference

| Path | Trigger (Telegram) | Trigger (SMS) | Best for |
|------|-------------------|---------------|----------|
| Chat + tools | normal message | normal message | Calendar, Jira, search, `[TOOL:…]` |
| Sovereign engine | `/engine …` or `/claude …` | `engine: …` | Multi-step autonomous tasks, alignment gate |
| Cursor bridge | `/cursor …` | `cursor: …` | Code changes on allowed GitHub repos |
| Vercel deploy | `[TOOL:vercel] deploy` | same | Production/preview deploy of linked project |
| Railway deploy | `[TOOL:railway] deploy` | same | Redeploy configured Railway service |

`/claude` is an alias for `/engine` (same handler, same sovereign runner).

## Environment checklist (S21)

Copy `config.example.toml` → `config.toml` and set secrets via env (recommended on Termux):

| Variable | Used by |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram channel |
| `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | Cloud inference + sovereign engine |
| `ATLASSIAN_DOMAIN`, `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN` | Jira / Confluence REST |
| `CURSOR_API_KEY` | Cursor Cloud Agents |
| `VERCEL_TOKEN` | Vercel deploy tool |
| `RAILWAY_TOKEN` | Railway deploy tool |

Enable sections in `config.toml`:

```toml
[engine]
enabled = true

[cursor]
enabled = true
allowed_repos = ["https://github.com/your-org/your-repo"]
default_repo = "https://github.com/your-org/your-repo"
require_blessing = true

[vercel]
enabled = true
default_project_id = "prj_…"

[railway]
enabled = true
default_service_id = "…"
default_environment_id = "…"
require_blessing = true
```

Run: `uv sync --extra telegram` on the phone, then `uv run python -m pocket_agent`.

## Chat vs engine vs Cursor

**Chat** — Default loop. Julian replies with local/cloud LLM, can emit `[TOOL:name] query`. Use for interactive Q&A, Jira lookups, reminders, file notes.

**Engine** (`/engine`, `/claude`, `engine:`, `claude:`) — Runs `PocketAgent.orchestrate_result` in a worker thread: 12WY alignment, optional **blessing** (`/approve` / `/deny`), autonomous tool use. Use when you want Julian to *execute* a goal, not just advise.

**Cursor** (`/cursor`, `cursor:`, `[TOOL:cursor]`) — Starts a Cursor Cloud Agent on an **allowlisted** repo. Polls until finished; notifies on Telegram. Requires `CURSOR_API_KEY` and `[cursor] allowed_repos`. Blessing gate mirrors engine when `require_blessing = true`.

## Human approval (blessing)

Shared `BlessingGate` for engine, Cursor (when configured), Vercel deploy, and Railway deploy.

1. Julian sends an approval summary to Telegram.
2. Reply `/approve` or `/deny` within 5 minutes.
3. Default deny on timeout.

Turn off per integration: `require_blessing = false` under `[cursor]`, `[vercel]`, or `[railway]`.

## Jira (create / comment / transition)

REST tool (or Atlassian MCP gateway). Julian emits:

```
[TOOL:jira] create PROJ | Summary | Optional description
[TOOL:jira] comment PROJ-123 | Comment text
[TOOL:jira] transition PROJ-123 | Done
[TOOL:jira] search assignee = currentUser()
[TOOL:jira] get PROJ-123
```

Verify on startup: logs `Jira REST auth verified ✓`. Test in Telegram:

1. `What's on my Jira?` → should hint jira tool and list issues.
2. `[TOOL:jira] create TEST | Julian smoke test | from S21` (use a real project key).
3. `[TOOL:jira] comment TEST-NN | Delegation runbook test`
4. `[TOOL:jira] transition TEST-NN | Done` (status name must match workflow).

Requires `ATLASSIAN_*` env vars or `[atlassian]` in config.

## Web apps: Vercel vs Railway

### Vercel (linked Git project)

Prerequisite: project already exists in Vercel dashboard with Git connected.

1. Set `VERCEL_TOKEN` and `[vercel] default_project_id` (or `default_project_name`).
2. In Telegram: `Deploy my app to Vercel` or `[TOOL:vercel] deploy main`
3. Approve if blessing enabled.
4. Check `[TOOL:vercel] get <deployment_id>` or Vercel dashboard.

Commands:

- `[TOOL:vercel] status` — auth + config summary
- `[TOOL:vercel] projects` — list projects for token
- `[TOOL:vercel] deploy [branch]` — trigger deployment (`withLatestCommit` + optional branch ref)
- `[TOOL:vercel] get dpl_…` — deployment status

### Railway (existing service)

Prerequisite: service already created in Railway; copy IDs from dashboard URL.

1. Set `RAILWAY_TOKEN` (account token; team tokens may fail for deploy API).
2. Set `default_project_id`, `default_service_id`, `default_environment_id` in `[railway]`.
3. In Telegram: `[TOOL:railway] deploy` → approve → `[TOOL:railway] deployments`

Commands:

- `[TOOL:railway] status` — auth + configured IDs
- `[TOOL:railway] deploy` — `serviceInstanceDeploy` mutation
- `[TOOL:railway] deployments` — last 5 deployments
- `[TOOL:railway] get <deployment_id>` — single deployment status

**New Railway service from repo** is not automated in this MVP — create the service in the Railway UI (or template), paste IDs into config, then use `deploy`.

## Testing on Telegram

1. Start agent with `config.toml` and allowed user ID in `[telegram] allowed_users`.
2. `/engine say hello` — expect sovereign engine reply (or blocked if misaligned in hard mode).
3. `/claude list open questions` — same path as engine.
4. `/cursor fix typo in README` — expect launch message + later completion (if Cursor configured).
5. `[TOOL:vercel] status` / `[TOOL:railway] status` — expect connected message.
6. `/approve` / `/deny` only when a pending approval message is shown.

## Logs and audit

- Engine runs: `data/engine_runs.jsonl`
- Cursor jobs: `data/cursor_jobs.jsonl`
- Traces: `data/traces.db` when `[observability] enabled = true`
