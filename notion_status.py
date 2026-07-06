"""Notion Tasks DB 현황 조회 — /project-status 슬래시 커맨드 백엔드.

slack_bot.py의 /project-status 핸들러가 사용한다. Notion REST API로 Tasks DB
전체를 페이지네이션 조회한 뒤 로컬에서 분류(지연/차단/진행중/대기/완료)해
Slack mrkdwn 리포트 문자열을 만든다.

선택 기능: NOTION_TOKEN이 없으면 enabled()가 False고 커맨드는 안내만 답한다
(slack_notifier와 같은 원칙 — 미설정이 봇 부팅을 깨지 않는다). 대상 DB는
Notion 통합(integration)에 연결돼 있어야 하고, Assignee 이름을 읽으려면
통합 capability에 "사용자 정보 읽기"가 필요하다.

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

_API = "https://api.notion.com/v1"
# 2022-06-28 고정 — databases/{id}/query가 기본 data source를 직접 질의한다.
# (2025-09 버전부터는 data_source id를 따로 받아야 해 설정이 한 단계 늘어난다.)
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 30.0

# Notion Status 속성의 complete 그룹 (Done/Drop). 이 밖의 값은 전부 미완료 취급.
DONE_STATUSES = frozenset({"Done", "Drop"})
MAX_ITEMS_PER_SECTION = 10

# /project-status 인자 → 버킷 키. 매치되지 않는 인자는 담당자 이름 부분일치로 해석.
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

_SECTIONS = (
    ("delayed", "🔴 지연"),
    ("blocked", "🚧 차단"),
    ("in_progress", "🔵 진행중"),
    ("todo", "⏸️ 대기"),
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


def fetch_tasks() -> list[dict]:
    """Tasks DB 전체 페이지 객체 목록 (100개 단위 페이지네이션, 아카이브 제외).

    Done까지 전부 가져온다 — Blocked by 해석에 blocker의 상태가 필요하고,
    완료 개수도 요약에 쓴다. 팀 태스크 DB 규모(수백 건)에선 몇 페이지면 끝난다.
    """
    results: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = httpx.post(
            f"{_API}/databases/{NOTION_TASKS_DB_ID}/query",
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


def _fetch_page_title(page_id: str) -> str:
    """페이지(프로젝트) 제목 조회 — type이 title인 첫 속성을 쓴다(속성명 무관)."""
    resp = httpx.get(f"{_API}/pages/{page_id}", headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    for prop in (resp.json().get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title") or [])
    return ""


def project_title_resolver() -> Callable[[str], str]:
    """프로젝트 페이지 id → 제목. 실행 1회짜리 dict 캐시 — 표시되는 태스크의
    고유 프로젝트 수(보통 한 자릿수)만큼만 API를 부른다. 실패는 빈 문자열로
    강등(제목 없는 리포트가 조회 실패보다 낫다)."""
    cache: dict[str, str] = {}

    def resolve(page_id: str) -> str:
        if page_id not in cache:
            try:
                cache[page_id] = _fetch_page_title(page_id)
            except Exception:
                logger.exception("프로젝트 제목 조회 실패: %s", page_id)
                cache[page_id] = ""
        return cache[page_id]

    return resolve


# ── 순수 함수 (테스트 대상) ─────────────────────────────────────────────────


def parse_task(page: dict) -> dict:
    """Notion 페이지 객체에서 리포트에 필요한 필드만 추출한다."""
    props = page.get("properties") or {}

    def _prop(name: str) -> dict:
        return props.get(name) or {}

    name = (
        "".join(t.get("plain_text", "") for t in _prop("Name").get("title") or [])
        or "(제목 없음)"
    )
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


def _overdue(task: dict, today: str) -> bool:
    """계획 종료일(없으면 시작일)이 오늘 이전인가. ISO 문자열은 사전순 비교로 충분."""
    end = (task["plan_end"] or task["plan_start"])[:10]
    return bool(end) and end < today


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


def _format_item(task: dict, project_title: Callable[[str], str]) -> str:
    parts = [f"• <{task['url']}|{task['name']}>"]
    meta = []
    projects = " · ".join(
        filter(None, (project_title(pid) for pid in task["project_ids"]))
    )
    if projects:
        meta.append(projects)
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
    today: str,
    project_title: Callable[[str], str],
    note: str = "",
    show_done: bool = False,
) -> str:
    """버킷 → Slack mrkdwn 리포트. 섹션당 MAX_ITEMS_PER_SECTION개 + '외 N건'.

    완료는 평소 개수만 요약에 노출하고, done 키워드 필터일 때(show_done)만
    목록을 편다 — 오래된 완료 태스크로 리포트가 길어지는 걸 막는다.
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
    sections = _SECTIONS + (("done", "✅ 완료"),) if show_done else _SECTIONS
    for key, label in sections:
        items = buckets[key]
        if not items:
            continue
        lines.append(f"\n*{label} ({len(items)})*")
        lines.extend(
            _format_item(t, project_title) for t in items[:MAX_ITEMS_PER_SECTION]
        )
        if len(items) > MAX_ITEMS_PER_SECTION:
            lines.append(f"… 외 {len(items) - MAX_ITEMS_PER_SECTION}건")
    if total == 0:
        lines.append("\n조건에 맞는 태스크가 없어요.")
    return "\n".join(lines)


# ── 진입점 ──────────────────────────────────────────────────────────────────


def build_report(filter_text: str = "") -> str:
    """Notion 조회 + 분류 + 포맷. slack_bot의 /project-status 스레드에서 호출."""
    today = date.today().isoformat()
    tasks = [parse_task(p) for p in fetch_tasks()]
    buckets = classify(tasks, today)
    buckets, note = apply_filter(buckets, filter_text)
    show_done = _FILTER_KEYWORDS.get((filter_text or "").strip().lower()) == "done"
    return format_report(buckets, today, project_title_resolver(), note, show_done)
