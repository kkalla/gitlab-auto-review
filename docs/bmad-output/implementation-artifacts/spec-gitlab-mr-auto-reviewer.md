---
title: 'GitLab MR 자동 리뷰 서비스 초기 구현'
type: 'feature'
created: '2026-05-19'
status: 'done'
baseline_commit: '905f87840bf8594e79f38378badf98601fb57787'
context:
  - '{project-root}/docs/gitlab-ai-reviewer.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 사내 GitLab에서 본인이 리뷰어로 지정된 MR을 수동으로 코드 리뷰하는 비용이 크고, 1차 피드백 사이클이 늦어진다.

**Approach:** GitLab Webhook으로 MR 이벤트를 받아 리뷰어 username이 `max`이면 컨테이너 내부에서 Claude Code CLI의 `/review-pr` 슬래시 커맨드를 실행해 MR diff를 분석하고, 그 결과를 MR 노트로 자동 게시한다. Anthropic API가 아닌 호스트의 Claude 구독 세션(`~/.claude` 마운트)을 사용한다.

## Boundaries & Constraints

**Always:**
- Webhook 인증은 `X-Gitlab-Token` 헤더와 서버의 `WEBHOOK_SECRET` 정확 일치 검증 후 처리.
- `object_attributes.action`이 `open` 또는 `update`이고, `reviewers[].username`에 `max`가 포함된 경우에만 리뷰 실행.
- 리뷰 실행은 webhook 응답을 먼저 200으로 반환한 후 백그라운드 태스크로 분리(GitLab webhook 타임아웃 회피).
- Claude 호출 시 첫 줄은 반드시 `/review-pr` 슬래시 커맨드여야 한다(diff는 그 뒤에 전달).
- 모든 시크릿(`GITLAB_TOKEN`, `WEBHOOK_SECRET` 등)은 환경변수로만 주입하고, `.env`는 `.gitignore`에 포함.
- MR diff 수집은 상위 N개 파일/길이로 제한해 토큰 폭주 방지(초기값: 파일 10개, 파일당 2000자).

**Ask First:**
- 동시 MR 처리 정책(세마포어/큐) 도입 — 초기 구현에서는 미적용, 운영 중 충돌 발생 시 협의.
- 인라인 코멘트(discussions API) 도입 — 초기 구현에서는 단일 MR 노트로만 게시.
- `Approve` / `Request Changes` 자동 처리 — 초기 구현에서는 비활성.

**Never:**
- Anthropic API 키 사용 금지(구독 세션만 사용).
- `max` 이외의 사용자가 리뷰어로 지정된 MR에 대한 자동 리뷰 게시 금지.
- 시크릿을 소스 코드/이미지/문서에 하드코딩 금지.
- `claude` CLI 호출 시 `--dangerously-skip-permissions` 외 임의 권한 우회 플래그 추가 금지.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 정상 리뷰 | `action=open|update`, reviewers에 `max` 포함, secret 일치 | 200 `{status: "review started"}` 응답 후 백그라운드에서 diff→Claude→MR 노트 게시 | N/A |
| 다른 리뷰어 | secret 일치하나 reviewers에 `max` 없음 | 200 `{status: "skipped"}` | N/A |
| 비대상 액션 | `action=close` 등 | 200 `{status: "skipped"}` | N/A |
| 잘못된 secret | `X-Gitlab-Token` 불일치 | 401 응답 | 본문 없이 401만 반환 |
| GitLab API 실패 | diff 조회 시 4xx/5xx | 백그라운드 태스크에서 예외 로그 후 종료, webhook 응답에는 영향 없음 | 스택 트레이스 stdout에 기록 |
| Claude CLI 실패 | returncode != 0 또는 timeout(120s) | 백그라운드 태스크에서 RuntimeError 로그, MR 노트 미게시 | stderr 로그에 기록 |

</frozen-after-approval>

## Code Map

- `Dockerfile` -- Python 3.11 + Node 20 + Claude Code CLI 글로벌 설치 베이스 이미지.
- `docker-compose.yml` -- 포트 8080 노출, `~/.claude` 볼륨 마운트, `.env` 참조.
- `.env.example` -- 필수 환경변수 템플릿(`GITLAB_URL`, `GITLAB_TOKEN`, `WEBHOOK_SECRET`, `REVIEWER_USERNAME=max`).
- `.gitignore` -- `.env`, `__pycache__/`, `.venv/` 등 제외.
- `requirements.txt` -- `fastapi`, `uvicorn`, `httpx`.
- `webhook_server.py` -- FastAPI 엔트리포인트, 토큰/리뷰어/액션 필터링, 백그라운드 태스크 디스패치.
- `review_runner.py` -- GitLab diff 수집, `/review-pr` 프롬프트로 `claude -p` 호출, MR 노트 게시.
- `README.md` -- 셋업/배포/Webhook 등록 절차.

