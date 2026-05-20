---
stepsCompleted: [1, 2, 3, 4]
inputDocuments: []
session_topic: '자동 리뷰 서비스 실행 중 에러 발생 시 사용자 알림(노티) 방안'
session_goals: '에러 발생을 놓치지 않고 알 수 있는 현실적인 알림 방안 발굴 및 구현 후보 좁히기'
selected_approach: 'progressive-flow'
techniques_used: ['What If Scenarios', 'Morphological Analysis', 'SCAMPER', 'Decision Tree Mapping']
ideas_generated: 12
context_file: ''
session_active: false
workflow_completed: true
---

# Brainstorming Session Results

**Facilitator:** Max
**Date:** 2026-05-20

## Session Overview

**Topic:** GitLab 자동 리뷰 서비스(webhook_server → review_runner)가 실행 중 에러로 죽을 때 사용자에게 알림을 보내는 방안
**Goals:** 컨테이너 로그에만 남고 사용자가 모르는 실패를 감지/통보할 현실적 방안 발굴, 구현 후보로 좁히기

### Session Setup

현재 구조상 review_runner 서브프로세스가 rc!=0로 종료되면 webhook_server가 로그만 남기고 끝. 사용자(max)는 별도로 로그를 보지 않으면 실패를 인지할 수 없음. 이 사각지대를 메우는 알림 메커니즘을 설계하는 세션.

## Technique Selection

**Approach:** Progressive Technique Flow
**Journey Design:** 확산 탐색 → 패턴 인식 → 아이디어 발전 → 실행 계획

**Progressive Techniques:**

- **Phase 1 - Exploration:** What If Scenarios — 알림 채널/트리거/방식 아이디어 확산
- **Phase 2 - Pattern Recognition:** Morphological Analysis — 실패단계 × 감지지점 × 채널 × 심각도 격자 매핑
- **Phase 3 - Development:** SCAMPER — 선정 후보 7개 렌즈로 정교화
- **Phase 4 - Action Planning:** Decision Tree Mapping — 구현 경로 트리화

**Journey Rationale:** 알림 설계는 채널·트리거 후보가 다양해 넓게 펼친 뒤 파라미터 격자로 체계적으로 좁히는 흐름이 적합.

## Technique Execution Results

### Phase 1 — What If Scenarios

**핵심 질문:** "코드 한 줄 안 짜도 알림을 받을 수 있다면?" / "알림이 '실패했다'가 아니라 '원인+해결법'까지 담겨 있다면?"

**생성 아이디어:**

