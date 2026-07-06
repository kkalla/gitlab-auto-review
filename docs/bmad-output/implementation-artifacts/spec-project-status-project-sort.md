---
title: '/task-status 신설(티어 정렬 태스크 리포트) + /project-status를 프로젝트 현황 조회로 재정의'
type: 'feature'
created: '2026-07-06'
status: 'done'
baseline_commit: '367e50e7e54a850dcda11ffa26574a5cf1686c21'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `/project-status`가 이름과 달리 태스크 리포트를 반환해 프로젝트 단위 현황을 볼 수단이 없고, 태스크 리포트는 프로젝트 상태(진행중/종료)를 반영하지 않아 진행중 프로젝트의 태스크가 위로 오지 않으며, 종료된 프로젝트에 미완료 태스크가 남는 정합성/동기화 이슈도 드러나지 않는다.

**Approach:** ① `/task-status` 신설 — 기존 태스크 리포트를 이관하고 1차 그룹을 프로젝트 티어로 재편: 진행중 프로젝트 태스크 → 미시작·스케줄 확정 태스크 → 종료 프로젝트의 미완료 태스크(⚠️ 정합성 이슈) → 기타. 기존 지연/차단/진행중/대기 분류는 티어 내 정렬 순서와 아이템 이모지(🔴🚧🔵⏸️)로 유지. ② `/project-status` 재정의 — Projects DB를 조회해 상태 그룹별(진행중/예정/종료) 프로젝트 목록을 답한다. 프로젝트 메타(제목·상태)는 Projects DB 벌크 쿼리 1회로 맵을 만들고, 맵에 없는 id(아카이브 등)만 페이지 GET 폴백.

## Boundaries & Constraints

**Always:**
- `NOTION_PROJECTS_DB_ID` env 신설(기본 `2e19f036b30780f1bd47cc2d6e3af1f9`), `.env.example` 반영.
- Projects DB Status 값: to_do=`Pending`/`Not started`, in_progress=`In progress`, complete=`Done`/`Fail`/`Drop`.
- 조회 실패는 빈 값 강등(리포트는 렌더, 예외 전파 금지) — 기존 title 리졸버 원칙 유지.
- `/task-status`는 기존 필터(버킷 키워드·담당자 부분일치)·`MAX_ITEMS_PER_SECTION` 캡·show_done·`public` 인자 동작 유지. `/project-status` 인자는 `[public]`만.
- 순수 함수(파싱·티어·포맷)는 네트워크 없이 테스트 — resolver/맵은 주입.
- 핸들러는 ack 즉시 + 별도 스레드에서 respond (기존 패턴).
- `/project-status` 아이템에 완료율 표시 — Projects DB `Completion` rollup을 API로 읽지 않고(관계 25개 초과 시 API rollup 부정확), `fetch_tasks` 결과에서 project_ids × `DONE_STATUSES`로 done/total 로컬 계산. 태스크 0개 프로젝트는 완료율 생략.
- 티어 ②의 "스케줄 잡힘" 판정은 태스크 `Schedule (Plan)` 기준 (체크포인트 1에서 확정).

**Ask First:**
- 없음 — 초안의 두 결정(티어② 기준, 완료율 표시)은 체크포인트 1에서 확정되어 Always로 이동.

