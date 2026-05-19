---
title: 'GitLab MR 자동 리뷰 서비스 초기 구현'
type: 'feature'
created: '2026-05-19'
status: 'in-progress'
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
