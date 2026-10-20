# 리뷰어 196-server 이관 설계

- **작성일**: 2026-07-30
- **상태**: 승인됨 (구현 계획 대기)
- **범위**: `gitlab-ai-reviewer` 컨테이너를 macOS 로컬 podman에서 사내 196-server(Docker + Jenkins)로 완전 이관

## 1. 배경

리뷰 파이프라인이 오늘 두 번 멈췄고, 두 번 다 원인이 **컨테이너가 개인 config 디렉토리를 빌려 쓰는 구조**였다.

1. 커스텀 커맨드를 스킬로 마이그레이션하면서 `~/.claude/commands/review-pr.md`가 유실 → `Unknown command: /review-pr`. `review_runner.py`는 프롬프트 첫 줄이 `/review-pr`인 슬래시 커맨드 호출에 의존하므로 그 파일이 없으면 리뷰가 성립하지 않는다.
2. `block_env_dump.py`를 **단일 파일** bind-mount로 걸어둔 상태에서 호스트에서 그 파일을 수정 → atomic rename으로 inode가 교체되고 podman/virtiofs가 따라가지 못해 컨테이너 안에서 dangling(`-?????????? block_env_dump.py`) → `python3`가 `can't open file`로 exit 2 → Claude Code 훅 규약상 **컨테이너 안 모든 Bash 호출 차단** → 리뷰가 `git diff`조차 못 돌림.

두 사고의 공통 구조는 "리뷰가 동작하기 위한 자산이 이 레포 밖, 개인 대화 환경 안에 있다"는 것이다. CLAUDE.md에도 이미 *"A new deployment must reapply this narrowing on its host (it lives outside this repo)"* 라는 경고가 남아 있다 — 즉 알려진 부채다.

여기에 macOS 로컬 운영 자체의 문제가 겹친다: 노트북이 sleep하면 폴러가 죽고, 호스트에서 `claude-work` 세션을 돌리는 중에는 컨테이너 리뷰와 같은 `.claude.json`에 동시 쓰기가 난다.

## 2. 목표 / 비목표

**목표**

- 리뷰 동작에 필요한 모든 자산을 이 레포 안으로 들여와 이미지에 굽는다 → 호스트 사전준비 0, 마운트 0개
- 상시 가동(노트북 sleep 무관), 개인 세션과의 상태 충돌 제거
- 서브에이전트 6개의 도구 좁히기를 **CI 불변식으로 승격** (문서상의 주의사항 → 테스트)
- 배포를 ags-watchtower와 동일한 Jenkins 파이프라인으로 통일

**비목표**

- 파이프라인 로직 변경 (webhook/slack 트리거, 증분 리뷰, 실패 알림, 프롬프트) — 이번 이관은 **config 조달 경로만** 바꾼다
- macOS 로컬 상시 운영 유지 (폐기)
- 리뷰 품질 튜닝

## 3. 확정된 결정

| # | 결정 | 대안 | 근거 |
|---|---|---|---|
| D1 | **완전 이관** — macOS 로컬 폐기 | 서버+로컬 override / 이중화 | 이중화는 폴러가 양쪽에서 돌아 같은 MR을 두 번 리뷰한다. in-flight 가드는 프로세스 내부 상태라 호스트가 갈리면 무효 — 공유 상태로 빼는 작업이 추가된다 |
| D2 | **리뷰 전용 최소 지시문만** vendoring | 개인 전역 config 재현 | 개인 취향(쪽별 커버리지 80%, web 디자인 정책 등)이 팀 동료 MR 판정에 새지 않는다. 리뷰 기준은 클론된 레포의 프로젝트 `CLAUDE.md`가 담당. 부수 효과로 "무엇이 로드되는지 모른다"는 상태도 해소 |
| D3 | **Jenkins 파이프라인** (watchtower 동일) | ssh + docker compose 수동 | 재사용 가능한 Jenkinsfile이 이미 있고, 배포마다 사람 손이 필요 없다. `.env`가 서버 평문 파일이 아니라 Jenkins credential에 남는다 |
| D4 | **config를 이미지에 굽기 (COPY)** | 서버 호스트 디렉토리 bind mount / 하이브리드 | bind mount는 "코드는 git, 리뷰 기준은 서버 로컬"로 갈라져 §1의 사고 구조가 서버에서 재현 가능한 형태로 남는다. 빠른 튜닝 루프라는 장점은 `review_runner.py`(프롬프트 본체)가 이미 `COPY` 대상이라 재빌드가 기정사실이므로 실익이 거의 없다. 3분 폴링이라 재빌드 비용도 낮다 |

