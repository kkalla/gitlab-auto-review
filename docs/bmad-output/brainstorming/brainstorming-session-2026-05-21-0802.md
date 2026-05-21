---
stepsCompleted: [1, 2, 3, 4]
inputDocuments: []
session_topic: 'AI 리뷰가 기존 MR 코멘트/리뷰 히스토리를 참고하도록 context에 포함하는 방안'
session_goals: 'glab으로 기존 MR 코멘트를 가져와 리뷰 프롬프트 context에 주입하는 구현 후보까지 좁히기'
selected_approach: 'ai-recommended'
techniques_used: ['Question Storming', 'Morphological Analysis', 'Decision Tree Mapping']
techniques_used: []
ideas_generated: 3
context_file: ''
session_active: false
workflow_completed: true
---

# Brainstorming Session Results

**Facilitator:** Max
**Date:** 2026-05-21

## Session Overview

**Topic:** 현재 AI 리뷰는 MR source ↔ target branch diff만 context에 넣어, 사용자가 이전에 같은 MR에 남긴 코멘트/리뷰 히스토리를 모름. 이로 인해 이미 지적된 사항 중복 지적, 사용자 코멘트와 맥락 불일치 리뷰가 발생. AI 리뷰가 기존 MR 코멘트를 참고하도록 만드는 방안 발굴.
**Goals:** glab으로 기존 MR 코멘트를 가져와 리뷰 프롬프트 context에 주입하는 흐름 설계 및 구현 후보까지 좁히기

### Session Setup

clone 기반 리뷰 구조에서 `claude`는 git diff만 본다. MR 메타데이터(title/description)는 가져오지만 notes(코멘트/리뷰)는 가져오지 않음. glab API(`/merge_requests/:iid/notes` 또는 `glab` CLI)로 기존 코멘트를 수집해 프롬프트 context에 포함하는 것이 목표 방향.

## Technique Selection

**Approach:** AI-Recommended Techniques

**Recommended Techniques:**

- **Question Storming:** 코멘트 수집의 숨은 난제(어떤 코멘트, 어디까지, 중복 처리)를 질문으로 먼저 정의
- **Morphological Analysis:** `코멘트 종류 × 수집 방법 × 주입 위치 × 필터링` 격자로 조합 체계 탐색
- **Decision Tree Mapping:** 추린 후보를 구현 경로 트리화 → MVP 후보 확정

**AI Rationale:** 수렴형 목표(구현 후보 좁히기)에 맞춰 질문 정의 → 격자 매핑 → 실행 트리의 progressive 흐름 구성.

## Technique Execution Results

### Phase 1 — Question Storming

**핵심 질문:** "어떤 코멘트가 가치 있나?" / "재리뷰가 돌 때 이상적인 결과물은?"

**발견 (증상 vs 뿌리):**
원래 문제 "이전 코멘트 안 보고 리뷰"는 *증상*. 진짜 뿌리는 webhook filter가 `update` 액션을 받으므로 **MR에 커밋이 push될 때마다 자동리뷰가 재실행되는데, 매번 전체 diff를 처음 보듯 다시 리뷰**한다는 것.

**작업 흐름 (전부 AI):**
```
AI 리뷰 #1 → (사람) 코멘트 반영 커밋 push → AI 증분 리뷰 #2 → ...
```

**Phase 1 결정 사항:**
- AI 자신이 단 코멘트도 context에 포함, resolved 스레드는 제외.
- 재리뷰 방식은 **(b) 증분 리뷰** — 지난 리뷰 이후 올라온 커밋만 본다.
- 증분 + 지난 AI/사용자 코멘트 둘 다 context. 증분 diff가 "뭘 볼지", 코멘트가 "어떻게 해석할지" 담당.

**남은 미해결 질문 (Phase 2 격자 축이 됨):**
- 증분 기준점(last-reviewed commit SHA)을 어디에 저장? (서비스는 stateless)
- 첫 리뷰(open)는 증분 기준이 없음 → 전체 diff fallback.
- 코멘트를 glab으로 어떻게 수집? 어떤 종류만? 프롬프트 어디에 주입?

### Phase 2 — Morphological Analysis

증분 리뷰 구현을 위한 4개 파라미터 축을 각각 확정:

