# gitlab-auto-review

GitLab 사내 인스턴스에서 본인이 리뷰어로 지정된 MR에 대해 Claude Code CLI(`/review-pr`)로 자동 코드 리뷰 코멘트를 남기는 서비스.

- Anthropic API 미사용 — 호스트의 Claude 구독 세션을 컨테이너에 마운트해서 사용
- 실행 환경: Docker (FastAPI + Claude Code CLI)
- 리뷰 트리거: GitLab webhook의 `Merge request events` 중 `action ∈ {open, update}` & `reviewers`에 지정 username 포함
- `claude` 호출은 `--allowed-tools "Read"` 화이트리스트로 제한 — diff prompt injection으로 인한 RCE 표면 차단

자세한 설계 배경은 [docs/gitlab-ai-reviewer.md](docs/gitlab-ai-reviewer.md), 구현 스펙은 [docs/bmad-output/implementation-artifacts/spec-gitlab-mr-auto-reviewer.md](docs/bmad-output/implementation-artifacts/spec-gitlab-mr-auto-reviewer.md) 참고.

## 사전 준비

1. 호스트에 Claude Code CLI 설치 & 로그인
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude login
   ls ~/.claude/   # credentials 디렉토리 확인
   ```
2. GitLab Personal Access Token 발급 (`api` 스코프) — GitLab → Preferences → Access Tokens
3. Webhook 시크릿용 임의의 긴 랜덤 문자열 준비

## 셋업

### 1. 환경변수 설정

```bash
cp .env.example .env
```

`.env`를 열어서 채운다:

| 키 | 값 | 메모 |
|---|---|---|
| `GITLAB_URL` | `https://gitlab.사내.도메인` | 끝 슬래시 X. `http(s)://`만 허용. embedded auth(`user:pass@`) 거부 |
| `GITLAB_TOKEN` | `glpat-...` | `api` 스코프 PAT |
| `WEBHOOK_SECRET` | 랜덤 16자 이상 | `change-me*`로 시작하면 부팅 거부 |
| `REVIEWER_USERNAME` | `max` | GitLab username (생략 시 `max`) |

`WEBHOOK_SECRET` 생성 한 줄:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 또는
openssl rand -base64 32
```

### 2. 빌드 & 실행

```bash
docker compose up -d --build
docker compose logs -f ai-reviewer
```

부팅 시 `WEBHOOK_SECRET` 길이/패턴, `GITLAB_URL` 스킴, 필수 env 누락 여부를 검증한다. 검증 실패 시 컨테이너가 즉시 종료되므로 로그에서 사유 확인.

`~/.claude` 마운트에서 read-only 권한 에러가 발생하면 `docker-compose.yml`에서 `:ro` 플래그를 제거한다. `claude` 호출이 `--allowed-tools "Read"`로 제한되어 있어 컨테이너가 호스트 파일을 변조하지는 못한다.

> **`sudo docker compose`로 실행하는 환경 주의**: base `docker-compose.yml`은 마운트 소스를 `~/.claude` 로 두는데, `sudo` 환경에서는 `~`이 `/root/.claude`로 확장되어 마운트가 빈 채로 컨테이너가 뜬다(→ `Not logged in` 에러). 이 경우 `docker-compose.override.yml.example`을 `docker-compose.override.yml`로 복사해 절대경로(예: `/home/max/.claude`)로 덮어쓴다. override는 `.gitignore`에 포함되어 커밋되지 않는다.

## GitLab Webhook 등록

리뷰 받고 싶은 프로젝트별로 등록 — **GitLab 프로젝트 → Settings → Webhooks → Add new webhook**.

| 항목 | 값 |
|---|---|
| URL | `http://<서버 호스트>:8080/webhook/gitlab` (HTTPS 권장) |
| Secret Token | `.env`의 `WEBHOOK_SECRET`와 **완전히 동일** (앞뒤 공백 주의) |
| Trigger | ☑ `Merge request events` (다른 건 체크 해제) |
| SSL verification | 사내 인증서 환경에 맞게 설정 |

> 서버가 인터넷에 직접 노출되지 않는다면 GitLab과 같은 사내망에 두거나, 리버스 프록시 뒤에 둔다.

등록 후 webhook 페이지에서 **Test → Merge request events**를 누르면 로그에 `skip:` 또는 `dispatch:` 가 즉시 떠야 한다.

## 동작 확인

```bash
# 헬스체크
curl -s http://localhost:8080/healthz
# {"status":"ok"}

# 잘못된 토큰 → 401 (본문 없음)
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

# 같은 MR을 다시 쏘면 (앞 리뷰가 진행 중인 동안)
# {"status":"skipped","reason":"review in progress"}
```

`docker compose logs -f ai-reviewer` 로 백그라운드 태스크가 GitLab API 호출 → Claude 실행 → MR 노트 게시까지 진행되는지 확인.

## 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| 부팅 직후 즉시 종료 | `WEBHOOK_SECRET` 16자 미만 또는 `change-me*` 시작 | 랜덤 시크릿 재생성 후 `.env` 갱신 |
| 부팅 직후 즉시 종료 + `GITLAB_URL` 에러 | http/https 아닌 스킴, 또는 `user:pass@` 포함 | URL을 `https://gitlab.예제.com` 형태로 정리 |
| `~/.claude` 권한 에러 | read-only 마운트와 OAuth 토큰 갱신 충돌 | `docker-compose.yml`에서 `:ro` 제거 |
| 401 응답만 반복 | 헤더/시크릿 불일치 | GitLab Webhook 설정의 Secret Token과 `.env`의 `WEBHOOK_SECRET` 일치 확인 |
| `review in progress` 응답 반복 | 같은 MR로 webhook 빨리 두 번 옴 (의도된 차단) | 첫 리뷰 끝나면 in-flight set이 자동 해제됨 |
| `Claude 응답이 비어있음` 로그 | 호스트 세션 만료 | 호스트에서 `claude login` 재실행 (컨테이너 재시작 불필요) |
| `claude CLI를 PATH에서 찾을 수 없음` | 이미지 빌드 실패 | `docker compose build` 재실행, `docker compose exec ai-reviewer claude --version` 확인 |
| webhook 이벤트는 오는데 skipped | `action`/`reviewers` 불일치 또는 payload 형식 이상 | `docker compose logs`에서 skip 사유 로그 확인 |
| `review_runner timeout` 로그 | Claude 응답이 180s 초과 stuck | 서버 부하/네트워크 확인. 반복되면 `CLAUDE_TIMEOUT_SEC`/`SUBPROCESS_TIMEOUT_SEC` 조정 |

## 향후 개선 (스코프 외)

- 인라인 코멘트 (GitLab discussions API)
- `~/.claude` 마운트 분리 — 호스트 사용자 자격증명 격리 (별도 서비스 계정 Claude 세션)
- SIGTERM graceful shutdown — 진행 중 자식 프로세스 정리
- 파일 확장자 필터링
- Approve / Request Changes 자동 처리
- 동시 MR 처리 정책 강화 (운영 부하 보고 결정)
