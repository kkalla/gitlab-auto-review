# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI service that receives GitLab MR webhooks and posts AI code review comments back to the MR via the GitLab API. Review generation is delegated to the Claude Code CLI (`claude -p`) using the `/review-pr` slash command — **not** the Anthropic API.

## Run / debug

```bash
# Production-like (Docker)
docker compose up -d --build
docker compose logs -f ai-reviewer

# Production-like (Podman, macOS — 권장 타깃)
# 1) Linux VM 준비 (최초 1회). claude+node+git이라 리소스는 넉넉히.
podman machine init --cpus 4 --memory 4096   # 이미 있으면 생략
podman machine start
# 2) podman compose는 docker-compose/podman-compose 중 설치된 provider를 호출한다.
podman compose up -d --build
podman compose logs -f ai-reviewer
# 마운트되는 ${HOME}/.claude·${HOME}/.claude.json은 호스트에서 `claude login`이
# 끝나 있어야 한다(컨테이너가 그 세션을 재사용). macOS엔 SELinux가 없어 `:z` 불필요.

# Local Python (still requires env vars from .env)
pip install -r requirements.txt
uvicorn webhook_server:app --reload --port 8080

# Trigger review_runner directly (bypasses webhook gate)
python review_runner.py <project_id> <mr_iid> [oldrev]

# Unit tests (pure functions in review_runner.py)
# 격리 venv에서 실행 — 호스트 conda(cv2 등) 오염을 피한다. venv는 멱등 생성.
make test

# (수동) 직접 venv 구성
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q

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

### Two trigger entrypoints (둘 중 하나만 띄움)

`review_runner.py`는 두 트리거가 공유한다. **하나의 컨테이너 이미지, 두 진입점:**

1. **`webhook_server.py`** (webhook 모드) — GitLab MR webhook을 공개 HTTP로 받음. `WEBHOOK_SECRET` 필요, `ports: 8080` 노출.
2. **`slack_bot.py`** (Slack 봇 모드, **기본 CMD**) — Slack Socket Mode 봇. **공개 inbound 포트 불필요** — 봇이 Slack으로 아웃바운드 WebSocket을 연다(방화벽/NAT 무관). 트리거 **세 가지**: **자동·채널알림**(GitLab Slack notification이 뿌린 MR 링크를 `message` 이벤트로 잡음 — 주로 MR open) / **자동·폴링**(봇이 `POLL_INTERVAL_SEC`마다 reviewer 지정 열린 MR의 source SHA를 확인해 변경분을 리뷰 — **push 증분의 길**; GitLab Slack 알림은 MR push를 채널에 안 띄우므로 필요) / **수동·멘션**(`@ags-watchtower <MR URL>`). 설정은 `SLACK_SETUP.md`.

```
(자동·채널) GitLab 알림 → 채널 message → slack_bot.py (handle_channel_message)
(자동·폴링) POLL_INTERVAL_SEC마다 GET /merge_requests?reviewer_username=… → SHA 변경분 (_poll_loop)
(수동·멘션) @봇 <MR URL> → slack_bot.py (app_mention)
                 ├─ MR URL/목록에서 project_id·mr_iid 해석
                 ├─ (멘션/채널만) 스레드 ack 답글
                 └─ subprocess ──> review_runner.py <project_id> <mr_iid>
                                     └─ (기존 파이프라인) + 완료/실패 시 Slack DM