## 4. 타당성 검증 (2026-07-30 실측)

196-server에서 직접 확인했다.

**런타임**: Docker 27.5.1 / **podman 없음** / `$HOME=/home/max` / node·git·claude(2.1.220) 설치됨 / 상주 중: jenkins, ags-watchtower 3종, k8s 컨트롤플레인, GPU 워크로드.

**네트워크(outbound)**: `api.anthropic.com` 401(도달 정상, 인증만 요구) · `deb.nodesource.com` 200 · `registry.npmjs.org` 200 · `pypi.org` 200 · `slack.com/api` 200 · 사내 GitLab `git.sparklingsoda.ai:8443` 302 + `git ls-remote` 성공. **빌드·런타임 모두 막힘 없음.**

**호스트 claude는 쓰이지 않는다**: 서버의 `/usr/bin/claude`와 `~/.claude`(2025-12, stale)는 컨테이너가 자기 이미지 안의 claude + 구운 config를 쓰므로 무관하다. 간섭이 없다는 점이 오히려 장점.

**ags-watchtower는 config 문제의 선례가 아니다**: 마운트가 0개인 순수 API 봇(Notion·Slack·OpenRouter)이라 config dir 문제를 겪은 적이 없다. **배포 방식만** 참조 대상이다.

## 5. 아키텍처

```
[사내 GitLab 태그 푸시]
      │  pollSCM H/3  (사내망 Jenkins로 inbound 웹훅 불가 → 당겨온다)
      ▼
[Jenkins @196-server]  job: gitlab-auto-review-deploy
      │  credential 'gitlab-auto-review-env' (Secret file) → .env
      │  docker compose -p gitlab-auto-review up -d --build     (호스트 docker.sock)
      ▼
[gitlab-ai-reviewer 컨테이너]   ← 마운트 0개
   slack_bot.py (Socket Mode, 아웃바운드 WebSocket만)
      ├─ 채널알림 / 폴링(source SHA 변경) / @멘션
      └─ subprocess → review_runner.py
                        ├─ CLAUDE_CONFIG_DIR=/opt/reviewer-claude   ← 이미지에 구움
                        ├─ git clone (사내 GitLab, shallow)
                        ├─ claude -p "/review-pr\n…"
                        │     --allowed-tools Read,Glob,Grep,Bash(git:*),Task
                        │     └─ Task → 서브에이전트 6개 (전부 Bash(git:*), 이미지 동봉)
                        └─ MR 코멘트(+ reviewed-sha 마커) + Slack DM
```

바뀌는 것은 굵게 표시한 **config 조달 경로**뿐이다. 트리거·증분 리뷰·실패 알림 로직은 그대로다.

## 6. 컴포넌트별 변경

### 6.1 `claude-config/` (신규, 44K 실측)

```
claude-config/
├── commands/review-pr.md         # 호스트 ~/.claude/commands 에서 이동
├── agents/                       # 6개만
│   ├── code-reviewer.md
│   ├── comment-analyzer.md
│   ├── pr-test-analyzer.md
│   ├── silent-failure-hunter.md
│   ├── type-design-analyzer.md
│   └── code-simplifier.md
├── settings.json                 # 신규 최소 구성 (§6.3)
└── scrub/block_env_dump.py       # 개인 레포에서 이미지 안으로 vendoring
```

`rules/`·전역 `CLAUDE.md`·`coding_convention.md`는 **들어가지 않는다**(D2).

측정 근거 — 지금 마운트하는 양과 실제 필요량의 격차:

| | 크기 | 내용 |
|---|---|---|
| 현행 마운트 | `~/.claude` 587M + `~/.claude-work` 38M | 개인 대화 세션 상태 전부(`sessions/`, `projects/`, `history.jsonl`, `shell-snapshots/`, statusline, plugins…) |
| 리뷰가 실제로 필요한 것 | **약 540K** (그중 vendoring 대상 45K) | `commands/review-pr.md`(8K) + 서브에이전트 6개(~35K) [+ D2로 제외된 `rules/` 344K, `CLAUDE.md` 4K, `coding_convention.md` 20K] |

