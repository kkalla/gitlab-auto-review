"""slack_bot의 폴러·버튼·GitLab 조회 로직 테스트 (네트워크·Slack 연결 없음).

slack_bot은 import 시점에 필수 env를 읽으므로 import 전에 더미 값을 채운다.
App(token_verification_enabled=False)라 import만으로는 네트워크를 타지 않는다.
"""

import json
import os
from unittest.mock import MagicMock, patch

import httpx

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")

import slack_bot as sb

SHA_A = "a" * 40
SHA_B = "b" * 40


def _mr(pid: int = 1, iid: int = 5, sha: str = SHA_A, **extra) -> dict:
    return {"project_id": pid, "iid": iid, "sha": sha, "web_url": f"https://g/{pid}/-/merge_requests/{iid}", **extra}


def _resp(data, next_page: str = "", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=data,
        headers={"X-Next-Page": next_page},
        request=httpx.Request("GET", "https://g/api"),
    )


# ── _poll_once: baseline·변경 감지·실패 처리 ─────────────────────────────────


def test_poll_once_fetch_failure_returns_false_and_keeps_seen():
    seen = {(1, 5): SHA_A}
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=None), \
         patch.object(sb, "_notify_review_request") as notify:
        assert sb._poll_once(seen, baseline=False) is False
    notify.assert_not_called()
    assert seen == {(1, 5): SHA_A}


def test_poll_once_baseline_records_without_dm():
    seen: dict = {}
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=[_mr()]), \
         patch.object(sb, "_notify_review_request") as notify:
        assert sb._poll_once(seen, baseline=True) is True
    notify.assert_not_called()
    assert seen == {(1, 5): SHA_A}


def test_poll_once_new_assignment_and_push_send_dm():
    seen = {(1, 5): SHA_A}
    mrs = [_mr(sha=SHA_B), _mr(iid=6)]
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=mrs), \
         patch.object(sb, "_notify_review_request", return_value=True) as notify:
        sb._poll_once(seen, baseline=False)
    reasons = [c.args[1] for c in notify.call_args_list]
    assert reasons == [f"새 push {SHA_A[:8]}→{SHA_B[:8]}", "리뷰어로 지정됨"]
    assert seen == {(1, 5): SHA_B, (1, 6): SHA_A}


def test_poll_once_same_iid_in_other_projects_do_not_collide():
    seen = {(1, 5): SHA_A, (2, 5): SHA_B}
    mrs = [_mr(pid=1, sha=SHA_A), _mr(pid=2, sha=SHA_B)]
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=mrs), \
         patch.object(sb, "_notify_review_request") as notify:
        sb._poll_once(seen, baseline=False)
    notify.assert_not_called()


def test_poll_once_dm_failure_retries_next_cycle():
    seen = {(1, 5): SHA_A}
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=[_mr(sha=SHA_B)]), \
         patch.object(sb, "_notify_review_request", return_value=False) as notify:
        sb._poll_once(seen, baseline=False)
        sb._poll_once(seen, baseline=False)
    assert notify.call_count == 2
    assert seen == {(1, 5): SHA_A}


def test_poll_once_reassignment_after_removal_sends_dm():
    seen = {(1, 5): SHA_A}
    with patch.object(sb, "_fetch_open_reviewer_mrs", side_effect=[[], [_mr()]]), \
         patch.object(sb, "_notify_review_request", return_value=True) as notify:
        sb._poll_once(seen, baseline=False)  # 리뷰어 해제 → 목록에서 빠짐
        assert seen == {}
        sb._poll_once(seen, baseline=False)  # 같은 SHA로 재지정
    notify.assert_called_once()


def test_poll_once_skips_fork_mr():
    seen: dict = {}
    with patch.object(sb, "_fetch_open_reviewer_mrs", return_value=[_mr(source_project_id=99)]), \
         patch.object(sb, "_notify_review_request") as notify:
        sb._poll_once(seen, baseline=False)
    notify.assert_not_called()


# ── GitLab 페이지네이션 ───────────────────────────────────────────────────


def test_fetch_open_reviewer_mrs_follows_pages():
    pages = [_resp([_mr(iid=1)], next_page="2"), _resp([_mr(iid=2)])]
    with patch.object(sb.httpx, "get", side_effect=pages):
        assert [m["iid"] for m in sb._fetch_open_reviewer_mrs()] == [1, 2]


def test_fetch_open_reviewer_mrs_http_error_returns_none():
    with patch.object(sb.httpx, "get", return_value=_resp({}, status=502)):
        assert sb._fetch_open_reviewer_mrs() is None


def test_fetch_changed_paths_truncated_returns_empty():
    # 페이지 상한을 넘으면 일부 파일만 보고 intent로 오판할 수 있다 → 판별 불가로.
    endless = _resp([{"new_path": "docs/intents/a.md"}], next_page="2")
    with patch.object(sb.httpx, "get", return_value=endless):
        assert sb._fetch_changed_paths(1, 5) == []


def test_fetch_changed_paths_falls_back_to_changes_on_404():
    responses = [_resp({}, status=404), _resp({"changes": [{"new_path": "a.py"}]})]
    with patch.object(sb.httpx, "get", side_effect=responses):
        assert sb._fetch_changed_paths(1, 5) == ["a.py"]


# ── 버튼 핸들러 ──────────────────────────────────────────────────────────


def _button_body(user: str = "U1") -> dict:
    return {
        "user": {"id": user},
        "actions": [{"value": json.dumps({"p": 1, "i": 5, "u": "https://g/x"})}],
        "channel": {"id": "D1"},
        "message": {"ts": "1.2", "text": "t", "blocks": [{"type": "section"}, {"type": "actions"}]},
    }


def test_button_reviewer_click_dispatches_and_removes_button(monkeypatch):
    monkeypatch.setattr(sb, "REVIEWER_SLACK_ID", "U1")
    client = MagicMock()
    with patch.object(sb, "_dispatch_review", return_value=True) as dispatch:
        sb.handle_review_button(MagicMock(), _button_body(), client)
    dispatch.assert_called_once_with(1, 5, "https://g/x", "D1", "1.2")
    blocks = client.chat_update.call_args.kwargs["blocks"]
    assert [b["type"] for b in blocks] == ["section", "context"]


def test_button_other_user_is_ignored(monkeypatch):
    monkeypatch.setattr(sb, "REVIEWER_SLACK_ID", "U1")
    with patch.object(sb, "_dispatch_review") as dispatch:
        sb.handle_review_button(MagicMock(), _button_body(user="U2"), MagicMock())
    dispatch.assert_not_called()


def test_button_empty_reviewer_id_rejects_all(monkeypatch):
    monkeypatch.setattr(sb, "REVIEWER_SLACK_ID", "")
    with patch.object(sb, "_dispatch_review") as dispatch:
        sb.handle_review_button(MagicMock(), _button_body(user=""), MagicMock())
    dispatch.assert_not_called()


def test_button_malformed_value_is_ignored(monkeypatch):
    monkeypatch.setattr(sb, "REVIEWER_SLACK_ID", "U1")
    body = _button_body()
    body["actions"][0]["value"] = "not-json"
    with patch.object(sb, "_dispatch_review") as dispatch:
        sb.handle_review_button(MagicMock(), body, MagicMock())
    dispatch.assert_not_called()


def test_escape_mrkdwn_neutralizes_link_syntax():
    assert sb._escape_mrkdwn("<x|y>&") == "&lt;x¦y&gt;&amp;"
