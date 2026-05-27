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
python review_runner.py <project_id> <mr_iid> [oldrev]

# Unit tests (pure functions in review_runner.py)
pip install -r requirements-dev.txt
pytest -q

# Smoke tests against a running server
curl -s http://localhost:8080/healthz
curl -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"max"}]}'
```

Tests cover only `review_runner.py`'s pure functions (marker parsing, comment filtering, injection defense) in `tests/test_review_runner.py`. Test-only deps live in `requirements-dev.txt` — the runtime `requirements.txt` (and the Docker image) deliberately exclude `pytest`. `tests/conftest.py` fills dummy env vars so the module can be imported.

## Architecture

Two-file pipeline, intentionally split into separate processes:

```
GitLab webhook → webhook_server.py (long-lived FastAPI)
                    └─ asyncio.create_subprocess_exec ──> review_runner.py (per-MR, one-shot)
                          ├─ GET /merge_requests/:iid, GET /projects/:id   (메타데이터)
                          ├─ GET /merge_requests/:iid/discussions  (직전 리뷰·코멘트 수집)
                          ├─ git clone --depth (임시 디렉토리) + target branch fetch
                          ├─ claude -p "/review-pr\n..."  (클론에서 git diff 직접 실행, 증분 가능)
                          └─ POST /merge_requests/:iid/notes  (성공 리뷰 + SHA 마커, 또는 ⚠️ 실패 알림)
