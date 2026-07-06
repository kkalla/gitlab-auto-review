"""notion_status.py 순수 함수 테스트 — 파싱·분류·티어·필터·포맷.

Notion API I/O(fetch_tasks/fetch_projects 등)는 다루지 않는다. review_runner
테스트와 같은 원칙: 네트워크 없는 순수 로직만 검증한다. 메타 리졸버는
_fetch_page_meta를 monkeypatch해 폴백/강등 경로만 본다.
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


def _project(**kw) -> dict:
    base = {
        "id": "p1",
        "url": "https://notion.so/p1",
        "name": "프로젝트",
        "status": "In progress",
        "owners": [],
        "date_start": "",
        "date_end": "",
        "product": "",
    }
    base.update(kw)
    return base


def _no_title(_pid: str) -> str:
    return ""


def _tiers(tasks: list[dict], status_map: dict[str, str]) -> dict:
    buckets = ns.classify(tasks, TODAY)
    return ns.group_tiers(buckets, lambda pid: status_map.get(pid, ""))


def _report(tasks: list[dict], status_map: dict | None = None, **kw) -> str:
    buckets = ns.classify(tasks, TODAY)
    smap = status_map or {}
    tiers = ns.group_tiers(buckets, lambda pid: smap.get(pid, ""))
    return ns.format_report(buckets, tiers, TODAY, _no_title, **kw)


# ── parse_task ──────────────────────────────────────────────────────────────


def test_parse_task_extracts_fields():
    page = {
        "id": "abc",
        "url": "https://notion.so/abc",
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "모델 "}, {"plain_text": "학습"}],
            },
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


# ── parse_project ───────────────────────────────────────────────────────────


def test_parse_project_extracts_fields():
    page = {
        "id": "proj1",
        "url": "https://notion.so/proj1",
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "AI "}, {"plain_text": "리뷰어"}],
            },
            "Status": {"status": {"name": "In progress"}},
            "Owner": {"people": [{"name": "kkalla"}]},
            "Date": {"date": {"start": "2026-07-01", "end": "2026-09-30"}},
            "Product": {"select": {"name": "Vision"}},
        },
    }
    assert ns.parse_project(page) == {
        "id": "proj1",
        "url": "https://notion.so/proj1",
        "name": "AI 리뷰어",
        "status": "In progress",
        "owners": ["kkalla"],
        "date_start": "2026-07-01",
        "date_end": "2026-09-30",
        "product": "Vision",
    }


def test_parse_project_empty_page_is_safe():
    p = ns.parse_project({})
    assert p["name"] == "(제목 없음)"
    assert p["status"] == ""
    assert p["owners"] == []
    assert p["product"] == ""


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


# ── group_tiers ─────────────────────────────────────────────────────────────


def test_group_tiers_active_project():
    # 티어①: 프로젝트가 In progress면 최상단
    tiers = _tiers([_task(project_ids=["p1"])], {"p1": "In progress"})
    assert [t["id"] for _, t in tiers["active"]] == ["t1"]
    assert not tiers["integrity"] and not tiers["rest"]


def test_group_tiers_scheduled():
    # 티어②: 미시작(Pending/Not started/빈 상태) + Schedule (Plan) 있음,
    # 프로젝트는 진행중/종료가 아님(없거나 미시작)
    tasks = [
        _task(id="s1", status="Pending", plan_start="2026-08-01"),
        _task(id="s2", status="Not started", plan_end="2026-08-10", project_ids=["p1"]),
        _task(id="s3", status="", plan_start="2026-08-01"),
    ]
    tiers = _tiers(tasks, {"p1": "Not started"})
    assert {t["id"] for _, t in tiers["scheduled"]} == {"s1", "s2", "s3"}


def test_group_tiers_integrity():
    # 티어③: 종료 프로젝트(Done/Fail/Drop)의 미완료 태스크 — 정합성 이슈
    for status in ("Done", "Fail", "Drop"):
        tiers = _tiers([_task(project_ids=["p1"])], {"p1": status})
        assert len(tiers["integrity"]) == 1, status


def test_group_tiers_done_task_excluded():
    # 완료 태스크는 티어에 아예 안 들어간다 (종료 프로젝트라도 정합성 이슈 아님)
    tiers = _tiers([_task(status="Done", project_ids=["p1"])], {"p1": "Done"})
    assert not any(tiers.values())


def test_group_tiers_rest():
    # 티어④: 프로젝트 없음/미시작 + 스케줄 없음, 또는 미시작 아닌 태스크
    tasks = [
        _task(id="r1", status="Pending"),  # 프로젝트·스케줄 없음
        _task(
            id="r2", status="Pending", project_ids=["p1"]
        ),  # 미시작 프로젝트 + 스케줄 없음
        _task(
            id="r3", status="In progress", plan_start="2026-08-01"
        ),  # 미시작 아님 → ② 불가
    ]
    tiers = _tiers(tasks, {"p1": "Pending"})
    assert {t["id"] for _, t in tiers["rest"]} == {"r1", "r2", "r3"}


def test_group_tiers_multi_project_active_wins():
    # 다중 프로젝트: In progress 하나라도 있으면 ① (① > ③)
    tiers = _tiers(
        [_task(project_ids=["fin", "act"])], {"fin": "Done", "act": "In progress"}
    )
    assert len(tiers["active"]) == 1
    assert not tiers["integrity"]


def test_group_tiers_multi_project_done_without_active_is_integrity():
    # 다중 프로젝트: 진행중 없이 종료+미시작 혼재면 ③
    tiers = _tiers(
        [_task(project_ids=["fin", "wait"])], {"fin": "Done", "wait": "Pending"}
    )
    assert len(tiers["integrity"]) == 1
    assert not tiers["active"] and not tiers["rest"]


def test_group_tiers_unknown_meta_demotes():
    # 메타 조회 실패(빈 status) → ①/③ 판정 불가, ②/④ 강등
    tasks = [
        _task(
            id="u1", status="Pending", plan_start="2026-08-01", project_ids=["ghost"]
        ),
        _task(id="u2", project_ids=["ghost"]),
    ]
    tiers = _tiers(tasks, {})
    assert {t["id"] for _, t in tiers["scheduled"]} == {"u1"}
    assert {t["id"] for _, t in tiers["rest"]} == {"u2"}


def test_group_tiers_internal_order_is_urgency():
    # 같은 티어 안에선 지연→차단→진행중→대기 순, 버킷 키가 이모지 표시용으로 실림
    tasks = [
        _task(id="w", status="Pending", project_ids=["p1"]),
        _task(id="g", status="In progress", project_ids=["p1"]),
        _task(id="b", status="Pending", blocked_by=["ghost"], project_ids=["p1"]),
        _task(id="d", status="Delayed", project_ids=["p1"]),
    ]
    tiers = _tiers(tasks, {"p1": "In progress"})
    assert [(k, t["id"]) for k, t in tiers["active"]] == [
        ("delayed", "d"),
        ("blocked", "b"),
        ("in_progress", "g"),
        ("todo", "w"),
    ]


# ── project_meta_resolver ───────────────────────────────────────────────────


def test_project_meta_resolver_bulk_hit_skips_network(monkeypatch):
    def _boom(_pid):
        raise AssertionError("벌크 맵 히트는 페이지 GET을 부르면 안 됨")

    monkeypatch.setattr(ns, "_fetch_page_meta", _boom)
    resolve = ns.project_meta_resolver({"p1": _project(status="Done")})
    assert resolve("p1")["status"] == "Done"
    assert resolve("p1")["name"] == "프로젝트"


def test_project_meta_resolver_fallback_and_failure_demotes(monkeypatch):
    calls = []

    def _fake(pid):
        calls.append(pid)
        if pid == "archived":
            return {"name": "복구", "status": "Done"}
        raise RuntimeError("GET 실패")

    monkeypatch.setattr(ns, "_fetch_page_meta", _fake)
    resolve = ns.project_meta_resolver({})
    # 벌크 맵 미스(아카이브) → GET 폴백으로 상태 해석 (티어③ 판정 가능)
    assert resolve("archived") == {"name": "복구", "status": "Done"}
    # GET까지 실패 → 빈 메타 강등 (예외 전파 없음)
    assert resolve("gone") == {"name": "", "status": ""}
    resolve("archived")
    resolve("gone")
    assert calls == ["archived", "gone"]  # dict 캐시 — 실패도 1회만


def test_project_meta_resolver_no_fallback_skips_network(monkeypatch):
    # 벌크 조회 자체가 실패한 경우: GET 폴백 봉인 → 프로젝트 수만큼 GET 폭주 방지
    def _boom(_pid):
        raise AssertionError("allow_fallback=False는 페이지 GET을 부르면 안 됨")

    monkeypatch.setattr(ns, "_fetch_page_meta", _boom)
    resolve = ns.project_meta_resolver({}, allow_fallback=False)
    assert resolve("any") == {"name": "", "status": ""}


# ── project_task_counts ─────────────────────────────────────────────────────


def test_project_task_counts():
    tasks = [
        _task(id="t1", status="Done", project_ids=["p1"]),
        _task(id="t2", status="In progress", project_ids=["p1"]),
        _task(id="t3", status="Drop", project_ids=["p1", "p2"]),  # Drop도 완료 그룹
        _task(id="t4"),  # 프로젝트 없음 — 집계 제외
    ]
    assert ns.project_task_counts(tasks) == {"p1": (2, 3), "p2": (1, 1)}


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


def test_format_report_summary_and_tier_sections():
    tasks = [
        _task(id="d1", status="Delayed", project_ids=["p1"]),
        _task(id="g1", status="In progress"),
        _task(id="done1", status="Done"),
    ]
    report = _report(tasks, {"p1": "In progress"})
    # 요약 라인은 기존 urgency 버킷 개수 유지
    assert "전체 3 · 지연 1 · 차단 0 · 진행중 1 · 대기 0 · 완료 1" in report
    # 1차 그룹은 프로젝트 티어 섹션
    assert "🔵 진행중 프로젝트 (1)" in report
    assert "📦 기타 (1)" in report
    # 아이템은 urgency 이모지가 불릿
    assert "🔴 <https://notion.so/t1|태스크>" in report
    assert "✅ 완료" not in report  # 완료는 기본 개수만


def test_format_report_tier_section_order():
    tasks = [
        _task(id="a", project_ids=["act"]),
        _task(id="s", status="Pending", plan_start="2026-08-01"),
        _task(id="i", project_ids=["fin"]),
        _task(id="r", status="Pending"),
    ]
    report = _report(tasks, {"act": "In progress", "fin": "Done"})
    positions = [
        report.index(label)
        for label in ("🔵 진행중 프로젝트", "📅 예정", "⚠️ 정합성 이슈", "📦 기타")
    ]
    assert positions == sorted(positions)  # ①→②→③→④


def test_format_report_show_done_lists_items():
    report = _report([_task(status="Done")], show_done=True)
    assert "✅ 완료 (1)" in report
    assert "• <https://notion.so/t1|태스크>" in report  # 완료 아이템은 기본 불릿


def test_format_report_caps_section():
    many = [_task(id=f"t{i}", status="Delayed") for i in range(13)]  # 전부 티어④
    report = _report(many)
    assert "… 외 3건" in report


def test_format_report_item_meta():
    task = _task(
        assignees=["kkalla"],
        plan_end="2026-07-10",
        priority="High",
        project_ids=["p1"],
    )
    buckets = ns.classify([task], TODAY)
    tiers = ns.group_tiers(buckets, lambda pid: "In progress")
    report = ns.format_report(buckets, tiers, TODAY, lambda pid: "AI 프로젝트")
    assert "AI 프로젝트 · kkalla · ~07-10 · 🔺High" in report


def test_format_report_empty():
    report = _report([])
    assert "조건에 맞는 태스크가 없어요" in report


# ── format_projects_report ──────────────────────────────────────────────────


def test_format_projects_report_groups_and_summary():
    projects = [
        _project(id="a", status="In progress"),
        _project(id="b", status="Pending"),
        _project(id="c", status="Not started"),
        _project(id="d", status="Done"),
        _project(id="e", status="Fail"),
        _project(id="f", status="Drop"),
    ]
    report = ns.format_projects_report(projects, TODAY, {})
    assert "전체 6 · 진행중 1 · 예정 2 · 종료 3" in report
    assert "🔵 진행중 (1)" in report
    assert "📅 예정 (2)" in report
    assert "✅ 종료 (3)" in report


def test_format_projects_report_unknown_status_goes_todo():
    report = ns.format_projects_report([_project(status="이상한값")], TODAY, {})
    assert "📅 예정 (1)" in report


def test_format_projects_report_item_meta_and_completion():
    p = _project(
        product="Vision",
        owners=["kkalla"],
        date_start="2026-07-01",
        date_end="2026-09-30",
    )
    report = ns.format_projects_report([p], TODAY, {"p1": (3, 8)})
    assert (
        "• <https://notion.so/p1|프로젝트> — Vision · kkalla · 07-01~09-30 · 완료 3/8"
        in report
    )


def test_format_projects_report_omits_empty_meta_and_zero_task_completion():
    # 빈 값(Product/Owner/Date)과 태스크 0개 완료율은 아이템에서 생략
    report = ns.format_projects_report([_project()], TODAY, {})
    assert "• <https://notion.so/p1|프로젝트>" in report.splitlines()


def test_format_projects_report_caps_section():
    many = [_project(id=f"p{i}") for i in range(12)]
    report = ns.format_projects_report(many, TODAY, {})
    assert "… 외 2건" in report


def test_format_projects_report_empty():
    report = ns.format_projects_report([], TODAY, {})
    assert "프로젝트가 없어요" in report
