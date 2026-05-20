# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI service that receives GitLab MR webhooks and posts AI code review comments back to the MR via the GitLab API. Review generation is delegated to the Claude Code CLI (`claude -p`) using the `/review-pr` slash command — **not** the Anthropic API.

## Run / debug

```bash
# Production-like
docker compose up -d --build
docker compose logs -f ai-reviewer

# Local Python (still requires env vars from .env)
pip install -r requirements.txt
uvicorn webhook_server:app --reload --port 8080

# Trigger review_runner directly (bypasses webhook gate)
python review_runner.py <project_id> <mr_iid>

# Smoke tests against a running server
curl -s http://localhost:8080/healthz
curl -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"max"}]}'
```

There is no test suite yet (`requirements.txt` has no pytest).

## Architecture

Two-file pipeline, intentionally split into separate processes:

```
GitLab webhook → webhook_server.py (long-lived FastAPI)
                    └─ asyncio.create_subprocess_exec ──> review_runner.py (per-MR, one-shot)
                                                              ├─ GET /merge_requests/:iid + /diffs
                                                              ├─ claude -p "/review-pr\n..."
                                                              └─ POST /merge_requests/:iid/notes
```

- **`webhook_server.py`** — webhook validation, filtering, dispatch only. Never blocks on review work; spawns `review_runner.py` as a subprocess so a crash there can't take the server down.
- **`review_runner.py`** — does the actual API calls and Claude invocation. Designed to be invokable standalone for local testing.

### Webhook filter contract (must hold for a review to fire)

1. `X-Gitlab-Token` header equals `WEBHOOK_SECRET` (else 401).
2. `object_attributes.action ∈ {open, update}`.
3. `REVIEWER_USERNAME` appears in `reviewers[].username`.
4. `project.id` and `object_attributes.iid` are both present.

Anything else returns `{"status": "skipped", "reason": "..."}` with 200. Don't tighten the filter without updating both the README's example payloads and `TARGET_ACTIONS` together.

### Auth model — the load-bearing decision

The container has **no Anthropic API key**. It runs `claude` by mounting the host's `~/.claude` into the container (`docker-compose.yml`), reusing the host user's Claude subscription session. Consequences worth remembering:

- The `:ro` mount can fight OAuth token refresh — README documents removing `:ro` as the fix; preserve that path.
- If `claude -p` returns non-zero, the most common cause is host session expiry, not container state. Tell the user to re-run `claude login` on the **host**.
- Don't add code paths that assume `ANTHROPIC_API_KEY`; that env var is intentionally absent.

### Claude invocation rules

In `run_claude_review()`, the **first line of the prompt must be the slash command** (`/review-pr\n`) — Claude Code only treats it as a slash command in that position. Output is requested in Korean markdown to match the rest of the comment template (`🤖 **AI 자동 코드 리뷰**`).

### Failure notification

When `review_runner.py` fails (clone/fetch, `claude` non-zero or empty output, GitLab API errors), it posts a `⚠️ **AI 자동 코드 리뷰 실패**` comment to the MR with an `@REVIEWER_USERNAME` mention — the mention makes GitLab send its standard email, so the user learns of failures without watching container logs. The comment carries the failed stage, a heuristic cause, and the last `STDERR_TAIL_LINES` lines of `claude` stderr in a collapsed block. `ReviewError` carries `(stage, reason, detail)` from a failing stage up to `notify_failure()`. Notification is best-effort: if the comment POST itself fails (token/GitLab down) it is logged only — that overlap of "failure channel" and "failed thing" is an accepted blind spot. `claude` stderr is captured via `stderr=PIPE` (not inherited) and re-logged so docker logs still show it.

### Token-budget guardrails

`review_runner.py` truncates aggressively before sending to Claude:

| Constant | Value | Why |
|---|---|---|
| `MAX_FILES` | 10 | Cap files included in the prompt |
| `MAX_DIFF_CHARS_PER_FILE` | 2000 | Cap per-file diff size |
| `CLAUDE_TIMEOUT_SEC` | 120 | Hard kill on stalled CLI |

If you change these, also update the "files omitted" footer message that depends on `len(diffs) > MAX_FILES`.

## Required env vars

All consumed at import time (`os.environ[...]` — missing keys crash on boot, by design):

- `GITLAB_URL` — base URL, no trailing slash (stripped defensively anyway).
- `GITLAB_TOKEN` — PAT with `api` scope.
- `WEBHOOK_SECRET` — must match GitLab webhook Secret Token.
- `REVIEWER_USERNAME` (default `max`) — used by **both** processes: in `webhook_server.py` it gates which `reviewers[].username` triggers a review; in `review_runner.py` it is the `@`-mention target of the failure-notification comment.

## Conventions

- Korean commit messages, Conventional Commits prefixes (`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`). Matches existing git log.
- User-facing strings (MR comment body, prompt, log skip reasons) are Korean; keep them consistent if you add new ones.
- BMad tooling lives under `_bmad/`, `.claude/skills/`, `.agents/skills/` — installed as scaffolding, not part of the runtime. Don't pull from it at import time.