## Tasks & Acceptance

**Execution:**
- [ ] `.gitignore` -- `.env`, Python 캐시, 가상환경 제외 -- 시크릿 노출 방지.
- [ ] `.env.example` -- 필수 환경변수 키 + 더미값 작성 -- 신규 셋업자 가이드.
- [ ] `requirements.txt` -- `fastapi`, `uvicorn`, `httpx` 명시 -- 빌드 재현성.
- [ ] `Dockerfile` -- Python slim + Node 20 + `@anthropic-ai/claude-code` 글로벌 설치 + uvicorn 실행 -- 컨테이너 부트스트랩.
- [ ] `docker-compose.yml` -- 서비스 정의, `.env` 로드, `~/.claude` ro 마운트, 8080 노출 -- 단일 명령 배포.
- [ ] `webhook_server.py` -- `POST /webhook/gitlab` 핸들러, 토큰 검증, 리뷰어/액션 필터링, `asyncio.create_task`로 `review_runner.py` 실행 -- webhook 수신 계층.
- [ ] `review_runner.py` -- `get_mr_diff`, `run_claude_review`(첫 줄 `/review-pr`), `post_comment` 함수 + `__main__` CLI 진입점 -- 리뷰 실행 계층.
- [ ] `README.md` -- 셋업 순서(로그인→credentials 확인→`docker compose up`→Webhook 등록→curl 테스트) -- 운영 절차 문서화.

**Acceptance Criteria:**
- Given `.env`에 유효한 값이 채워진 상태에서, when `docker compose up -d`를 실행하면, then 컨테이너가 정상 기동되어 `:8080`에서 FastAPI가 응답한다.
- Given 컨테이너가 실행 중일 때, when 잘못된 `X-Gitlab-Token`으로 POST하면, then 401을 응답한다.
- Given `REVIEWER_USERNAME=max`로 실행 중일 때, when reviewers에 `max`가 없는 payload를 보내면, then 200 + `status: skipped`를 응답한다.
- Given `REVIEWER_USERNAME=max`로 실행 중일 때, when reviewers에 `max`가 포함된 `action=open` payload를 보내면, then 200 + `status: review started`를 즉시 응답하고 백그라운드에서 `review_runner.py`가 호출된다.
- Given `review_runner.py`가 호출되었을 때, when Claude CLI를 실행하면, then 전달 프롬프트의 첫 줄이 `/review-pr` 이다(grep 또는 stdout dry-run으로 확인 가능).

## Spec Change Log

<!-- empty -->

## Design Notes

- **`/review-pr` 프롬프트 전달 방식**: `claude -p "<프롬프트>"`에서 프롬프트 본문이 `/`로 시작하면 Claude Code CLI가 슬래시 커맨드로 해석한다. 따라서 prompt 문자열은 반드시 `/review-pr\n\n...diff...` 형태로 시작해야 한다. 다음을 골든 예시로 본다:

  ```python
  prompt = f"""/review-pr

  아래는 GitLab Merge Request의 diff 정보야. PR 리뷰하듯이 분석해줘.

  {diff_text}
  """
  ```

- **백그라운드 분리 이유**: GitLab webhook은 약 10초 이내 응답을 기대한다. Claude CLI는 응답까지 수십 초 걸리므로 `asyncio.create_task`로 분리해 즉시 200을 반환한다. 컨테이너 재시작 시 진행 중 리뷰가 유실될 수 있지만 초기 구현에서는 허용한다.

- **단일 사용자 매칭**: 환경변수 `REVIEWER_USERNAME` 단일 값으로 비교한다. 향후 복수 사용자/팀 확장은 별도 스코프.

## Verification

