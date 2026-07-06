"""notion_status.py 순수 함수 테스트 — 파싱·분류·필터·포맷.

Notion API I/O(fetch_tasks 등)는 다루지 않는다. review_runner 테스트와 같은
원칙: 네트워크 없는 순수 로직만 검증한다.
"""

import notion_status as ns

TODAY = "2026-07-06"


def _task(**kw) -> dict:
    base = {
        "id": "t1",
        "url": "https://notion.so/t1",
        "name": "태스크",
        "status": "In progress",
        "priority": "",
        "assignees": [],
        "plan_start": "",
        "plan_end": "",
        "blocked_by": [],
        "project_ids": [],
    }
    base.update(kw)
    return base


def _no_title(_pid: str) -> str:
    return ""


# ── parse_task ──────────────────────────────────────────────────────────────


def test_parse_task_extracts_fields():
    page = {
        "id": "abc",
        "url": "https://notion.so/abc",
        "properties": {
            "Name": {"title": [{"plain_text": "모델 "}, {"plain_text": "학습"}]},
            "Status": {"status": {"name": "In progress"}},
            "Priority": {"select": {"name": "High"}},
            "Assignee": {"people": [{"name": "kkalla"}, {"id": "u2"}]},
            "Schedule (Plan)": {"date": {"start": "2026-07-01", "end": "2026-07-10"}},
            "Blocked by": {"relation": [{"id": "dep1"}]},
            "Project": {"relation": [{"id": "proj1"}]},
        },
    }
    t = ns.parse_task(page)
    assert t["name"] == "모델 학습"
    assert t["status"] == "In progress"
    assert t["priority"] == "High"
    assert t["assignees"] == ["kkalla", "?"]  # 이름 없는 user는 "?"
    assert t["plan_end"] == "2026-07-10"
    assert t["blocked_by"] == ["dep1"]
    assert t["project_ids"] == ["proj1"]


def test_parse_task_empty_page_is_safe():
    t = ns.parse_task({})
    assert t["name"] == "(제목 없음)"
    assert t["status"] == ""
    assert t["assignees"] == []


# ── classify ────────────────────────────────────────────────────────────────


def test_classify_done_and_drop_go_done():
    buckets = ns.classify([_task(status="Done"), _task(id="t2", status="Drop")], TODAY)
    assert len(buckets["done"]) == 2


def test_classify_delayed_status_and_overdue():
    tasks = [
        _task(id="d1", status="Delayed"),
        _task(id="d2", status="In progress", plan_end="2026-07-05"),
        _task(id="ok", status="In progress", plan_end="2026-07-06"),  # 오늘은 아직
    ]
    buckets = ns.classify(tasks, TODAY)
    assert {t["id"] for t in buckets["delayed"]} == {"d1", "d2"}
    assert {t["id"] for t in buckets["in_progress"]} == {"ok"}


def test_classify_overdue_datetime_string():
    # datetime이어도 [:10] 사전순 비교로 지연 판정
    buckets = ns.classify([_task(plan_end="2026-07-05T18:00:00+09:00")], TODAY)
    assert len(buckets["delayed"]) == 1


def test_classify_blocked_only_when_blocker_unfinished():
    tasks = [
        _task(id="blocker-open", status="In progress"),
        _task(id="blocker-done", status="Done"),
        _task(id="b1", status="Pending", blocked_by=["blocker-open"]),
        _task(id="b2", status="Pending", blocked_by=["blocker-done"]),
    ]
    buckets = ns.classify(tasks, TODAY)
    assert {t["id"] for t in buckets["blocked"]} == {"b1"}
    assert {t["id"] for t in buckets["todo"]} == {"b2"}


def test_classify_unknown_blocker_is_conservative():
    # 조회 결과에 없는 blocker(아카이브 등)는 미완료로 간주 → 차단
    buckets = ns.classify([_task(blocked_by=["ghost"])], TODAY)
    assert len(buckets["blocked"]) == 1


def test_classify_delayed_wins_over_blocked():
    buckets = ns.classify([_task(status="Delayed", blocked_by=["ghost"])], TODAY)
    assert len(buckets["delayed"]) == 1
    assert not buckets["blocked"]


# ── apply_filter ────────────────────────────────────────────────────────────


def _buckets():
    return ns.classify(
        [
            _task(id="d1", status="Delayed", assignees=["Kim Kkalla"]),
            _task(id="p1", status="In progress", assignees=["Lee"]),
            _task(id="done1", status="Done", assignees=["Kim Kkalla"]),
        ],
        TODAY,
    )


def test_apply_filter_empty_passthrough():
    buckets = _buckets()
    out, note = ns.apply_filter(buckets, "")
    assert out is buckets and note == ""


def test_apply_filter_keyword_keeps_single_bucket():
    out, note = ns.apply_filter(_buckets(), "지연")
    assert len(out["delayed"]) == 1
    assert not out["in_progress"] and not out["done"]
    assert "필터" in note


def test_apply_filter_assignee_substring_case_insensitive():
    out, note = ns.apply_filter(_buckets(), "@kkalla")
    assert {t["id"] for t in out["delayed"]} == {"d1"}
    assert not out["in_progress"]
    assert {t["id"] for t in out["done"]} == {"done1"}
    assert "담당자" in note


# ── format_report ───────────────────────────────────────────────────────────


def test_format_report_summary_and_sections():
    report = ns.format_report(_buckets(), TODAY, _no_title)
    assert "전체 3 · 지연 1 · 차단 0 · 진행중 1 · 대기 0 · 완료 1" in report
    assert "🔴 지연 (1)" in report
    assert "<https://notion.so/t1|태스크>" in report
    assert "✅ 완료" not in report  # 완료는 기본 개수만


def test_format_report_show_done_lists_items():
    report = ns.format_report(_buckets(), TODAY, _no_title, show_done=True)
    assert "✅ 완료 (1)" in report


def test_format_report_caps_section():
    many = [_task(id=f"t{i}", status="Delayed") for i in range(13)]
    report = ns.format_report(ns.classify(many, TODAY), TODAY, _no_title)
    assert "… 외 3건" in report


def test_format_report_item_meta():
    task = _task(
        assignees=["kkalla"],
        plan_end="2026-07-10",
        priority="High",
        project_ids=["p1"],
    )
    report = ns.format_report(
        ns.classify([task], TODAY), TODAY, lambda pid: "AI 프로젝트"
    )
    assert "AI 프로젝트 · kkalla · ~07-10 · 🔺High" in report


def test_format_report_empty():
    buckets = ns.classify([], TODAY)
    report = ns.format_report(buckets, TODAY, _no_title)
    assert "조건에 맞는 태스크가 없어요" in report