`~/.claude/commands/`에는 `review-pr.md` 파일 하나뿐이고, 6개 에이전트는 모두 `tools: [Read, Grep, Glob, "Bash(git:*)"]`로 좁혀진 상태임을 확인했다(code-simplifier는 Write/Edit도 제거됨).

### 6.2 `Dockerfile`

```dockerfile
RUN npm install -g @anthropic-ai/claude-code@2.1.220   # 현행: 버전 미고정
COPY claude-config/ /opt/reviewer-claude/
ENV CLAUDE_CONFIG_DIR=/opt/reviewer-claude
```

버전 핀을 박는 이유: 현행 `npm install -g @anthropic-ai/claude-code`는 빌드마다 최신을 당겨오므로 **재배포가 조용한 claude 업그레이드**가 된다. 2.1.220은 실제로 리뷰가 돌던 버전이므로 이를 고정하고, 올릴 때는 의도적으로 커밋한다.

`CLAUDE_CONFIG_DIR`은 claude가 `.claude.json`·`shell-snapshots/`·`sessions/`를 쓰므로 **쓰기 가능해야 한다**. 이미지 레이어 위 경로라 컨테이너 안에서 쓰기가 되고, 컨테이너 재생성 시 사라진다 — 보존할 상태가 없으므로(증분 리뷰 기준점은 MR 코멘트의 `reviewed-sha` 마커가 보관) 오히려 바람직하다.