**Commands:**
- `docker compose config` -- expected: 에러 없이 머지된 컴포즈 설정 출력.
- `docker compose build` -- expected: 이미지 빌드 성공, Claude CLI/Node가 이미지에 포함됨.
- `docker compose up -d && sleep 3 && curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8080/webhook/gitlab -H "X-Gitlab-Token: wrong" -d '{}'` -- expected: `401`.
- `curl -s -X POST http://localhost:8080/webhook/gitlab -H "X-Gitlab-Token: $WEBHOOK_SECRET" -H "Content-Type: application/json" -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"other"}]}'` -- expected: `{"status":"skipped"}`.
- `curl -s -X POST http://localhost:8080/webhook/gitlab -H "X-Gitlab-Token: $WEBHOOK_SECRET" -H "Content-Type: application/json" -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"max"}]}'` -- expected: `{"status":"review started"}` (실제 MR 호출은 GitLab/Claude 통합 환경에서 별도 수동 검증).
- `grep -n "/review-pr" review_runner.py` -- expected: `run_claude_review` 함수 내 prompt 첫 줄에 매칭.

**Manual checks (if no CLI):**
- `docker compose logs ai-reviewer` 에서 webhook 수신/필터링 로그가 보이는지.
- 호스트에서 `claude` 로그인된 상태에서 컨테이너 안 `claude --version`이 동작하는지(`docker compose exec ai-reviewer claude --version`).

### Review Findings

_Code review by `bmad-code-review` on 2026-05-20. Diff: `905f878..0a34f53`. Layers: Blind Hunter + Edge Case Hunter + Acceptance Auditor._

#### Decision needed (3 — all resolved)

- [x] [Review][Decision→Patch] **CRITICAL** `--dangerously-skip-permissions` 제거 + `--allowed-tools "Read"` 화이트리스트로 변경 → diff prompt injection RCE 경로 차단 (resolved: option 1 — Read만 허용으로 좁힘)
- [x] [Review][Decision→Patch] **HIGH** 동일 MR 동시 webhook 중복 처리 → in-flight set 도입, 진행 중인 `(project_id, mr_iid)`는 200 + `skipped` 응답 (resolved: option 1)
- [x] [Review][Decision→Defer] **MEDIUM** `~/.claude` 마운트 권한 정책 — deferred. 사유: Decision #1로 RCE 표면 충분히 좁혀짐, 운영 후 재검토.

#### Patch (28 — all applied)

**CRITICAL**