- **[탐색 #1] 컨테이너 다운 노티** — `restart: on-failure` + 외부 모니터링. 한계: review_runner는 서브프로세스라 컨테이너는 안 죽음 → webhook_server 자체 다운 때만 유효.
- **[탐색 #2] 로그 watcher 사이드카** — `ai-reviewer` 로그를 tail 하다 `[ERROR]`/`rc=1` 패턴 감지해 발송.
- **[탐색 #3] GitLab을 알림판으로** — 실패 시 해당 MR에 코멘트를 달아버림. GitLab이 이미 메일 연동되어 있어 추가 인프라 0. ← **사용자 채택**
- **[탐색 #4] @멘션 코멘트** — 코멘트에 `@max` 멘션을 넣어 GitLab 메일 발송을 확실히 트리거. ← **최종 채택**
- **[탐색 #5] GitLab To-Do API** — 직접 To-Do 꽂기. 메일 + 좌상단 빨간 뱃지.
- **[탐색 #6] MR 라벨** — `ai-review-failed` 자동 부착. 알림은 약하나 필터링 용이.
- **[탐색 #7] commit/MR status 빨간불** — 시각적이나 메일 없음.
- **[탐색 #8] 전용 이슈 생성** — 추적은 좋으나 무거움.
- **[탐색 #9] 실패 원인까지 담기** — 단순 "rc=1"이 아니라 실패 단계 + stderr 꼬리 + 추정 원인/해결법까지 코멘트에 포함. ← **채택**

**Phase 1 결론:** 새 인프라 없이 GitLab 메일 연동에 얹는 `@멘션 코멘트` + `실패 원인 포함` 으로 컨셉 확정.

### Phase 2 — Morphological Analysis

**격자: 실패 단계 × @멘션 코멘트 커버 여부**

| # | 실패 단계 | project_id/iid 인지 | @멘션 코멘트 가능 |
|---|---|---|---|
| 1 | webhook_server 자체 다운 | — | ❌ 코드 자체가 안 돎 |
| 2 | 페이로드 파싱 실패 | ❌ | ❌ 댓글 달 MR 모름 |
| 3 | review_runner clone 실패 | ✅ | ✅ |
| 4 | claude 실행 실패 (rc=1) | ✅ | ✅ |
| 5 | post_comment 자체 실패 | ✅ | ❌ 알림 수단 = 실패한 수단 |

**생성 아이디어:**

- **[패턴 #10] 2차 fallback 채널** — GitLab POST 실패(5번) 시에만 SMTP 직발송 또는 Slack/Discord incoming webhook 한 방.
- **[패턴 #11] 외부 헬스체크** — 컨테이너 다운(1번)을 healthchecks.io 류 무료 cron ping으로 별도 보강.
- **[패턴 #12] 사각지대 = 허용된 손실** — 1·2·5번은 드무니 로그로만 두고, 가장 빈번한 3·4번만 커버하는 80/20.

**Phase 2 결론 (사용자 결정):** MVP — **3·4번만 커버**. 1·2·5번은 허용된 손실.

### Phase 3 — SCAMPER

선정 컨셉을 4개 렌즈로 정교화:

- **[S] Substitute** — 코멘트 발송 주체는 webhook_server가 아니라 **review_runner 본인**. 실패 단계와 stderr를 가장 잘 아는 게 review_runner이기 때문.
- **[C] Combine** — 정상 리뷰(`🤖 **AI 자동 코드 리뷰**`)와 같은 패밀리로 `⚠️ **AI 자동 코드 리뷰 실패**`. 멘션은 본문에 `@max` 삽입.
- **[M] Modify** — 코멘트 내용: 실패 단계 + stderr 마지막 20줄(`<details>` 접은 코드블록) + 추정 원인 한 줄.
- **[E] Eliminate** — 스팸: open/update 재시도로 코멘트 도배 가능하나 MVP에선 그냥 둠(반복 실패가 오히려 인지에 도움).

**사용자 결정:** 멘션 대상은 `REVIEWER_USERNAME` env var, stderr는 마지막 20줄.

### Phase 4 — Decision Tree Mapping

```
review_runner 실패 알림
├─ ① stderr 확보 — claude subprocess stderr=PIPE 캡처 + 로그 재출력
│     (코멘트 재료 + docker logs 가시성 둘 다, 지난 rc=1 미가시 문제도 해결)
├─ ② 실패 단계 식별 — main()의 단계별 except가 stage 라벨 보유
│     stage ∈ { clone, claude 실행, 결과 비어있음 }
├─ ③ 추정 원인 휴리스틱
│     claude rc!=0 → "호스트 claude 세션 만료 의심, claude login 재실행"
│     clone 실패    → "GITLAB_TOKEN/네트워크 확인"
│     결과 비어있음 → "claude 출력 없음, 타임아웃/프롬프트 확인"
├─ ④ 코멘트 전송 — 기존 post_comment() 재사용, 본문만 실패 템플릿
│     멘션: os.environ.get("REVIEWER_USERNAME", "max")
│     전송 실패 시 → 로그만 (5번 사각지대, 허용된 손실)
└─ ⑤ 종료 코드 — 코멘트 발송해도 rc는 정직하게 non-zero 유지
```

## Idea Organization and Prioritization

**Thematic Organization:**

- **Theme A — 기존 인프라 재활용 (채택):** GitLab 알림판화(#3), @멘션 코멘트(#4), To-Do(#5), 라벨(#6), status(#7), 이슈(#8)
- **Theme B — 외부 감시 계층:** 컨테이너 다운 노티(#1), 로그 watcher 사이드카(#2), 외부 헬스체크(#11)
- **Theme C — 견고성/fallback:** 2차 채널(#10), 사각지대 = 허용된 손실(#12)
- **Cross-cutting:** 실패 원인 포함(#9) — 어느 채널을 쓰든 메시지 품질을 결정

**Prioritization Results:**

- **Top Priority (MVP):** #4 @멘션 코멘트 + #9 실패 원인 포함 — 추가 인프라 0, 커버율 높음
- **Quick Win:** #9의 stderr 캡처는 지난 rc=1 디버깅 난점도 함께 해결
- **Deferred (사후 재검토):** #10 fallback 채널(5번 사각지대), #11 외부 헬스체크(1번 사각지대)

**Action Planning — MVP 구현 (review_runner.py 단일 파일, ~40~60줄):**

1. `build_failure_comment(stage, reason, stderr_tail)` 추가 — `⚠️ **AI 자동 코드 리뷰 실패**` + `@{reviewer}` 멘션 + 단계/원인 + `<details>` stderr 20줄.
2. `run_claude_review()` 수정 — `stderr=subprocess.PIPE` 캡처 + 받은 stderr를 logger로 재출력.
3. `main()` 수정 — 단계별 except에서 stage 라벨 잡아 `post_comment()`로 실패 코멘트 전송 후 non-zero exit.
4. env — `REVIEWER_USERNAME`은 동일 컨테이너 env라 review_runner가 `os.environ.get`으로 읽으면 끝. `.env.example` / README에 한 줄 추가.
5. 스모크 테스트 — 깨진 토큰/잘못된 브랜치로 review_runner 직접 실행 → MR 실패 코멘트 + 메일 수신 확인.

**Resources Needed:** 없음 (기존 GITLAB_TOKEN, GitLab 메일 연동 활용)
**Timeline:** 단일 세션 구현 가능
**Success Indicators:** 의도적으로 실패시킨 review_runner가 해당 MR에 원인 포함 코멘트를 달고, GitLab이 max에게 메일을 발송

## Session Summary and Insights

**Key Achievements:**

- 알림 채널 12개 아이디어 발산 후 "기존 인프라 재활용" 테마로 수렴
- 형태 분석 격자로 @멘션 코멘트의 구조적 사각지대(1·2·5번)를 명시적으로 식별 → MVP 범위를 의도적으로 좁힘
- stderr 캡처가 알림 + 디버깅 두 문제를 동시 해결한다는 점 발견

**Session Reflections:**

- 새 채널을 추가하기보다 "이미 메일을 쏘는 GitLab"에 얹는 결정이 의존성과 운영 비용을 0으로 만든 핵심 통찰.
- 사각지대를 없애려 하지 않고 "허용된 손실"로 명시적으로 인정한 것이 MVP를 가볍게 유지.
- Deferred: fallback 채널(#10), 외부 헬스체크(#11)는 운영하며 5번/1번 실패 빈도가 확인되면 재검토.