```

Review is **clone-based**: `review_runner.py` shallow-clones the repo into a temp dir and lets `claude` run `git diff` itself — it does **not** fetch diffs via the GitLab API.

- **`webhook_server.py`** — webhook validation, filtering, dispatch only. Never blocks on review work; spawns `review_runner.py` as a subprocess so a crash there can't take the server down.
- **`review_runner.py`** — does the actual API calls and Claude invocation. Designed to be invokable standalone for local testing.

### Webhook filter contract (must hold for a review to fire)

1. `X-Gitlab-Token` header equals `WEBHOOK_SECRET` (else 401).
2. `object_attributes.action ∈ {open, update}`.
3. `REVIEWER_USERNAME` appears in `reviewers[].username`.
4. `project.id` and `object_attributes.iid` are both present.

Anything else returns `{"status": "skipped", "reason": "..."}` with 200. Don't tighten the filter without updating both the README's example payloads and `TARGET_ACTIONS` together.

`webhook_server.py` also extracts `object_attributes.oldrev` (previous source-branch HEAD on a push) and passes it as the **optional 3rd argv** to `review_runner.py` — `python review_runner.py <project_id> <mr_iid> [oldrev]`. It is only an incremental-review fallback; absence is normal (e.g. local invocation, non-push updates).

### Incremental review

A webhook `update` fires on every push to the MR, so a re-review would otherwise re-review the whole diff each time. Instead `review_runner.py` does an **incremental review**: it diffs only commits added since the last successful review.

- The last-reviewed source HEAD SHA is stored in an **HTML-comment marker** appended to each successful review comment: `<!-- ai-auto-review reviewed-sha: <40-hex> -->`. `build_review_comment()` appends it; `extract_reviewed_sha()` recovers it on the next run from the `discussions` API. `build_review_comment()` truncates the body *before* appending the marker so the post-side length cap can't sever it.
- This marker is also the **fingerprint** identifying our service's reviews — since `post_comment()` posts `/review-pr` output verbatim with no service header, a review pasted by hand is otherwise indistinguishable. AI-review identification requires marker present **and** `note.author` equal to the token owner (`get_token_username()` via `GET /user`) — this blocks another MR participant from spoofing a marker to hijack the incremental base. If `GET /user` fails it degrades to marker-only matching.
- Base resolution order: marker SHA → `oldrev` argv (A4 fallback) → none (first review, full diff). If the resolved SHA is not present in the shallow clone, it falls back to a full diff.
- Incremental mode diffs `git diff <reviewed_sha>..HEAD`; first review keeps the `origin/<target>...HEAD` (or disjoint `..`) path. If incremental mode resolves but `reviewed_sha..HEAD` has **zero** new commits (a metadata-only `update` — label/title/assignee change), `run_claude_review()` returns `None` and `main()` skips posting entirely.
- Prior context: `collect_prior_comments()` pulls the latest AI review (1 only) + all unresolved user comments from `discussions`, excluding system notes, resolved threads, failure notifications, and older AI reviews. `_format_prior_context()` serializes them into a prompt-injection-immune `<untrusted-comments-<nonce>>` block (per-run random nonce defeats block-escape injection). `fetch_discussions()` failure degrades gracefully to a full review with no prior context.
- `discussions` is `created_at`-ascending, so the **latest** review sits on the last page. `fetch_discussions()` reads `X-Total-Pages` and, when the count exceeds `MAX_DISCUSSION_PAGES`, collects the *last* N pages (not the first) so a busy MR doesn't silently lose its most recent review. If the `X-Total-Pages` header is absent (older GitLab, proxy), it falls back to forward pagination (stop at a short page, capped at `MAX_DISCUSSION_PAGES`).

### Auth model — the load-bearing decision

The container has **no Anthropic API key**. It runs `claude` by mounting the host's `~/.claude` into the container (`docker-compose.yml`), reusing the host user's Claude subscription session. Consequences worth remembering:

- The host `~/.claude` is mounted **read-write**. Claude Code's Bash tool writes shell snapshots under `~/.claude/shell-snapshots/`, and OAuth token refresh also needs write access — a `:ro` mount breaks the Bash tool with `EROFS`. Don't re-add `:ro`.
- If `claude -p` returns non-zero, the most common cause is host session expiry, not container state. Tell the user to re-run `claude login` on the **host**.
- Don't add code paths that assume `ANTHROPIC_API_KEY`; that env var is intentionally absent.

### Claude invocation rules

In `run_claude_review()`, the **first line of the prompt must be the slash command** (`/review-pr\n`) — Claude Code only treats it as a slash command in that position. Output is requested in Korean markdown. The CLI's output is posted to the MR **verbatim** — `post_comment()` prepends no header (the `/review-pr` output already carries its own heading). The only thing it adds is the trailing `<!-- ai-auto-review reviewed-sha: … -->` marker (see Incremental review). Failures instead post the `⚠️` header — see Failure notification.

Tool access is gated by a **static** `--allowed-tools` allowlist (`Read,Glob,Grep,Bash(git:*)`), deliberately **not** `--permission-mode auto`: auto mode consults a classifier model on every Bash call, and when that model is "temporarily unavailable" the unattended `-p` run has no one to fall back to — it stalls for the entire `CLAUDE_TIMEOUT_SEC` and is killed. The static allowlist has no model dependency. Don't switch this back to `auto`.

`Bash(git:*)` does **not** by itself prevent arbitrary command execution — `git -c core.pager=…`, `git -c diff.external=…`, and `!`-aliases all run a shell and all match the `git ` prefix. So the allowlist is a surface-reducer, not an RCE seal. The actual defense against credential theft is **env isolation**: `run_claude_review()` builds a `claude_env` that strips `GITLAB_TOKEN` and `WEBHOOK_SECRET` and passes it as `env=`. `claude` only runs local git (`diff`/`log`/`show`) on the already-cloned repo — clone/fetch finished before it starts — so it needs neither secret. Never pass the full process environment to the `claude` subprocess.

### Failure notification

When `review_runner.py` fails (clone/fetch, `claude` non-zero or empty output, GitLab API errors), it posts a `⚠️ **AI 자동 코드 리뷰 실패**` comment to the MR with an `@REVIEWER_USERNAME` mention — the mention makes GitLab send its standard email, so the user learns of failures without watching container logs. The comment carries the failed stage, a heuristic cause, and the last `STDERR_TAIL_LINES` lines of `claude` stderr in a collapsed block. `ReviewError` carries `(stage, reason, detail)` from a failing stage up to `notify_failure()`. Notification is best-effort: if the comment POST itself fails (token/GitLab down) it is logged only — that overlap of "failure channel" and "failed thing" is an accepted blind spot. `claude` stderr is captured via `stderr=PIPE` (not inherited) and re-logged so docker logs still show it.

### Clone / execution guardrails

`review_runner.py` works from a shallow clone (not API-fetched diffs). Key constants:

| Constant | Value | Why |
|---|---|---|
| `CLONE_DEPTH` | 100 | shallow clone 깊이 — 일반적인 MR 분기 폭 커버 |
| `DEEPEN_STEPS` | (300, 1000) | `merge-base` 미도달 시 점진적 `--deepen`, 최후엔 `--unshallow` |
| `CLAUDE_TIMEOUT_SEC` | 1200 | stalled CLI 강제 종료 (운영 중 실측: 작은 MR도 `/review-pr`이 Read/Grep 컨텍스트 수집으로 10분 초과하는 케이스 발생) |
| `GIT_CLONE_TIMEOUT_SEC` / `GIT_FETCH_TIMEOUT_SEC` | 120 / 60 | git 작업 타임아웃 |
| `STDERR_TAIL_LINES` / `MAX_DETAIL_CHARS` | 20 / 4000 | 실패 알림 코멘트 stderr 블록의 줄 수 / 문자 상한 |
| `MAX_TITLE_CHARS` / `MAX_DESCRIPTION_CHARS` | 200 / 1000 | 프롬프트에 넣기 전 MR 메타데이터 절단 |
| `MAX_DISCUSSION_PAGES` | 5 | discussions 페이지네이션 상한 (per_page=100 → 최대 500개) |
| `MAX_PRIOR_REVIEW_CHARS` | 6000 | 프롬프트에 넣을 직전 AI 리뷰 본문 상한 |
| `MAX_PRIOR_COMMENT_CHARS` / `MAX_PRIOR_COMMENTS_TOTAL` | 1000 / 8000 | 사용자 코멘트 1건 / 전체 합산 상한 |

`cloned_repo()` clones the source branch, fetches the target branch with an explicit refspec, and `_ensure_base_reachable()` deepens until `merge-base` resolves (or falls back to two-dot `..` diff on disjoint history). There is **no** `MAX_FILES` / `MAX_DIFF_CHARS_PER_FILE` truncation — that was the pre-clone, API-diff design and is gone.

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