**축 A — last-reviewed SHA 저장 위치**
- 확정: **A1 (MR 코멘트 본문에 SHA 심기)** primary + **A4 (`oldrev`)** fallback.
- 근거: `oldrev`는 "직전 push 기준"이라 dedup(`_IN_FLIGHT_MRS`)·리뷰 실패 시 구간 누락. A1은 "마지막 성공 리뷰 기준"이라 정확. 어차피 코멘트를 읽어오므로 SHA 한 줄 심는 비용 ≈ 0.

**축 B — 코멘트 수집 방법**
- 확정: **B1a (기존 httpx 클라이언트 재사용)** + **B2b (`GET .../discussions` 엔드포인트)**.
- 근거: review_runner가 이미 httpx로 같은 API에 GET/POST 중. discussions는 스레드 단위라 resolved 판별·코드 라인 `position` 확보가 깔끔. `glab` CLI 바이너리는 불필요.

**축 C — context 주입 위치/방식**
- 확정: **C1a (증분 diff 범위 교체)** + **C2a (코멘트 본문 직접 삽입, untrusted 블록)**.
- C1a: `diff_hint`를 `git diff <reviewed_sha>..source`로 교체. 첫 리뷰는 기존 `target...source`.
- C2a: 코멘트를 `<untrusted-comments>` 블록으로 감싸 prompt injection 면역. C2b(파일+Read)는 claude가 Read 건너뛸 리스크, C2c(요약)는 YAGNI.

**축 D — 코멘트 필터링 전략**
- 확정: 시스템 노트 제외 + resolved 스레드 제외 + **D1a (최신 AI 리뷰 1개 전문)** + **D2a (outdated 위치 코멘트 포함)** + 사용자 코멘트 전부 포함.
- D1a 근거: 증분 리뷰는 직전 리뷰만으로 충분. D2a 근거: outdated여도 "AI 지적 ↔ 사용자 반박" 스레드 판단은 유효, resolved면 어차피 걸러짐.

**Phase 2 결론:** 증분 리뷰 = `git diff <last-reviewed-SHA>..source` + discussions에서 끌어온 미해결 코멘트(최신 AI 리뷰 1개 + 사용자 코멘트)를 untrusted 블록으로 프롬프트에 주입. SHA는 AI 리뷰 코멘트 본문에 심어 다음 회차에 회수.

### Phase 3 — Decision Tree Mapping

증분 리뷰 실행 흐름을 `review_runner.py` 트리로 확정:

```
증분 리뷰 (review_runner.py)
├─ ① discussions GET — GET /merge_requests/:iid/discussions (httpx)
│     └─ 한 번의 GET으로 'SHA 회수' + '코멘트 수집' 둘 다 처리
├─ ② reviewed_sha 회수 (축 A)
│     ├─ 마커 보유 코멘트에서 SHA 파싱 → 있으면 사용
│     ├─ 없으면 → payload object_attributes.oldrev fallback
│     └─ 그것도 없으면 → 첫 리뷰, 전체 diff 모드
├─ ③ 코멘트 필터링 (축 D) — 같은 discussions 응답에서
│     ├─ system:true 제외, resolved 스레드 제외
│     ├─ 마커 보유 최신 코멘트 1개 = 직전 AI 리뷰 (D1a)
│     └─ 마커 없는 코멘트 전부 = 사용자 코멘트 (outdated 포함, D2a)
├─ ④ 클론 + 증분 범위 결정 (축 C)
│     ├─ reviewed_sha 있음 → diff_hint = `git diff <sha>..source`
│     ├─ 없음 → 기존 `target...source`
│     └─ reviewed_sha가 shallow clone(depth 100)에 없으면 → 전체 diff fallback
├─ ⑤ 프롬프트 빌드 — diff_hint 교체 + <untrusted-comments> 블록 삽입
└─ ⑥ 리뷰 코멘트 POST — 본문 끝에 마커 심기 (현재 source HEAD SHA)
```

**결정점 확정:**
- **Q1 (현재 HEAD SHA 출처):** 클론 후 `git rev-parse HEAD`. payload `last_commit.id` 비신뢰.
- **Q2 (서비스 AI 리뷰 식별):** `post_comment()`는 `/review-pr` 출력을 verbatim 게시 → 서비스 전용 본문 헤더 없음. 사용자가 수동으로 붙여넣은 AI 리뷰와 본문만으론 구분 불가. → **축 A의 HTML 주석 마커가 곧 서비스 지문.** 마커 보유 = 우리 서비스 리뷰, 마커 없음 = 사용자/외부 코멘트. 마커 하나가 SHA 회수 + 서비스 식별 둘 다 담당.
- **Q3 (reviewed_sha가 clone에 없을 때):** 전체 diff fallback (드문 케이스, deepen은 오버).