**Never:**
- notion SDK 도입 금지(httpx만), Notion API 버전 변경 금지(2022-06-28).
- 기존 `/project-status`의 태스크 리포트 동작 하위호환 유지 금지 — 완전 대체.
- `/project-status`에 정합성 이슈 중복 표시 금지 — 그건 `/task-status` 티어 ③의 몫.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 티어① | project Status=`In progress` | `/task-status` 최상단 섹션 | N/A |
| 티어② | task Status∈{Pending, Not started, ""} + `Schedule (Plan)` 있음, 프로젝트 진행중/종료 아님 | 예정 섹션 | N/A |
| 티어③ | project Status∈{Done, Fail, Drop} + task 미완료 | ⚠️ 정합성 이슈 섹션 | N/A |
| 티어④ | 그 외(프로젝트 없음/미시작 + 스케줄 없음) | 기타 섹션 | N/A |
| 다중 프로젝트 | In progress 하나라도 → ①, 아니면 complete 있으면 → ③ | 우선순위 ①>③ | N/A |
| 티어 내 정렬 | 같은 티어에 지연+대기 혼재 | 지연→차단→진행중→대기 순 + 아이템 이모지 | N/A |
| 아카이브 프로젝트 | 벌크 맵에 없는 project_id | 페이지 GET 폴백으로 상태 해석(티어③ 판정 가능) | GET 실패 시 빈 메타 → ②/④ 강등 |
| 프로젝트 리포트 | `/project-status` | 요약 카운트 + 🔵 진행중/📅 예정/✅ 종료 섹션, 캡+"외 N건" | 조회 실패 시 ⚠️ 오류 respond |
| 프로젝트 아이템 | Owner·Date·Product·태스크 존재 | `• <url\|이름> — Product · Owner · MM-DD~MM-DD · 완료 n/N` (빈 값·태스크 0개 항목 생략) | N/A |
| NOTION_TOKEN 없음 | 두 커맨드 공통 | 안내만 ack (기존 동작) | N/A |

</frozen-after-approval>

## Code Map

- `notion_status.py` -- 주 변경. `_fetch_page_title`/`project_title_resolver`(109-134) → meta 리졸버(벌크 맵+GET 폴백), `classify`(178) 유지, `format_report`(250-285) 티어 재편, `build_report`(291) 유지 + `fetch_projects`/`parse_project`/`build_projects_report` 신설.
- `slack_bot.py:340-366` -- `/project-status` 핸들러 → `/task-status`로 이관(build_report), `/project-status`는 build_projects_report 호출로 교체. ack/스레드/public 패턴 재사용.
- `tests/test_notion_status.py` -- format 단언 갱신 + 티어·parse_project·프로젝트 리포트 테스트 추가.
- `SLACK_SETUP.md` §8, `CLAUDE.md`, `.env.example` -- 커맨드 2개 및 새 env 반영. `/task-status` Slack 앱 등록 필요 안내.

## Tasks & Acceptance

**Execution:**
- [x] `notion_status.py` -- `fetch_projects()`(fetch_tasks와 동일 페이지네이션, NOTION_PROJECTS_DB_ID) + `parse_project()`(id/url/name/status/owners/date/product) + 프로젝트 메타 리졸버(벌크 맵 우선, 미스 시 `GET /pages/{id}`로 title+Status 파싱, dict 캐시, 실패 빈 값) -- 두 커맨드가 공유하는 데이터 계층
- [x] `notion_status.py` -- `group_tiers(buckets, project_status)` 순수 함수: done 제외 태스크를 urgency 순서로 순회하며 active/scheduled/integrity/rest 배정 + `format_report` 재편(요약 라인 유지, 섹션 `🔵 진행중 프로젝트`/`📅 예정 (스케줄 확정·미시작)`/`⚠️ 정합성 이슈 — 종료 프로젝트 미완료`/`📦 기타`, 아이템 분류 이모지, 캡·빈 문구 유지) -- 사용자 요구 정렬
- [x] `notion_status.py` -- `build_projects_report()`: 상태 그룹별 섹션 + 요약 카운트, 캡 적용, `fetch_tasks` 결과로 프로젝트별 done/total 완료율 로컬 계산 -- `/project-status` 백엔드
- [x] `slack_bot.py` -- `/task-status` 핸들러 신설(기존 로직 이관), `/project-status` 핸들러를 프로젝트 리포트로 교체([public]만 파싱) -- 커맨드 분리
- [x] `tests/test_notion_status.py` -- I/O 매트릭스 케이스 커버(티어 판정·우선순위·폴백 강등·티어 내 정렬·parse_project·프로젝트 포맷) + 기존 단언 갱신 -- 회귀 방지
- [x] `SLACK_SETUP.md`/`CLAUDE.md`/`.env.example` -- 문서·env 동기화, Slack 앱에 `/task-status` 등록 안내 -- 배포 시 커맨드 미등록 사고 방지