```

- `slack_bot.py`는 `webhook_server.py`와 같은 격리 원칙: review_runner를 **subprocess**로 띄워 claude 타임아웃 SIGKILL/크래시가 봇 WebSocket을 죽이지 못하게 한다. 리뷰는 Bolt 핸들러/폴러를 막지 않도록 별도 `threading.Thread`에서 실행.
- 세 트리거는 `_dispatch_review()`를 공유하고 `(project_id, mr_iid)` in-flight 가드로 중복을 막는다 — 멘션 메시지는 `app_mention`·`message` 둘 다 발생하지만 먼저 잡은 쪽만 실행된다. 봇 자신의 답글이 `message`로 되돌아와 재트리거되는 것은 Bolt 기본 `ignoring_self_events`가 막는다.
- **폴러**(`_poll_loop`, 데몬 스레드)는 첫 순회를 **baseline**으로 잡고(봇 기동 시 기존 MR 일괄 리뷰 방지) 이후 source SHA가 바뀐 MR만 트리거한다. 폴링 트리거는 `channel`/`say` 없이 `_dispatch_review`를 호출해 **스레드 답글 없이 조용히** 돌고(결과는 review_runner의 MR 코멘트 + DM), `_post`는 `channel`이 없으면 no-op이다. `POLL_INTERVAL_SEC=0`이면 폴러 비활성화. 봇 재시작 시 baseline이 비어 그 사이 push는 한 번 놓칠 수 있다(수동 멘션으로 커버).
- 봇은 oldrev를 넘기지 않는다 — 증분 리뷰는 review_runner가 MR 코멘트의 `reviewed-sha` 마커로 자체 처리하므로 `@멘션` 수동 트리거에서도 정상 동작한다.
- `slack_bot.py`만 `slack_bolt`에 의존한다. `review_runner.py`/`slack_notifier.py`/`notion_status.py`는 `httpx`만 쓴다 — review_runner의 테스트 의존성을 가볍게 유지하기 위함(테스트는 `slack_bolt` 미설치로도 통과).

### Notion 현황 (`notion_status.py`, /task-status·/project-status)

MR 리뷰와 무관한 Slack 봇 부가 기능. 슬래시 커맨드 **두 개**가 Notion을 REST API로 전체 페이지네이션 조회한 뒤 **로컬에서 분류**해 mrkdwn 리포트로 답한다. Socket Mode라 커맨드도 WebSocket으로 들어온다(Request URL 불필요) — 핸들러는 `ack()`만 즉시 하고(3초 제한) 조회는 별도 스레드에서 `respond()`로 답한다(`_respond_notion_report` 공유). 기본 ephemeral, `public` 인자면 채널 공개.

- **`/task-status [지연|차단|진행|대기|완료|담당자이름] [public]`** — Tasks DB(`NOTION_TASKS_DB_ID`) 태스크 리포트. 1차 그룹은 **프로젝트 티어**: ① 진행중 프로젝트 → ② 예정(미시작 태스크 + `Schedule (Plan)` 있음) → ③ ⚠️ 정합성 이슈(종료 프로젝트 Done/Fail/Drop의 미완료 태스크) → ④ 기타. 다중 프로젝트는 ①>③. 티어 안은 기존 urgency 분류(지연→차단→진행중→대기) 순 + 아이템 이모지(🔴🚧🔵⏸️).
- **`/project-status [public]`** — Projects DB(`NOTION_PROJECTS_DB_ID`) 프로젝트 현황(진행중/예정/종료) + 완료율. 완료율은 `Completion` rollup을 API로 읽지 않고(관계 25개 초과 시 부정확) `fetch_tasks` 결과에서 로컬 계산(`project_task_counts`), 태스크 0개면 생략.
- urgency 분류 우선순위: Done/Drop → 지연(`Delayed` 상태 또는 계획 일정 경과) → 차단(미완료 `Blocked by` 존재) → 진행중 → 대기. blocker 상태 해석을 위해 Done 포함 전체를 가져온다. 조회 결과에 없는 blocker는 미완료로 간주(보수적).
- 프로젝트 메타(제목·상태)는 Projects DB **벌크 쿼리 1회**로 맵을 만들고(`fetch_projects`+`parse_project`), 맵에 없는 id(아카이브 등)만 페이지 GET 폴백(`project_meta_resolver`, dict 캐시). 조회 실패는 빈 메타로 강등 — 빈 status는 티어 ②/④로 떨어지고 리포트는 렌더된다(예외 전파 금지).
- Notion API 버전은 `2022-06-28` 고정 — `databases/{id}/query`가 기본 data source를 직접 질의한다(2025-09 버전의 data_source id 단계 회피).
- `NOTION_TOKEN` 미설정이면 `enabled()` False → 두 커맨드는 안내만 답하고 봇 부팅엔 영향 없음(slack_notifier와 같은 원칙). `NOTION_TOKEN`은 `_TOKEN` 접미사라 review_runner의 fail-secure denylist가 claude env에서 자동으로 가린다.
- 테스트는 `tests/test_notion_status.py` — 순수 함수(parse/classify/tier/filter/format)만. 리졸버는 `_fetch_page_meta` monkeypatch로 폴백 경로만 검증.
- Slack 앱에 Slash Command **2개** 등록(`/task-status`는 신규 등록 필요) + Notion 통합의 Tasks·Projects DB 연결 필요 — `SLACK_SETUP.md` §8.

### Slack 알림 (`slack_notifier.py`)

`review_runner.py`는 리뷰 **완료**(`notify_slack_success`) 시 리뷰어+assignee에게, **실패**(`notify_failure`) 시 리뷰어에게 Slack DM을 보낸다. 모두 **best-effort** — `SLACK_BOT_TOKEN`이 없으면 `slack_notifier.enabled()`가 False라 조용히 no-op이고, 전송 실패도 예외를 던지지 않는다(MR 코멘트가 이미 게시된 뒤이므로 알림 누락이 리뷰 결과를 깨선 안 됨).

- 리뷰어 DM 대상은 `REVIEWER_SLACK_ID`(Slack member ID)로 직접 지정.
- assignee는 **GitLab 이메일 → Slack `users.lookupByEmail`**로 매핑. GitLab MR의 assignee 객체엔 이메일이 없어 `get_user_public_email()`이 `/users/:id`의 `public_email`을 별도 조회한다. 공개 이메일이 비었거나 Slack 이메일과 다르면 그 assignee는 건너뛴다(설계상 허용된 누락).

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

The container authenticates `claude` with a **long-lived OAuth token** (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token` on the host) — a Claude **subscription** token, **not** `ANTHROPIC_API_KEY`. Why a token env var instead of just mounting the host session:

