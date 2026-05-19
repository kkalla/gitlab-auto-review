# gitlab-auto-review

GitLab 사내 인스턴스에서 본인이 리뷰어로 지정된 MR에 대해 Claude Code CLI(`/review-pr`)로 자동 코드 리뷰 코멘트를 남기는 서비스.

- Anthropic API 미사용 — 호스트의 Claude 구독 세션을 컨테이너에 마운트해서 사용
- 실행 환경: Docker (FastAPI + Claude Code CLI)
- 리뷰 트리거: GitLab webhook의 `Merge request events` 중 `action ∈ {open, update}` & `reviewers`에 지정 username 포함

자세한 설계 배경은 [docs/gitlab-ai-reviewer.md](docs/gitlab-ai-reviewer.md), 구현 스펙은 [docs/bmad-output/implementation-artifacts/spec-gitlab-mr-auto-reviewer.md](docs/bmad-output/implementation-artifacts/spec-gitlab-mr-auto-reviewer.md) 참고.

## 사전 준비

1. 호스트에 Claude Code CLI 설치 & 로그인
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude login
   ls ~/.claude/   # credentials 디렉토리 확인
   ```
2. GitLab Personal Access Token 발급 (`api` 스코프)
3. 임의의 긴 문자열을 Webhook Secret으로 준비

## 셋업

```bash
# 1) 환경변수 설정
cp .env.example .env
# .env 열어서 GITLAB_URL, GITLAB_TOKEN, WEBHOOK_SECRET, REVIEWER_USERNAME 채우기

# 2) 빌드 & 실행
docker compose up -d --build

# 3) 로그 확인
docker compose logs -f ai-reviewer
```

`~/.claude` 마운트에서 read-only 권한 에러가 발생하면 `docker-compose.yml`에서 `:ro` 플래그를 제거한다.

## GitLab Webhook 등록

GitLab 프로젝트 → **Settings → Webhooks**

| 항목 | 값 |
|---|---|
| URL | `http://<서버 호스트>:8080/webhook/gitlab` |
| Secret Token | `.env`의 `WEBHOOK_SECRET`와 동일 |
| Trigger | `Merge request events` |

## 동작 확인

```bash
# 헬스체크
curl -s http://localhost:8080/healthz
# {"status":"ok"}

# 잘못된 토큰 → 401
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: wrong" -d '{}'
# 401

# 리뷰어 불일치 → skipped
curl -s -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"other"}]}'
# {"status":"skipped","reason":"reviewer not matched"}

# 리뷰어 일치 → review started (백그라운드 실행)
curl -s -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"object_attributes":{"action":"open","iid":1},"project":{"id":10},"reviewers":[{"username":"max"}]}'
# {"status":"review started"}
```

`docker compose logs -f ai-reviewer` 로 백그라운드 태스크가 GitLab API 호출 → Claude 실행 → MR 노트 게시까지 진행되는지 확인.

## 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| `~/.claude` 권한 에러 | read-only 마운트와 OAuth 토큰 갱신 충돌 | `docker-compose.yml`에서 `:ro` 제거 |
| 401 응답만 반복 | 헤더/시크릿 불일치 | GitLab Webhook 설정의 Secret Token과 `.env`의 `WEBHOOK_SECRET` 일치 확인 |
| `claude` 실행 실패 (rc≠0) | 호스트 세션 만료 | 호스트에서 `claude login` 재실행 (컨테이너 재시작 불필요) |
| Webhook 이벤트는 오는데 skipped | `action` 또는 `reviewers` 불일치 | `docker compose logs`에서 skip 사유 로그 확인 |

## 향후 개선 (스코프 외)

- 인라인 코멘트 (GitLab discussions API)
- 동시 MR 처리 큐 (`asyncio.Semaphore` 또는 Celery)
- 파일 확장자 필터링
- Approve / Request Changes 자동 처리
