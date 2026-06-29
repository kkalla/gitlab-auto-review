"""Slack Web API 헬퍼 (httpx 기반).

review_runner.py가 리뷰 완료/실패를 Slack DM으로 알릴 때 사용한다.
SLACK_BOT_TOKEN이 없으면 모든 호출이 조용히 no-op이 되어(best-effort), 로컬 CLI
실행이나 테스트에서 Slack 설정 없이도 review_runner가 정상 동작한다.

slack_bolt에 **의존하지 않는다** — review_runner의 런타임/테스트 의존성을 가볍게
유지하기 위함이다(봇 프로세스 slack_bot.py만 slack_bolt를 쓴다). 여기서는 httpx로
Slack Web API를 직접 호출한다.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("slack_notifier")

_SLACK_API = "https://slack.com/api"
_HTTP_TIMEOUT = 10.0

# 봇 토큰(xoxb-...). 비어 있으면 enabled()=False → 모든 전송이 no-op.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()

# GitLab MR URL에서 namespace/project 경로 + iid 추출.
#   https://git.example.com:8443/group/sub/repo/-/merge_requests/123 → ("group/sub/repo", 123)
_MR_URL_RE = re.compile(
    r"https?://[^/\s]+/(?P<path>[^\s]+?)/-/merge_requests/(?P<iid>\d+)"
)


def enabled() -> bool:
    """Slack 전송이 가능한 상태인지(봇 토큰 존재) 여부."""
    return bool(SLACK_BOT_TOKEN)


def parse_mr_url(text: str) -> tuple[str, int] | None:
    """문자열에서 GitLab MR URL을 찾아 (project_path, mr_iid)를 반환한다.

    매칭 실패 시 None. slack_bot.py가 멘션 텍스트 파싱에 사용하며, slack_bolt에
    의존하지 않는 순수 함수로 두어 단위 테스트가 쉽도록 여기에 둔다.
    """
    m = _MR_URL_RE.search(text or "")
    if not m:
        return None
    return m.group("path"), int(m.group("iid"))


def _post(method: str, payload: dict) -> dict[str, Any]:
    """Slack Web API POST (application/json). 실패해도 예외를 던지지 않는다."""
    if not SLACK_BOT_TOKEN:
        return {"ok": False, "error": "no_token"}
    try:
        resp = httpx.post(
            f"{_SLACK_API}/{method}",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json()
    except Exception:
        logger.exception("Slack %s 호출 실패", method)
        return {"ok": False, "error": "request_failed"}
    if not isinstance(data, dict) or not data.get("ok"):
        logger.warning(
            "Slack %s 응답 오류: %s",
            method,
            data.get("error") if isinstance(data, dict) else "(non-dict)",
        )
    return data if isinstance(data, dict) else {"ok": False, "error": "bad_response"}


def _get(method: str, params: dict) -> dict[str, Any]:
    """Slack Web API GET. 실패해도 예외를 던지지 않는다."""
    if not SLACK_BOT_TOKEN:
        return {"ok": False, "error": "no_token"}
    try:
        resp = httpx.get(
            f"{_SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params=params,
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json()
    except Exception:
        logger.exception("Slack %s 호출 실패", method)
        return {"ok": False, "error": "request_failed"}
    if not isinstance(data, dict) or not data.get("ok"):
        logger.warning(
            "Slack %s 응답 오류: %s",
            method,
            data.get("error") if isinstance(data, dict) else "(non-dict)",
        )
    return data if isinstance(data, dict) else {"ok": False, "error": "bad_response"}


def lookup_user_id_by_email(email: str) -> str | None:
    """이메일로 Slack member ID 조회 (users.lookupByEmail). 실패 시 None.

    `users:read.email` 스코프가 필요하다. GitLab assignee → Slack 사용자 매핑에 쓴다.
    """
    if not email:
        return None
    data = _get("users.lookupByEmail", {"email": email})
    user = data.get("user")
    if isinstance(user, dict) and isinstance(user.get("id"), str):
        return user["id"]
    return None


def _open_dm_channel(user_id: str) -> str | None:
    """user_id와의 DM 채널을 열어 channel id를 반환 (conversations.open).

    `im:write` 스코프가 필요하다. 봇이 먼저 말 건 적 없는 사용자에게도 DM하려면
    채널을 먼저 열어야 안정적이다.
    """
    data = _post("conversations.open", {"users": user_id})
    channel = data.get("channel")
    if isinstance(channel, dict) and isinstance(channel.get("id"), str):
        return channel["id"]
    return None


def send_dm(user_id: str, text: str) -> bool:
    """user_id에게 DM 전송. 성공 여부 반환 (best-effort, 예외 없음).

    text는 Slack mrkdwn으로 렌더된다 — `<url|라벨>` 링크 문법 사용 가능.
    """
    if not user_id or not enabled():
        return False
    channel = _open_dm_channel(user_id)
    if not channel:
        return False
    return bool(_post("chat.postMessage", {"channel": channel, "text": text}).get("ok"))