- **macOS Keychain isn't portable.** The host (macOS) stores its OAuth token in the Keychain, which the Linux container can't read. Mounting `~/.claude` / `~/.claude.json` carries config but **not** the token — `claude -p` then fails with `Not logged in`. `claude setup-token` prints a ~1-year token that bypasses Keychain; it flows `.env` → compose `env_file` → `review_runner`'s `claude_env` → the `claude` subprocess.
- `~/.claude` is still mounted **read-write** (Claude Code's Bash tool writes `~/.claude/shell-snapshots/`; a `:ro` mount breaks it with `EROFS`). But `~/.claude.json` is **not** mounted directly: a single-file bind mount + podman virtiofs breaks claude's atomic-rename rewrite of that file (it vanishes → `Claude configuration file not found`). Instead it's mounted **read-only at `/seed/.claude.json`** and the compose `entrypoint` copies it to a container-local `/root/.claude.json` on start. See `docker-compose.yml`.
- If `claude -p` fails with `Not logged in`, the token is missing/expired — re-run `claude setup-token` on the host and update `CLAUDE_CODE_OAUTH_TOKEN` in `.env`. (The `/login` short-lived OAuth token dies in ~8h with no refresh; the `setup-token` long-lived token does not — use setup-token.)
- `ANTHROPIC_API_KEY` stays intentionally absent — auth is the subscription OAuth token, not the API.
- **Security trade-off**: `CLAUDE_CODE_OAUTH_TOKEN` must reach the `claude` subprocess (it *is* the auth), so `claude_env` passes it through — unlike the stripped secrets. With `ALLOWED_TOOLS` permitting full `Bash`, a prompt-injection could read it from env; narrowing the allowlist back to `Bash(git:*)` shrinks that surface.

### Claude invocation rules

In `run_claude_review()`, the **first line of the prompt must be the slash command** (`/review-pr\n`) — Claude Code only treats it as a slash command in that position. Output is requested in Korean markdown. The CLI's output is posted to the MR **verbatim** — `post_comment()` prepends no header (the `/review-pr` output already carries its own heading). The only thing it adds is the trailing `<!-- ai-auto-review reviewed-sha: … -->` marker (see Incremental review). Failures instead post the `⚠️` header — see Failure notification.

Tool access is gated by a **static** `--allowed-tools` allowlist (`Read,Glob,Grep,Bash(git:*),Task`), deliberately **not** `--permission-mode auto`: auto mode consults a classifier model on every Bash call, and when that model is "temporarily unavailable" the unattended `-p` run has no one to fall back to — it stalls for the entire `CLAUDE_TIMEOUT_SEC` and is killed. The static allowlist has no model dependency. Don't switch this back to `auto`. `Task` is required — `/review-pr` spawns specialized subagents (code-reviewer, silent-failure-hunter, …) via Task; without it the review collapses to a single pass.

`Bash(git:*)` does **not** by itself prevent arbitrary command execution — `git -c core.pager=…`, `git -c diff.external=…`, and `!`-aliases all run a shell and all match the `git ` prefix. So the allowlist is a surface-reducer, not an RCE seal. **Subagent caveat**: the parent `--allowed-tools` does **not** propagate to Task-spawned subagents — each runs under its own definition. So `/review-pr`'s subagents are *separately* narrowed in the host `~/.claude/agents/` — all six (code-reviewer, comment-analyzer, pr-test-analyzer, silent-failure-hunter, type-design-analyzer, code-simplifier) end up at `tools: [Read, Grep, Glob, Bash(git:*)]`, with code-simplifier additionally losing Write/Edit. **A new deployment must reapply this narrowing on its host** (it lives outside this repo). The deeper defense is **env isolation**: `claude_env` strips `GITLAB_TOKEN`, `WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. But `CLAUDE_CODE_OAUTH_TOKEN` can **not** be stripped (claude needs it to authenticate), so the `Bash(git:*)` scoping on **both** parent and subagents is what guards that token from injection-driven exfiltration. `claude` only runs local git (`diff`/`log`/`show`) on the already-cloned repo, so the narrowing costs nothing. Never pass the full process environment to `claude`.

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

Consumed at import time (`os.environ[...]` — missing keys crash on boot, by design). 어떤 키가 필수인지는 **어느 진입점을 띄우느냐**에 따라 다르다:

공통 (`review_runner.py`):
- `GITLAB_URL` — base URL, no trailing slash (stripped defensively anyway).
- `GITLAB_TOKEN` — PAT with `api` scope.
- `CLAUDE_CODE_OAUTH_TOKEN` — `claude setup-token`(호스트 실행)으로 발급한 구독 OAuth 토큰. `claude` 인증에 쓰인다 — 컨테이너는 macOS Keychain을 못 읽으므로 **필수**(없으면 `claude -p`가 `Not logged in`). import가 아니라 claude 실행 시점에 필요. Auth model 섹션 참고.
- `REVIEWER_USERNAME` (default `max`) — `webhook_server.py`에선 `reviewers[].username` 필터, `review_runner.py`에선 실패 알림 코멘트의 `@`-mention 대상, `slack_bot.py` 폴러에선 폴링 대상 MR의 `reviewer_username` 필터.

webhook 모드 (`webhook_server.py`):
- `WEBHOOK_SECRET` — must match GitLab webhook Secret Token. (Slack 봇 모드에선 불필요.)

Slack 봇 모드 (`slack_bot.py`):
- `SLACK_BOT_TOKEN` (`xoxb-…`) — bot 토큰. 스코프 `chat:write, app_mentions:read, users:read, users:read.email, im:write`. 봇 부팅 필수이며, `review_runner.py`에선 **선택**(없으면 DM 알림만 no-op).
- `SLACK_APP_TOKEN` (`xapp-…`) — App-Level 토큰, `connections:write`. Socket Mode 전용. 봇 부팅 필수.
- `REVIEWER_SLACK_ID` (`U…`) — 완료/실패 DM을 받을 리뷰어 member ID. 선택 — 비면 리뷰어 DM 생략(assignee DM은 이메일 매핑으로 별도).
- `POLL_INTERVAL_SEC` (default 300) — 폴러 주기(초). reviewer 지정 열린 MR의 source SHA를 이 주기로 확인해 push 증분을 자동 리뷰한다. `0`이면 폴러 비활성화(채널알림·멘션만). slack_bot 전용.
- `NOTION_TOKEN` — Notion internal integration secret. 선택 — 비면 `/task-status`·`/project-status`만 비활성(안내 답변). 대상 DB(Tasks·Projects)에 통합 연결 필요.
- `NOTION_TASKS_DB_ID` (default 제1연구센터 Tasks) — `/task-status`가 조회할 Tasks DB ID (완료율 계산에도 사용).
- `NOTION_PROJECTS_DB_ID` (default 제1연구센터 Projects) — `/project-status`와 `/task-status` 티어 판정이 조회할 Projects DB ID.

## Conventions

- Korean commit messages, Conventional Commits prefixes (`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`). Matches existing git log.
- User-facing strings (MR comment body, prompt, log skip reasons) are Korean; keep them consistent if you add new ones.
- BMad tooling lives under `_bmad/`, `.claude/skills/`, `.agents/skills/` — installed as scaffolding, not part of the runtime. Don't pull from it at import time.