**마커 형식:** `<!-- ai-auto-review reviewed-sha: <40-hex> -->` — HTML 주석이라 사람 눈에 안 보이고 충돌 없음.

## Idea Organization and Prioritization

**Thematic Organization:**

- **테마 A — 증분 리뷰 (본체):** 매 push마다 전체 diff를 새로 리뷰하던 걸 `git diff <reviewed_sha>..source` 증분으로 전환. 중복 지적의 근본 해결.
- **테마 B — 코멘트 context 주입 (원래 요청):** discussions에서 미해결 코멘트를 끌어와 `<untrusted-comments>` 블록으로 프롬프트 삽입. AI가 "이미 지적함 / 사용자가 반박함"을 인지.
- **테마 C — 상태 관리 (stateless 제약 우회):** HTML 주석 마커 `<!-- ai-auto-review reviewed-sha: X -->` 하나가 ① 증분 기준점 저장 ② 우리 서비스 리뷰 식별 두 역할 겸함.

**Prioritization Results:**

- **Top Priority (MVP):** 테마 A + B + C 전부. 셋이 한 덩어리 — 마커 없으면 증분도 서비스 식별도 불가.
- **Quick Win:** 마커 = SHA 저장 + 서비스 식별 겸용. 설계 한 방으로 두 문제 동시 해결.
- **Deferred:** A4 `oldrev` fallback 정교화, shallow clone deepen, 코멘트 요약(C2c) — 첫 구현은 단순 fallback으로 충분.

**Action Planning — MVP 구현 (`review_runner.py` 단일 파일):**

1. `fetch_discussions(project_id, mr_iid)` 추가 — `GET .../discussions`, `_http_get_json` 재사용.
2. `extract_reviewed_sha(discussions)` — 마커 보유 최신 코멘트에서 정규식으로 SHA 파싱. 없으면 `oldrev` → `None`(첫 리뷰).
3. `collect_prior_comments(discussions)` — system·resolved 제외, 마커 보유 최신 1개(직전 AI 리뷰) + 마커 없는 코멘트 전부 수집.
4. `run_claude_review()` 수정 — `reviewed_sha` 있으면 `diff_hint`를 `git diff <sha>..source`로, 없으면 기존대로. `<untrusted-comments>` 블록을 프롬프트에 추가.
5. `build_review_comment()` — `/review-pr` 출력 끝에 `<!-- ai-auto-review reviewed-sha: <rev-parse HEAD> -->` 마커 append 후 `post_comment()`.
6. 엣지 — reviewed_sha가 clone에 없으면(`git cat-file -e` 체크 실패) 전체 diff fallback. 첫 리뷰(open)는 마커 없음 → 자연히 전체.
7. 스모크 테스트 — MR에 리뷰 1회 → 커밋 push → 2회차가 증분 범위 + 직전 코멘트 참고 + 마커 갱신 확인.

**Resources Needed:** 없음 (기존 GITLAB_TOKEN, httpx 재사용)
**Timeline:** 단일 세션 구현 가능
**Success Indicators:** push 후 재리뷰가 증분 범위만 보고, 이미 지적된 사항을 중복으로 달지 않으며, 마커 SHA가 매 회차 갱신됨

## Session Summary and Insights

**Key Achievements:**

- "이전 코멘트 미참고"라는 *증상*에서, webhook `update`가 매 push마다 전체 diff를 재리뷰하는 *뿌리*를 발견 → 문제를 증분 리뷰로 재정의.
- 4개 파라미터 축(저장/수집/주입/필터링)을 격자로 하나씩 확정.
- HTML 주석 마커 하나가 "증분 기준점 저장"과 "서비스 리뷰 식별" 두 난제를 동시에 푼다는 설계 통찰.

**Session Reflections:**

- `post_comment()`가 verbatim 게시라 서비스 전용 본문 헤더가 없다는 사실 → 사용자가 수동으로 붙여넣은 AI 리뷰와 구분 불가 → 마커가 유일한 지문이라는 결론으로 이어짐.
- stateless 서비스에서 "GitLab을 상태판으로" 쓰는 지난 세션의 패턴(@멘션 알림)을 그대로 계승 — 코멘트에 마커를 심어 상태 저장.
- Deferred 항목(oldrev 정교화, deepen, 요약)은 운영하며 실제 빈도가 확인되면 재검토.