- [x] [Review][Patch] Webhook 토큰 비교를 `hmac.compare_digest`로 변경 (타이밍 공격 방어) [`webhook_server.py:40`]
- [x] [Review][Patch] (from Decision #1) `claude` argv에서 `--dangerously-skip-permissions` 제거하고 `--allowed-tools "Read"` 사용 — diff prompt injection으로 인한 컨테이너 RCE 차단 [`review_runner.py:83`]

**HIGH**

- [x] [Review][Patch] `subprocess.TimeoutExpired`를 `RuntimeError`로 명시 래핑 + `main()` 단계별 try/except 로 어느 단계 실패인지 식별 [`review_runner.py:82-91`, `:106-112`]
- [x] [Review][Patch] `asyncio.create_task` 결과를 모듈 set에 보관 + `add_done_callback`으로 GC 사일런트 취소 방지 [`webhook_server.py:74`]
- [x] [Review][Patch] (from Decision #2) in-flight set 도입 — `(project_id, mr_iid)` 키로 진행 중 리뷰 추적, 같은 MR 재진입은 200 + `{"status": "skipped", "reason": "review in progress"}` [`webhook_server.py:74`]
- [x] [Review][Patch] `sys.executable` 사용 + `review_runner.py` 절대 경로로 호출, CWD/PATH 의존 제거 [`webhook_server.py:82-86`]
- [x] [Review][Patch] webhook payload type 가드 — `object_attributes`/`reviewers`/`project_id`/`mr_iid`가 비dict/비list/비int 일 때 500 대신 200 + `skipped` [`webhook_server.py:46-74`]
- [x] [Review][Patch] `diff_text` 안에 들어가는 title/description/diff 본문을 명시 구분자(`<diff>...</diff>`)로 감싸고 `/review-pr` 슬래시 명령 하이재킹/마크다운 스푸핑 방지 [`review_runner.py:65-72`]
- [x] [Review][Patch] MR 노트 본문 길이 truncate + "AI 생성, 검증 필요" 면책 + diff 인용 영역 표시 [`review_runner.py:96`]
- [x] [Review][Patch] GitLab API 응답 type 가드 — non-JSON, `diffs` non-list, diff entry non-dict 입력 방어 [`review_runner.py:35-56`]
- [x] [Review][Patch] `claude` 실행 결과 `rc=0` + `stdout` 빈 경우 → 빈 노트 게시 방지 (RuntimeError) [`review_runner.py:82-91`]
- [x] [Review][Patch] `FileNotFoundError`(claude 바이너리 누락) 명시 catch → 식별 가능한 RuntimeError로 변환 [`review_runner.py:82`]

**MEDIUM**

- [x] [Review][Patch] 부트 시 `WEBHOOK_SECRET` 길이/패턴 검증 — 빈 문자열·더미 값(`change-me-*`) 거부 [`webhook_server.py:23`, `review_runner.py:27-28`]
- [x] [Review][Patch] 부트 시 `GITLAB_URL` 스킴/호스트 검증 — `http(s)` 외 거부, 임베디드 auth 거부 (PRIVATE_TOKEN 누출 방어) [`review_runner.py:30`]
- [x] [Review][Patch] `.dockerignore` 추가 (`.env`, `.env.*`, `__pycache__/`, `.venv/`) — 이미지 시크릿 누출 이중 방어
- [x] [Review][Patch] 401 응답 본문 제거 — spec "본문 없이 401만 반환" 일치 (`Response(status_code=401)` 사용) [`webhook_server.py:43`]
- [x] [Review][Patch] `docs/gitlab-ai-reviewer.md`의 `kkalla` 예시 username을 `max`(또는 placeholder)로 정합화 — 컨텍스트 doc과 실제 동작 불일치
- [x] [Review][Patch] description 길이 cap(예: 1000자) 적용 — 토큰 폭주 방지 [`review_runner.py:42-44`]
- [x] [Review][Patch] truncate된 diff에 잔존 `` ``` `` fence 이스케이프 또는 별도 구분자 사용 — fence 조기 종료 방지 [`review_runner.py:54`]
- [x] [Review][Patch] GitLab API 호출에 5xx/429 한정 지수 백오프 1-2회 재시도 [`review_runner.py:38-50`, `:88-94`]
- [x] [Review][Patch] `Request.json()` 호출 실패 시 400으로 변환 + `Content-Length` 상한 검증 [`webhook_server.py:46`]
- [x] [Review][Patch] `_run_review` 서브프로세스에 `asyncio.wait_for` 외곽 타임아웃 가드 (예: 180s) [`webhook_server.py:88`]
- [x] [Review][Patch] 대용량 stdout/stderr 파이프 풀 블록 방지 — stdout DEVNULL 또는 사이즈 cap [`webhook_server.py:85-90`]
- [x] [Review][Patch] `claude` 프롬프트가 ARG_MAX(약 128KB) 근처일 때 stdin으로 전달하도록 변경 [`review_runner.py:82-86`]

**LOW**

- [x] [Review][Patch] 빈 `diffs` 리스트(변경 없는 MR) 명시 처리 — Claude가 빈 내용에 환각 리뷰하지 않도록 [`review_runner.py:47-56`]
- [x] [Review][Patch] `sys.argv` 양수/0 검증 — 부정/0 입력에 명시 에러 [`review_runner.py:115-120`]
- [x] [Review][Patch] `result.stderr or ''` 방어 — None 가능성 가드 [`review_runner.py:90`]
- [x] [Review][Patch] `RuntimeError` 메시지에서 `stderr` 직접 노출 제거 — debug 로그로만, 사용자 메시지에는 rc만 [`review_runner.py:88-91`]

#### Deferred (2)

- [x] [Review][Defer] **LOW** SIGTERM graceful shutdown 부재 [`webhook_server.py`] — deferred, 초기 구현 허용 범위 (spec의 `Ask First` "동시 MR 처리" 정책과 함께 운영 후 재검토)
- [x] [Review][Defer] **MEDIUM** `~/.claude` 마운트 권한 정책 [`docker-compose.yml:10` + `README.md:34-36`] — deferred. 사유: Decision #1로 RCE 표면 충분히 좁혀짐, 운영 후 재검토.

#### Dismissed (7)

- description falsy 체크(`mr.get('description') or '없음'`)가 빈 문자열을 None과 동일 처리 — 동작 차이 미미
- reviewers None entries 처리 후 빈 로그 모호함 — 이미 isinstance 필터로 안전
- docker-compose `version:` 키 누락 — Compose v2에서는 deprecated
- 골든 프롬프트에 "출력은 한국어 마크다운으로" 한 줄 추가 — spec 의도와 부합
- context doc의 `COPY . .` vs 실제 `COPY webhook_server.py review_runner.py ./` — 구현이 더 안전
- context doc의 `environment:` 인라인 vs 실제 `env_file:` — 구현이 더 안전
- "스택 트레이스 stdout 기록" — `logging.basicConfig(stream=sys.stdout)`로 만족