**Acceptance Criteria:**
- Given 진행중/종료/미시작 프로젝트의 태스크가 섞인 입력, when `/task-status`, then 섹션 순서가 ①→②→③→④이고 기존 필터 인자가 동일하게 동작한다.
- Given Projects DB에 각 상태 그룹 프로젝트가 존재, when `/project-status`, then 진행중/예정/종료 섹션으로 목록이 나온다.
- Given 프로젝트 메타 조회 실패(벌크+GET 모두), when `/task-status`, then 예외 없이 해당 태스크가 ②/④로 강등된다.

## Spec Change Log

- 2026-07-06 (checkpoint 1, human edit): 단일 커맨드 정렬 개선 → 커맨드 분리로 재정의. `/task-status` 신설(태스크 리포트+티어 정렬 이관), `/project-status`는 Projects DB 현황 조회로 대체. 이에 따라 "벌크 쿼리·새 env 금지" 제약 폐기(프로젝트 리포트에 필요), 벌크 맵+GET 폴백 구조 채택.
- 2026-07-06 (checkpoint 1, approve): Ask First 두 건 확정 — 티어②는 태스크 `Schedule (Plan)` 기준, `/project-status`에 완료율 표시(Completion rollup 대신 fetch_tasks 로컬 계산 — API rollup의 25개 관계 상한 회피). KEEP: 벌크 맵+GET 폴백, 티어 내 urgency 정렬 구조.

## Verification

**Commands:**
- `make test` -- expected: 전체 pass (기존 + 신규 테스트)

**Manual checks (if no CLI):**
- Slack 앱에 `/task-status` 슬래시 커맨드 등록 후 두 커맨드 실제 응답 확인 (배포 환경에서만 가능).

## Suggested Review Order

**티어 재편 — /task-status의 새 1차 그룹**

- 진입점: 조회→분류→티어→포맷 배선과 벌크 실패 시 GET 폴백 봉인 판단
  [`notion_status.py:508`](../../../notion_status.py#L508)

- 티어 판정 코어 — urgency 순회로 티어 내 정렬을 공짜로 얻는 구조
  [`notion_status.py:302`](../../../notion_status.py#L302)

- 요약 라인은 기존 버킷, 섹션은 티어, 분류는 아이템 이모지로 이동
  [`notion_status.py:401`](../../../notion_status.py#L401)

**프로젝트 메타 데이터 계층 (두 커맨드 공유)**

- 벌크 맵 우선 + GET 폴백 + 2키 정규화 캐시 + allow_fallback 봉인
  [`notion_status.py:170`](../../../notion_status.py#L170)

- 페이지네이션 공유화 + MAX_DB_PAGES 무한 루프 방어
  [`notion_status.py:119`](../../../notion_status.py#L119)

**프로젝트 현황 리포트 — 재정의된 /project-status**

- 완료율을 rollup 대신 태스크 로컬 집계로 — 25개 관계 상한 회피
  [`notion_status.py:341`](../../../notion_status.py#L341)

- 상태 그룹(진행중/예정/종료) 섹션 포맷과 아이템 메타 생략 규칙
  [`notion_status.py:471`](../../../notion_status.py#L471)

**Slack 핸들러 분리**

- 공유 스레드 본체 — respond 자체 실패까지 포착(데몬 스레드 침묵사 방지)
  [`slack_bot.py:341`](../../../slack_bot.py#L341)

- 신설 /task-status(기존 로직 이관)와 재정의 /project-status(구 인자 이관 안내)
  [`slack_bot.py:364`](../../../slack_bot.py#L364)

**주변부**

- 티어 판정 테스트 군(①~④·다중 프로젝트·강등·정렬)
  [`test_notion_status.py:188`](../../../tests/test_notion_status.py#L188)

- 리졸버 폴백/봉인 경로 테스트 (monkeypatch, 네트워크 없음)
  [`test_notion_status.py:316`](../../../tests/test_notion_status.py#L316)

- 배포 체크리스트 — /task-status는 Slack 앱 신규 등록 필요
  [`SLACK_SETUP.md:171`](../../../SLACK_SETUP.md#L171)

- 신규 env 기본값
  [`.env.example:53`](../../../.env.example#L53)