### 6.3 `claude-config/settings.json` (신규, 최소)

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [
          { "type": "command",
            "command": "python3 /opt/reviewer-claude/scrub/block_env_dump.py" }
        ] }
    ]
  }
}
```

개인 훅(memory-tick SessionStart/Stop 등)은 들어가지 않는다. env 덤프 차단만 **컨테이너 내부 절대경로**로 vendoring한다.

이 훅을 유지하는 이유: 리뷰 출력은 팀이 보는 MR 코멘트로 그대로 게시되므로, prompt injection이 env를 덤프하면 `CLAUDE_CODE_OAUTH_TOKEN`이 공개 코멘트에 박힌다. `claude_env`가 `GITLAB_TOKEN`·`WEBHOOK_SECRET`·`SLACK_*`은 이미 벗기지만 OAuth 토큰은 claude의 인증 수단이라 벗길 수 없다. 1.8K짜리 보험이고, 무엇보다 **호스트 경로 의존이 사라지므로 §1의 2번 사고 자체가 불가능해진다.**

### 6.4 `docker-compose.yml`

`volumes:` 블록 **전체 삭제**, `environment.CLAUDE_CONFIG_DIR` 삭제(Dockerfile이 담당). podman/virtiofs/macOS Keychain/절대경로 근거를 설명하는 주석 약 30줄도 함께 정리한다. 남는 것은 `init: true`(claude SIGKILL 시 orphan git 프로세스 reap), `command`, `env_file`, `restart` — watchtower와 동일한 최소 형태.

### 6.5 `Jenkinsfile` (신규)

watchtower의 것을 거의 그대로 복제하고 `-p gitlab-auto-review`, credential id `gitlab-auto-review-env`로 교체한다. Verify 스테이지(compose 서비스 개수 대조 + `state != running || RestartCount > 0` 실패 판정)는 이 레포에도 유효하다 — 부팅 시 `WEBHOOK_SECRET` 길이/`GITLAB_URL` 스킴/필수 env 누락을 검증해 즉시 종료하는 설계이므로 crash-loop이 정확히 잡힌다.

트리거는 `refs/tags/*` + Refspec `+refs/tags/*:refs/remotes/origin/tags/*`, 폴링은 `pollSCM('H/3 * * * *')`.

**폴링 대상 remote는 사내 GitLab(`gitlab`)이다.** 이 레포는 remote가 둘(`origin`=GitHub 공인망, `gitlab`=사내 `git.sparklingsoda.ai:8443`)인데, 사내망 Jenkins가 공인망 GitHub을 폴링할 이유가 없고 watchtower와도 어긋난다.

### 6.6 `.env` / credential

Jenkins credential `gitlab-auto-review-env`(Secret file)에 업로드한다. 라이브 `.env`를 그대로 올리지 말고 다음을 정리한다:

- **`NOTION_TOKEN`·`NOTION_TASKS_DB_ID`·`NOTION_PROJECTS_DB_ID` 제외** — Notion 현황 조회가 96 레포로 분리된 뒤 이 레포에서 읽지 않는 변수다. 죽은 시크릿을 새 호스트로 옮길 이유가 없다. (`.env.example`에는 이미 없다 — 라이브 `.env`에만 남은 잔재다.)
- **`WEBHOOK_SECRET` 제외** — `webhook_server.py:35`에서만 읽고 `slack_bot.py`는 그 모듈을 import하지 않는다. 기본 CMD가 Slack 봇 모드이므로 불필요하다. (README의 "부팅 시 `WEBHOOK_SECRET` 검증"은 webhook 모드 한정 서술이다. `review_runner.py:113`의 strip 목록에 이름이 남아 있지만 "있으면 벗긴다"이므로 부재는 무해하다.) webhook 모드로 되돌릴 때만 다시 넣는다.
- **`POLL_INTERVAL_SEC` 명시** — 현재 라이브 `.env`에 없어 기본값 300으로 돌고 있다. 서버는 상시 가동이므로 의도한 값을 명시적으로 적는다.

즉 새 credential에 담기는 것은 `GITLAB_URL`, `GITLAB_TOKEN`, `REVIEWER_USERNAME`, `CLAUDE_CODE_OAUTH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `REVIEWER_SLACK_ID`, `POLL_INTERVAL_SEC`, `LOG_LEVEL` — 시크릿 4종이 3종으로 줄고 Notion 3종이 빠진다.

credential 변경은 pollSCM(git 감지)이 잡지 못하므로 값만 바꿀 때는 Jenkins "지금 빌드" 또는 `docker compose -p gitlab-auto-review up -d --force-recreate`로 수동 트리거한다.

## 7. 남는 macOS 의존

`claude setup-token`은 브라우저 OAuth 플로우라 대화형 머신에서만 발급된다. 따라서 토큰 만료(~1년)마다:

1. macOS에서 `claude setup-token` 실행
2. `.env`의 `CLAUDE_CODE_OAUTH_TOKEN` 갱신
3. Jenkins credential `gitlab-auto-review-env` 재업로드
4. 수동 재배포 트리거

"로컬 폐기"라도 이 경로는 남는다. `claude -p`가 `Not logged in`으로 실패하면 1순위 용의자다.

## 8. 컷오버

**macOS 정지가 먼저다.** 둘 다 뜨면 폴러가 양쪽에서 돌아 같은 MR을 두 번 리뷰한다(D1 근거).

1. macOS: `podman compose -p automation-stack stop ai-reviewer` — 정지만, 제거하지 않음
2. `~/00_Projects/compose.yaml`의 `include`에서 `97_gitlab-auto-review/docker-compose.yml` 제거
3. Jenkins credential `gitlab-auto-review-env` 업로드 (`NOTION_*` 제외한 `.env`)
4. Jenkins job `gitlab-auto-review-deploy` 생성 (사내 GitLab remote, `refs/tags/*`, Refspec)
5. 태그 푸시 → 3분 내 빌드 → Verify 통과 확인
6. E2E 검증: `@ags-watchtower <MR URL>` 수동 멘션 1회 → MR 코멘트 게시 + Slack DM 수신 확인
7. 며칠 관찰 후 macOS 컨테이너/이미지 제거

**롤백**: 5~6단계 실패 시 서버 `docker compose -p gitlab-auto-review down`, macOS `include` 복원 후 `podman compose up -d ai-reviewer`. macOS `.env`는 7단계까지 보존하므로 즉시 복귀 가능하다.

**컷오버 창의 알려진 손실**: 폴러는 기동 시 첫 순회를 baseline으로 잡아 기존 MR을 일괄 리뷰하지 않는다. 대신 정지~기동 사이의 push 증분은 한 번 놓친다. 기존 설계에 이미 있는 특성이며 수동 멘션으로 커버한다.

## 9. 테스트

기존 `make test`(순수함수 유닛테스트: 마커 파싱, 코멘트 필터링, injection 방어)는 그대로 통과해야 한다. 여기에 `tests/test_deploy_config.py`를 신규 추가한다.

| 검증 | 이유 |
|---|---|
| `claude-config/agents/` 6개 전원 `Bash(git:*)`만 허용, `Write`/`Edit` 없음 | CLAUDE.md에 *"새 배포는 호스트에서 다시 적용해야 한다"*고 적힌 지뢰를 **CI 불변식으로 승격**. 이번 이관의 최대 실익 |
| `claude-config/commands/review-pr.md` 존재 + 6개 에이전트 이름 전부 언급 | `Unknown command: /review-pr` 및 서브에이전트 조용한 증발의 재발 차단 |
| `docker-compose.yml`에 `volumes:` 없음 | 호스트 경로 의존이 슬쩍 되돌아오는 것 차단 |
| Dockerfile의 claude 버전 핀 존재 | 재배포가 조용한 업그레이드가 되는 것 차단 |
| `.env.example`의 변수 집합 ↔ 코드가 읽는 env 집합 양방향 대조 | 죽은 변수(코드가 안 읽는데 example에 있음)와 누락(필수인데 example에 없음)을 둘 다 잡는다 |

**정규식 앵커 주의**: 설정 파일을 정규식으로 검증하는 테스트는 주석 산문까지 매치된다(과거에 `-p 오타로`라는 주석을 프로젝트명으로 오독한 사례가 있다). 앵커에는 `docker compose -p` 같은 커맨드 형태를 포함시킨다.

**테스트로 덮이지 않는 것**: 위 표의 env 대조 테스트는 작성 시점에 이미 통과한다(현재 `.env.example`은 깨끗하다) — 앞으로의 드리프트를 막는 회귀 가드이며, `NOTION_*`/`WEBHOOK_SECRET`를 credential에서 빼는 §6.6 작업 자체를 검증하지는 못한다. 라이브 `.env`와 Jenkins credential은 git 밖에 있어 테스트 대상이 아니다. 그 단계는 §8 체크리스트의 수동 확인으로 남고, 실제 검증은 §8-6의 E2E(멘션 1회 → 리뷰 게시)가 담당한다. 각 테스트를 작성할 때는 해당 불변식을 일부러 깨뜨려 RED를 확인한 뒤 GREEN으로 되돌린다.

## 10. 리스크

| 리스크 | 완화 |
|---|---|
| 서버 리소스 경합 (k8s 컨트롤플레인 + GPU 워크로드 상주) | 리뷰는 CPU·네트워크 위주로 부하가 크지 않다. `CLAUDE_TIMEOUT_SEC=1200`이 이미 stalled CLI를 끊는다. 실측 후 필요하면 조정 |
| 이미지 빌드가 npm/nodesource에 의존 | §4에서 200 확인. 버전 핀으로 재현성 확보 |
| vendoring한 config가 호스트 `~/.claude`와 드리프트 | **의도된 분기다** — 레포가 리뷰어의 유일한 진실원이다. 호스트 쪽 변경이 리뷰어에 자동 반영되지 않는 것이 §1 사고의 재발 방지책 |
| `CLAUDE_CONFIG_DIR`이 컨테이너 재생성 시 초기화 | 보존할 상태가 없다(§6.2). 증분 리뷰 기준점은 MR 코멘트 마커에 있다 |
| OAuth 토큰 만료 | §7 갱신 절차. 실패 시 `⚠️ AI 자동 코드 리뷰 실패` 코멘트 + Slack DM으로 드러난다 |

## 11. 후속 (이 스펙 범위 밖)

- CLAUDE.md의 Auth model / Clone·execution guardrails 절 재작성 — macOS Keychain·podman virtiofs·호스트 절대경로 근거가 전부 무효가 된다
- `~/.claude/commands/review-pr.md`와 6개 에이전트를 호스트에서 정리할지 여부(개인 대화 세션에서도 `/review-pr`을 쓰므로 유지가 맞을 것으로 보이나 확인 필요)
