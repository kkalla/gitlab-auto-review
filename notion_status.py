"""Notion 현황 조회 — /task-status·/project-status 슬래시 커맨드 백엔드.

slack_bot.py의 두 핸들러가 사용한다. Notion REST API로 DB 전체를 페이지네이션
조회한 뒤 로컬에서 분류해 Slack mrkdwn 리포트 문자열을 만든다:

- /task-status    — Tasks DB 태스크 리포트. 현재 날짜 기준 지연 태스크는 최상단
                    🔴 지연 섹션에 프로젝트 무관하게 모으고(N일 지남 표시), 나머지는
                    프로젝트 티어(진행중 프로젝트 → 예정 → 정합성 이슈(종료 프로젝트
                    미완료) → 프로젝트 미연결 → 일정 없음)로 묶는다. 티어 안에선 기존
                    urgency 분류(차단/진행중/대기)를 정렬 순서와 아이템 이모지
                    (🚧🔵⏸️)로 유지한다.
- /project-status — Projects DB 프로젝트 현황(진행중/예정/종료) + 완료율.
                    완료율은 Completion rollup을 API로 읽지 않고 fetch_tasks
                    결과로 로컬 계산한다(관계 25개 초과 시 API rollup 부정확).

선택 기능: NOTION_TOKEN이 없으면 enabled()가 False고 커맨드는 안내만 답한다
(slack_notifier와 같은 원칙 — 미설정이 봇 부팅을 깨지 않는다). 대상 DB
(Tasks·Projects 둘 다)는 Notion 통합(integration)에 연결돼 있어야 하고,
Assignee/Owner 이름을 읽으려면 통합 capability에 "사용자 정보 읽기"가 필요하다.

의존성은 httpx뿐 — review_runner와 같은 이유로 notion SDK를 쓰지 않는다
(테스트 의존성 경량 유지).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Callable

import httpx

logger = logging.getLogger("notion_status")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
# 기본값: 제1연구센터 Tasks DB
NOTION_TASKS_DB_ID = os.environ.get(
    "NOTION_TASKS_DB_ID", "2e19f036b307806c9a2cf0f77de82190"
).strip()
# 기본값: 제1연구센터 Projects DB — Tasks의 Project relation이 가리키는 DB
NOTION_PROJECTS_DB_ID = os.environ.get(
    "NOTION_PROJECTS_DB_ID", "2e19f036b30780f1bd47cc2d6e3af1f9"
).strip()

_API = "https://api.notion.com/v1"
# 2022-06-28 고정 — databases/{id}/query가 기본 data source를 직접 질의한다.
# (2025-09 버전부터는 data_source id를 따로 받아야 해 설정이 한 단계 늘어난다.)
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 30.0

# Tasks DB Status의 complete 그룹 (Done/Drop). 이 밖의 값은 전부 미완료 취급.
DONE_STATUSES = frozenset({"Done", "Drop"})
# Projects DB Status: to_do=Pending/Not started, in_progress=In progress,
# complete=Done/Fail/Drop. 알 수 없는 새 상태값은 예정(to_do) 취급.
PROJECT_ACTIVE_STATUS = "In progress"
PROJECT_DONE_STATUSES = frozenset({"Done", "Fail", "Drop"})
# 태스크 "미시작" 상태 — 티어② (스케줄 확정·미시작) 판정에 사용.
TASK_NOT_STARTED_STATUSES = frozenset({"Pending", "Not started", ""})
MAX_ITEMS_PER_SECTION = 10
# per_page=100 → 최대 2000건. 이상 응답(진행 안 되는 cursor) 무한 루프 방어.
MAX_DB_PAGES = 20

# /task-status 인자 → 버킷 키. 매치되지 않는 인자는 담당자 이름 부분일치로 해석.
_FILTER_KEYWORDS = {
    "delayed": "delayed",
    "지연": "delayed",
    "blocked": "blocked",
    "차단": "blocked",
    "progress": "in_progress",
    "in_progress": "in_progress",
    "진행": "in_progress",
    "진행중": "in_progress",
    "todo": "todo",
    "pending": "todo",
    "대기": "todo",
    "done": "done",
    "완료": "done",
}

# urgency 버킷의 순회 순서(지연→차단→진행중→대기)이자 아이템 이모지 —
# 티어 내 정렬과 표시가 이 dict 하나를 공유한다(삽입 순서 의존, 의도적).
_BUCKET_EMOJI = {
    "delayed": "🔴",
    "blocked": "🚧",
    "in_progress": "🔵",
    "todo": "⏸️",
}

# /task-status 1차 그룹(프로젝트 티어) 섹션 순서·라벨.
_TIER_SECTIONS = (
    ("active", "🔵 진행중 프로젝트"),
    ("scheduled", "📅 예정 (스케줄 확정·미시작)"),
    ("integrity", "⚠️ 정합성 이슈 — 종료 프로젝트 미완료"),
    ("no_project", "🧩 프로젝트 미연결"),
    ("no_schedule", "🗓️ 일정 없음"),
)

# /project-status 상태 그룹 섹션 순서·라벨.
_PROJECT_SECTIONS = (
    ("in_progress", "🔵 진행중"),
    ("todo", "📅 예정"),
    ("done", "✅ 종료"),
)


def enabled() -> bool:
    """Notion 현황 조회 사용 가능 여부 (NOTION_TOKEN 존재)."""
    return bool(NOTION_TOKEN)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": _NOTION_VERSION,
    }


# ── Notion API I/O ──────────────────────────────────────────────────────────


def _query_db_all(db_id: str) -> list[dict]:
    """DB 전체 페이지 객체 목록 (100개 단위 페이지네이션, 아카이브 제외)."""
    results: list[dict] = []
    cursor: str | None = None
    for _ in range(MAX_DB_PAGES):
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = httpx.post(
            f"{_API}/databases/{db_id}/query",
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results") or [])
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return results
    logger.warning("DB %s 조회가 %d페이지 초과 — 이후 항목 절단", db_id, MAX_DB_PAGES)
    return results


def fetch_tasks() -> list[dict]:
    """Tasks DB 전체 페이지 객체 목록.

    Done까지 전부 가져온다 — Blocked by 해석에 blocker의 상태가 필요하고,
    완료 개수·완료율 계산에도 쓴다. 팀 태스크 DB 규모(수백 건)에선 몇 페이지면 끝난다.
    """
    return _query_db_all(NOTION_TASKS_DB_ID)


def fetch_projects() -> list[dict]:
    """Projects DB 전체 페이지 객체 목록 — 두 커맨드가 공유하는 프로젝트 메타의 원천.

    아카이브된 프로젝트는 쿼리에 안 나온다 — 그 id는 project_meta_resolver의
    페이지 GET 폴백이 커버한다.
    """
    return _query_db_all(NOTION_PROJECTS_DB_ID)


def _fetch_page_meta(page_id: str) -> dict:
    """페이지(프로젝트) 제목·상태 조회 — 벌크 맵에 없는 id(아카이브 등)의 GET 폴백."""
    resp = httpx.get(f"{_API}/pages/{page_id}", headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    props = resp.json().get("properties") or {}
    status = ((props.get("Status") or {}).get("status") or {}).get("name") or ""
    return {"name": _title_of(props), "status": status}


def project_meta_resolver(
    bulk: dict[str, dict] | None = None, allow_fallback: bool = True
) -> Callable[[str], dict]:
    """프로젝트 페이지 id → {"name", "status"} 메타. 두 커맨드가 공유하는 리졸버.

    벌크 맵(fetch_projects → parse_project 결과) 우선, 미스는 페이지 GET 폴백을
    실행 1회짜리 dict 캐시에 담는다. 실패는 빈 메타로 강등(메타 빠진 리포트가
    조회 실패보다 낫다) — 빈 status는 group_tiers에서 ②/④ 티어로 떨어진다.

    allow_fallback=False면 미스를 GET 없이 곧장 빈 메타로 강등 — 벌크 조회
    자체가 실패했을 때 프로젝트 수만큼 GET이 발사되는 걸 막는다(rate limit,
    십중팔구 같은 원인으로 다 실패).
    """
    # 캐시 값은 {"name","status"} 2키로 정규화 — 벌크(parse_project 8키)와
    # GET 폴백이 같은 형태를 갖게 해 소비자가 경로에 따라 갈리지 않는다.
    cache: dict[str, dict] = {
        pid: {"name": p.get("name") or "", "status": p.get("status") or ""}
        for pid, p in (bulk or {}).items()
    }

    def resolve(page_id: str) -> dict:
        if page_id not in cache:
            if not allow_fallback:
                cache[page_id] = {"name": "", "status": ""}
                return cache[page_id]
            try:
                cache[page_id] = _fetch_page_meta(page_id)
            except Exception:
                logger.exception("프로젝트 메타 조회 실패: %s", page_id)
                cache[page_id] = {"name": "", "status": ""}
        return cache[page_id]

    return resolve


# ── 순수 함수 (테스트 대상) ─────────────────────────────────────────────────


def _title_of(props: dict) -> str:
    """type이 title인 첫 속성의 텍스트 — 속성명("Name" 등)에 의존하지 않는다."""
    for prop in props.values():
        if (prop or {}).get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title") or [])
    return ""


def parse_task(page: dict) -> dict:
    """Notion 페이지 객체에서 리포트에 필요한 필드만 추출한다."""
    props = page.get("properties") or {}

    def _prop(name: str) -> dict:
        return props.get(name) or {}

    name = _title_of(props) or "(제목 없음)"
    plan = _prop("Schedule (Plan)").get("date") or {}
    return {
        "id": page.get("id", ""),
        "url": page.get("url", ""),
        "name": name,
        "status": (_prop("Status").get("status") or {}).get("name") or "",
        "priority": (_prop("Priority").get("select") or {}).get("name") or "",
        "assignees": [
            p.get("name") or "?" for p in _prop("Assignee").get("people") or []
        ],
        "plan_start": plan.get("start") or "",
        "plan_end": plan.get("end") or "",
        "blocked_by": [
            r.get("id", "") for r in _prop("Blocked by").get("relation") or []
        ],
        "project_ids": [
            r.get("id", "") for r in _prop("Project").get("relation") or []
        ],
    }


def parse_project(page: dict) -> dict:
    """Projects DB 페이지 객체에서 리포트·티어 판정에 필요한 필드만 추출한다."""
    props = page.get("properties") or {}

    def _prop(name: str) -> dict:
        return props.get(name) or {}

    name = _title_of(props) or "(제목 없음)"
    dates = _prop("Date").get("date") or {}
    return {
        "id": page.get("id", ""),
        "url": page.get("url", ""),
        "name": name,
        "status": (_prop("Status").get("status") or {}).get("name") or "",
        "owners": [p.get("name") or "?" for p in _prop("Owner").get("people") or []],
        "date_start": dates.get("start") or "",
        "date_end": dates.get("end") or "",
        "product": (_prop("Product").get("select") or {}).get("name") or "",
    }


def _overdue(task: dict, today: str) -> bool:
    """계획 종료일(없으면 시작일)이 오늘 이전인가. ISO 문자열은 사전순 비교로 충분."""
    end = (task["plan_end"] or task["plan_start"])[:10]
    return bool(end) and end < today


def _days_overdue(task: dict, today: str) -> int:
    """계획 종료일 기준 지난 일수. 계획일 없음/미래거나 파싱 실패면 0(표시 생략)."""
    due = (task["plan_end"] or task["plan_start"])[:10]
    if not due or due >= today:
        return 0
    try:
        return (date.fromisoformat(today) - date.fromisoformat(due)).days
    except ValueError:
        # Delayed 상태지만 계획일이 이상 문자열이면 일수 없이 넘긴다(🔴만 표시)
        return 0


def classify(tasks: list[dict], today: str) -> dict[str, list[dict]]:
    """태스크를 우선순위 순서(완료 → 지연 → 차단 → 진행중 → 대기)로 단일 버킷에 배정.

    - 지연: Status가 Delayed거나 계획 일정이 지났는데 미완료 (지연이 차단보다 우선 —
      둘 다 해당하면 더 급한 쪽으로 보인다)
    - 차단: Blocked by 중 미완료 blocker가 하나라도 있음
    """
    status_by_id = {t["id"]: t["status"] for t in tasks}
    buckets: dict[str, list[dict]] = {
        "delayed": [],
        "blocked": [],
        "in_progress": [],
        "todo": [],
        "done": [],
    }
    for t in tasks:
        if t["status"] in DONE_STATUSES:
            buckets["done"].append(t)
        elif t["status"] == "Delayed" or _overdue(t, today):
            buckets["delayed"].append(t)
        elif any(status_by_id.get(b, "") not in DONE_STATUSES for b in t["blocked_by"]):
            # ponytail: 조회 결과에 없는 blocker(아카이브 등)는 미완료로 간주 — 보수적
            buckets["blocked"].append(t)
        elif t["status"] == "In progress":
            buckets["in_progress"].append(t)
        else:  # Pending / Not started / 알 수 없는 새 상태값
            buckets["todo"].append(t)
    return buckets


def group_tiers(
    buckets: dict[str, list[dict]], project_status: Callable[[str], str]
) -> dict[str, list[tuple[str, dict]]]:
    """done 제외 태스크를 프로젝트 티어로 재편 — /task-status의 1차 그룹.

    urgency 순서(차단→진행중→대기)로 순회하며 배정하므로 티어 내 정렬이
    저절로 유지된다. 아이템은 (버킷 키, 태스크) 쌍 — 버킷 키는 이모지 표시용.
    지연(delayed) 버킷은 여기서 제외한다 — format_report가 최상단 🔴 지연
    섹션으로 프로젝트 무관하게 따로 모은다(중복 방지).

    티어 판정 (다중 프로젝트는 ① > ③):
      ① active     — 프로젝트 하나라도 In progress
      ③ integrity  — (①이 아니고) 종료 프로젝트(Done/Fail/Drop)의 미완료 태스크
      ② scheduled  — 미시작 태스크(Pending/Not started/빈 상태) + Schedule (Plan) 있음
      ④ no_project — 위 어디에도 안 걸리고 Project relation이 아예 없음(연결 필요)
      ⑤ no_schedule— 그 외 (프로젝트는 있으나 진행중/종료도 아니고 예정도 아님; 주로 일정 미기입)

    메타 조회 실패로 status가 비면 ①/③ 판정 불가 → 그 아래 티어로 강등(의도된 동작).
    """
    tiers: dict[str, list[tuple[str, dict]]] = {
        "active": [],
        "scheduled": [],
        "integrity": [],
        "no_project": [],
        "no_schedule": [],
    }
    for key in _BUCKET_EMOJI:
        if key == "delayed":
            continue  # 지연은 최상단 전용 섹션으로 분리 — 티어에 넣지 않는다
        for t in buckets[key]:
            statuses = [project_status(pid) for pid in t["project_ids"]]
            if PROJECT_ACTIVE_STATUS in statuses:
                tier = "active"
            elif any(s in PROJECT_DONE_STATUSES for s in statuses):
                tier = "integrity"
            elif t["status"] in TASK_NOT_STARTED_STATUSES and (
                t["plan_start"] or t["plan_end"]
            ):
                tier = "scheduled"
            elif not t["project_ids"]:
                # 프로젝트 미연결 — 티어 판정 자체가 불가한 더 근본 이슈라 먼저 가른다
                tier = "no_project"
            else:
                tier = "no_schedule"
            tiers[tier].append((key, t))
    return tiers


def project_task_counts(tasks: list[dict]) -> dict[str, tuple[int, int]]:
    """프로젝트 id → (완료, 전체) 태스크 수 — /project-status 완료율의 원천.

    Projects DB의 Completion rollup을 API로 읽지 않는다 — 관계가 25개를 넘으면
    API rollup이 부정확해서, fetch_tasks 결과로 로컬 집계한다.
    """
    counts: dict[str, list[int]] = {}
    for t in tasks:
        done = t["status"] in DONE_STATUSES
        for pid in t["project_ids"]:
            c = counts.setdefault(pid, [0, 0])
            c[0] += int(done)
            c[1] += 1
    return {pid: (c[0], c[1]) for pid, c in counts.items()}


def apply_filter(
    buckets: dict[str, list[dict]], filter_text: str
) -> tuple[dict[str, list[dict]], str]:
    """필터 적용 → (버킷, 헤더에 붙일 필터 설명). 빈 필터면 그대로.

    인자가 버킷 키워드(delayed/차단/…)면 그 버킷만, 아니면 담당자 이름
    부분일치(대소문자 무시)로 전 버킷을 거른다.
    """
    q = (filter_text or "").strip()
    if not q:
        return buckets, ""
    key = _FILTER_KEYWORDS.get(q.lower())
    if key:
        return {k: (v if k == key else []) for k, v in buckets.items()}, f"필터: {q}"
    needle = q.lower().lstrip("@")
    filtered = {
        k: [t for t in v if any(needle in a.lower() for a in t["assignees"])]
        for k, v in buckets.items()
    }
    return filtered, f"담당자 필터: {q}"


def _format_item(
    task: dict,
    project_title: Callable[[str], str],
    bullet: str = "•",
    overdue_days: int = 0,
) -> str:
    parts = [f"{bullet} <{task['url']}|{task['name']}>"]
    meta = []
    projects = " · ".join(
        filter(None, (project_title(pid) for pid in task["project_ids"]))
    )
    if projects:
        meta.append(projects)
    if overdue_days:  # 지연 섹션에서만 — 얼마나 지났는지
        meta.append(f"{overdue_days}일 지남")
    if task["assignees"]:
        meta.append(", ".join(task["assignees"]))
    due = (task["plan_end"] or task["plan_start"])[:10]
    if due:
        meta.append(f"~{due[5:]}")  # MM-DD
    if task["priority"] == "High":
        meta.append("🔺High")
    if meta:
        parts.append(" — " + " · ".join(meta))
    return "".join(parts)


def format_report(
    buckets: dict[str, list[dict]],
    tiers: dict[str, list[tuple[str, dict]]],
    today: str,
    project_title: Callable[[str], str],
    note: str = "",
    show_done: bool = False,
) -> str:
    """버킷+티어 → /task-status Slack mrkdwn 리포트. 섹션당 MAX_ITEMS_PER_SECTION개 + '외 N건'.

    요약 라인은 기존 urgency 버킷 개수를 유지하고, 1차 그룹(섹션)은 프로젝트
    티어(진행중 프로젝트/예정/정합성 이슈/프로젝트 미연결/일정 없음)다 — 아이템 앞 이모지(🔴🚧🔵⏸️)가
    기존 분류를 노출한다. 완료는 평소 개수만 요약에 노출하고, done 키워드 필터일
    때(show_done)만 목록을 편다 — 오래된 완료 태스크로 리포트가 길어지는 걸 막는다.
    """
    counts = {k: len(v) for k, v in buckets.items()}
    total = sum(counts.values())
    header = f"*📊 프로젝트 태스크 현황* ({today})"
    if note:
        header += f" — {note}"
    lines = [
        header,
        f"전체 {total} · 지연 {counts['delayed']} · 차단 {counts['blocked']} · "
        f"진행중 {counts['in_progress']} · 대기 {counts['todo']} · 완료 {counts['done']}",
    ]
    # 최상단 전용 지연 섹션 — 프로젝트 티어와 무관하게 지연 태스크를 먼저 모은다.
    # 항목마다 '며칠 지났는지'를 붙이므로 티어 섹션과 렌더를 분리한다.
    delayed = buckets["delayed"]
    if delayed:
        lines.append(f"\n*🔴 지연 ({len(delayed)})*")
        lines.extend(
            _format_item(t, project_title, "🔴", overdue_days=_days_overdue(t, today))
            for t in delayed[:MAX_ITEMS_PER_SECTION]
        )
        if len(delayed) > MAX_ITEMS_PER_SECTION:
            lines.append(f"… 외 {len(delayed) - MAX_ITEMS_PER_SECTION}건")

    sections = [(label, tiers[key]) for key, label in _TIER_SECTIONS]
    if show_done:
        sections.append(("✅ 완료", [("done", t) for t in buckets["done"]]))
    for label, items in sections:
        if not items:
            continue
        lines.append(f"\n*{label} ({len(items)})*")
        lines.extend(
            _format_item(t, project_title, _BUCKET_EMOJI.get(bucket, "•"))
            for bucket, t in items[:MAX_ITEMS_PER_SECTION]
        )
        if len(items) > MAX_ITEMS_PER_SECTION:
            lines.append(f"… 외 {len(items) - MAX_ITEMS_PER_SECTION}건")
    if total == 0:
        lines.append("\n조건에 맞는 태스크가 없어요.")
    return "\n".join(lines)


def _project_group(status: str) -> str:
    """프로젝트 상태 → 리포트 그룹 키. 알 수 없는 새 상태값·빈 값은 예정 취급(보수적)."""
    if status == PROJECT_ACTIVE_STATUS:
        return "in_progress"
    if status in PROJECT_DONE_STATUSES:
        return "done"
    return "todo"  # Pending / Not started / 빈 값 / 알 수 없는 새 상태값


def _format_project_item(project: dict, task_counts: dict[str, tuple[int, int]]) -> str:
    parts = [f"• <{project['url']}|{project['name']}>"]
    meta = []
    if project["product"]:
        meta.append(project["product"])
    if project["owners"]:
        meta.append(", ".join(project["owners"]))
    start, end = project["date_start"][:10], project["date_end"][:10]
    if start or end:
        meta.append(f"{start[5:]}~{end[5:]}")  # MM-DD~MM-DD (한쪽 없으면 그쪽만 생략)
    done, total = task_counts.get(project["id"], (0, 0))
    if total:  # 태스크 0개 프로젝트는 완료율 생략
        meta.append(f"{round(done / total * 100)}%")
        meta.append(f"완료 {done}/{total}")
    if meta:
        parts.append(" — " + " · ".join(meta))
    return "".join(parts)


def format_projects_report(
    projects: list[dict],
    today: str,
    task_counts: dict[str, tuple[int, int]],
) -> str:
    """프로젝트 목록 → /project-status Slack mrkdwn 리포트 (진행중/예정/종료 그룹).

    섹션당 MAX_ITEMS_PER_SECTION개 + '외 N건'. 완료율은 task_counts
    (project_task_counts 결과)에서 읽는다. 정합성 이슈(종료 프로젝트의 미완료
    태스크)는 여기 표시하지 않는다 — 그건 /task-status 티어 ③의 몫.
    """
    groups: dict[str, list[dict]] = {"in_progress": [], "todo": [], "done": []}
    for p in projects:
        groups[_project_group(p["status"])].append(p)
    lines = [
        f"*📁 프로젝트 현황* ({today})",
        f"전체 {len(projects)} · 진행중 {len(groups['in_progress'])} · "
        f"예정 {len(groups['todo'])} · 종료 {len(groups['done'])}",
    ]
    for key, label in _PROJECT_SECTIONS:
        items = groups[key]
        if not items:
            continue
        lines.append(f"\n*{label} ({len(items)})*")
        lines.extend(
            _format_project_item(p, task_counts) for p in items[:MAX_ITEMS_PER_SECTION]
        )
        if len(items) > MAX_ITEMS_PER_SECTION:
            lines.append(f"… 외 {len(items) - MAX_ITEMS_PER_SECTION}건")
    if not projects:
        lines.append("\n프로젝트가 없어요.")
    return "\n".join(lines)


# ── 진입점 ──────────────────────────────────────────────────────────────────


def build_report(filter_text: str = "") -> str:
    """Notion 조회 + 분류 + 티어 재편 + 포맷. slack_bot의 /task-status 스레드에서 호출."""
    today = date.today().isoformat()
    tasks = [parse_task(p) for p in fetch_tasks()]
    buckets = classify(tasks, today)
    buckets, note = apply_filter(buckets, filter_text)
    show_done = _FILTER_KEYWORDS.get((filter_text or "").strip().lower()) == "done"
    try:
        bulk = {p["id"]: p for p in map(parse_project, fetch_projects())}
        meta = project_meta_resolver(bulk)
    except Exception:
        # 벌크 실패 시엔 페이지 GET 폴백도 봉인 — 전 프로젝트 GET 폭주를 막는다.
        # 티어는 ②/④로 강등되지만 리포트는 렌더된다.
        logger.exception("Projects DB 벌크 조회 실패 — 프로젝트 메타 없이 진행")
        meta = project_meta_resolver({}, allow_fallback=False)
    tiers = group_tiers(buckets, lambda pid: meta(pid)["status"])
    return format_report(
        buckets, tiers, today, lambda pid: meta(pid)["name"], note, show_done
    )


def build_projects_report() -> str:
    """Notion Projects DB 조회 + 그룹 포맷. slack_bot의 /project-status 스레드에서 호출."""
    today = date.today().isoformat()
    projects = [parse_project(p) for p in fetch_projects()]
    try:
        task_counts = project_task_counts([parse_task(p) for p in fetch_tasks()])
    except Exception:
        # 완료율은 장식 — Tasks 조회 실패가 프로젝트 리포트를 깨선 안 된다.
        logger.exception("완료율용 Tasks DB 조회 실패 — 완료율 없이 진행")
        task_counts = {}
    return format_projects_report(projects, today, task_counts)
