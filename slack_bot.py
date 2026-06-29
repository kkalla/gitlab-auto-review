"""Slack Socket Mode 봇 — GitLab MR 리뷰를 트리거한다.

`webhook_server.py`(FastAPI 공개 엔드포인트)의 대안 진입점이다. **Socket Mode**라
공개 inbound 포트·URL·리버스 프록시가 필요 없다 — 봇이 Slack으로 아웃바운드
WebSocket을 직접 연다(방화벽/NAT 무관).

트리거 3종:
    1. @멘션(수동)            — app_mention 이벤트, MR URL을 함께 멘션
    2. GitLab 채널 알림(자동)  — message 이벤트, GitLab Slack notification이 뿌린
                                 MR 링크를 잡는다(주로 MR open 시)
    3. 주기 폴링(자동, push 증분) — GitLab Slack 알림은 MR push를 채널에 안 띄우므로,
                                 봇이 직접 리뷰어 지정 열린 MR의 source SHA를 주기적으로
                                 확인해 변경분(새 push)을 리뷰한다

공통 흐름:
      → URL/목록에서 project_id/mr_iid 해석
      → review_runner.py 서브프로세스 실행 (증분 리뷰는 review_runner가 MR
        코멘트의 reviewed-sha 마커로 자체 처리 — 별도 oldrev 불필요)
    리뷰어·assignee DM(완료/실패)은 review_runner가 직접 보낸다 (slack_notifier).

review_runner는 별도 프로세스로 실행한다 — claude가 타임아웃으로 SIGKILL되거나
크래시해도 봇 프로세스(WebSocket 연결)는 살아 있게 하기 위함이다. webhook_server와
동일한 격리 원칙.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import slack_notifier

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("slack_bot")


def _required_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise RuntimeError(f"필수 환경변수 누락 또는 빈 값: {key}")
    return v


# Bot 토큰(xoxb-, chat:write/app_mentions:read/users:read.email/im:write 등) +
# App 토큰(xapp-, connections:write — Socket Mode 전용). 둘 다 없으면 부팅 실패.
SLACK_BOT_TOKEN = _required_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _required_env("SLACK_APP_TOKEN")

# MR URL의 project 경로(group/sub/repo)를 숫자 project_id로 해석할 때 사용.
GITLAB_URL = _required_env("GITLAB_URL").rstrip("/")
GITLAB_TOKEN = _required_env("GITLAB_TOKEN")

# 폴링 자동 증분 리뷰. GitLab Slack 알림은 MR push(새 커밋)를 채널에 안 띄우므로
# message 자동 트리거로는 push 증분을 못 잡는다 → 봇이 직접 GitLab API로 리뷰어 지정
# 열린 MR의 source SHA를 POLL_INTERVAL_SEC마다 확인해 변경분을 리뷰한다.
#   REVIEWER_USERNAME: 폴링 대상 MR 필터(reviewer_username). 실패 알림 @멘션과 동일 값.
#   POLL_INTERVAL_SEC: 폴링 간격(초). 0이면 폴러 비활성화(@멘션·채널 알림만).
REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "max").strip()
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "300"))

# review_runner 서브프로세스 외곽 가드. review_runner 내부 worst-case(~2040s)보다
# 커야 자체 타임아웃 처리/실패 알림이 가능하다. webhook_server.py와 동일 값.
SUBPROCESS_TIMEOUT_SEC = 2400

_REVIEW_RUNNER_PATH = str(Path(__file__).resolve().parent / "review_runner.py")

# token_verification_enabled=False: 기본값이면 App() 생성 시점에 auth.test를 동기
# 호출한다 — Slack이 잠깐 안 닿으면 부팅이 깨진다. Socket Mode 연결(app 토큰)과
# 실제 첫 chat.postMessage(bot 토큰)에서 어차피 검증되므로 부팅은 네트워크에
# 의존하지 않게 둔다(import도 부수효과 없음).
app = App(token=SLACK_BOT_TOKEN, token_verification_enabled=False)

# 동일 MR 중복 실행 차단 — (project_id, mr_iid). 봇은 멀티스레드로 이벤트를
# 처리하므로 락으로 보호한다.
_inflight_lock = threading.Lock()
_inflight: set[tuple[int, int]] = set()


def _resolve_project_id(project_path: str) -> int | None:
    """project 경로(group/sub/repo)를 숫자 project_id로 해석. 실패 시 None.

    GitLab API는 URL-encoded 경로를 project id 자리에 받아준다.
    review_runner.main()은 숫자 project_id를 요구하므로 여기서 미리 변환한다.
    """
    encoded = quote(project_path, safe="")
    try:
        resp = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{encoded}",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("project 경로 해석 실패: %s", project_path)
        return None
    if isinstance(data, dict) and isinstance(data.get("id"), int):
        return data["id"]
    logger.warning("project 응답에 숫자 id 없음: %s", project_path)
    return None


def _parse_mention(text: str) -> tuple[int, int, str] | None:
    """멘션 텍스트에서 MR을 식별. (project_id, mr_iid, web_url) 반환, 실패 시 None."""
    parsed = slack_notifier.parse_mr_url(text)
    if parsed is None:
        return None
    project_path, mr_iid = parsed
    project_id = _resolve_project_id(project_path)
    if project_id is None:
        return None
    web_url = f"{GITLAB_URL}/{project_path}/-/merge_requests/{mr_iid}"
    return project_id, mr_iid, web_url


def _mr_is_opened(project_id: int, mr_iid: int) -> bool:
    """MR이 열린 상태인지. 조회 실패 시 True(보수적 진행 — review_runner가 최종 방어).

    GitLab Slack notification은 MR close/merge에도 채널 알림을 보내므로, 자동
    트리거(message)가 닫히거나 병합된 MR을 리뷰하지 않도록 state를 확인한다.
    """
    try:
        resp = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception(
            "MR state 조회 실패 (project=%s mr=%s) — 트리거 진행", project_id, mr_iid
        )
        return True
    return isinstance(data, dict) and data.get("state") == "opened"


def _post(channel: str | None, thread_ts: str | None, text: str) -> None:
    """스레드 답글 게시 (best-effort). channel이 없으면(폴링 트리거 등) no-op."""
    if not channel:
        return
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception:
        logger.exception("Slack 스레드 답글 게시 실패")


def _run_review_and_report(
    project_id: int,
    mr_iid: int,
    web_url: str,
    channel: str | None,
    thread_ts: str | None,
) -> None:
    """review_runner를 서브프로세스로 실행하고 종료코드에 따라 스레드에 결과 답글.

    자식 stdout/stderr는 봇 프로세스(=컨테이너 stdout)에 상속 → docker logs로
    진행 상황이 실시간 노출된다. SLACK_* / GITLAB_* env도 그대로 상속되므로
    review_runner가 MR 코멘트 + 리뷰어/assignee DM을 직접 보낸다.

    channel이 None이면(폴링 트리거) 스레드 답글은 _post가 알아서 생략한다 —
    결과 통지는 review_runner의 MR 코멘트 + DM으로 충분하다.
    """
    key = (project_id, mr_iid)
    rc: int | None = None
    try:
        argv = [
            sys.executable,
            "-u",
            _REVIEW_RUNNER_PATH,
            str(project_id),
            str(mr_iid),
        ]
        logger.info("dispatch review_runner: project=%s mr=%s", project_id, mr_iid)
        proc = subprocess.run(argv, timeout=SUBPROCESS_TIMEOUT_SEC, check=False)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        logger.error(
            "review_runner timeout %ss (project=%s mr=%s)",
            SUBPROCESS_TIMEOUT_SEC,
            project_id,
            mr_iid,
        )
        _post(
            channel,
            thread_ts,
            f"⚠️ 리뷰가 시간 초과로 종료됐어요 (MR !{mr_iid}). 컨테이너 로그를 확인해 주세요.",
        )
        return
    except Exception:
        logger.exception(
            "review_runner 실행 오류 (project=%s mr=%s)", project_id, mr_iid
        )
        _post(channel, thread_ts, f"⚠️ 리뷰 실행 중 오류가 발생했어요 (MR !{mr_iid}).")
        return
    finally:
        with _inflight_lock:
            _inflight.discard(key)

    if rc == 0:
        _post(
            channel,
            thread_ts,
            f"✅ 리뷰 완료 — <{web_url}|MR !{mr_iid}>에 코멘트를 남겼어요. DM도 확인해 주세요.",
        )
    else:
        # review_runner가 실패해도 자체적으로 MR에 ⚠️ 코멘트 + 리뷰어 DM을 남긴다.
        _post(
            channel,
            thread_ts,
            f"⚠️ 리뷰가 실패로 끝났어요 (MR !{mr_iid}, rc={rc}). "
            f"<{web_url}|MR>의 실패 알림 코멘트와 로그를 확인해 주세요.",
        )


def _dispatch_review(
    project_id: int,
    mr_iid: int,
    web_url: str,
    channel: str | None = None,
    thread_ts: str | None = None,
    say=None,
) -> bool:
    """in-flight 가드 통과 시 (있으면) ack 답글 + 리뷰 스레드 시작. 이미 진행 중이면 False.

    세 경로가 공유한다: app_mention(수동)·message(GitLab 자동)·폴러(push 증분).
    같은 MR이 여러 경로로 동시에 들어와도 먼저 잡은 쪽만 실행된다. 폴러는 channel/say
    없이 호출하므로 ack·스레드 답글 없이 조용히 돌고, 결과는 review_runner가 MR 코멘트
    + 리뷰어/assignee DM으로 알린다.
    """
    key = (project_id, mr_iid)
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    if say is not None:
        say(
            thread_ts=thread_ts,
            text=f"🔍 MR !{mr_iid} 리뷰를 시작합니다. 보통 수 분 걸려요…",
        )
    # 리뷰는 길게(최대 40분) 걸리므로 Bolt 이벤트 핸들러/폴러를 막지 않도록 별도 스레드.
    threading.Thread(
        target=_run_review_and_report,
        args=(project_id, mr_iid, web_url, channel, thread_ts),
        daemon=True,
    ).start()
    return True


def _extract_text(event: dict) -> str:
    """이벤트 본문 + attachments에서 텍스트를 모은다.

    GitLab의 Slack notification(incoming webhook)은 MR 링크를 `text`가 아니라
    `attachments`(fallback/text/title_link 등)에 넣는다. 자동 트리거가 그 링크를
    잡으려면 attachments까지 훑어야 한다.
    """
    parts = [event.get("text", "")]
    for att in event.get("attachments") or []:
        if isinstance(att, dict):
            parts += [
                str(att.get(k, ""))
                for k in ("fallback", "text", "pretext", "title", "title_link")
            ]
    return "\n".join(p for p in parts if p)


@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """봇이 @멘션되면 호출. MR URL을 파싱해 리뷰를 트리거한다(수동 트리거)."""
    text = event.get("text", "")
    channel = event.get("channel", "")
    # 스레드 안에서 멘션되면 그 스레드에, 아니면 원본 메시지에 답글을 단다.
    thread_ts = event.get("thread_ts") or event.get("ts")

    parsed = _parse_mention(text)
    if parsed is None:
        say(
            thread_ts=thread_ts,
            text=(
                "리뷰할 MR을 못 찾았어요. GitLab Merge Request URL을 함께 멘션해 주세요.\n"
                "예: `@mr-reviewer https://git.example.com/group/repo/-/merge_requests/123`"
            ),
        )
        return

    project_id, mr_iid, web_url = parsed
    if not _dispatch_review(project_id, mr_iid, web_url, channel, thread_ts, say):
        say(
            thread_ts=thread_ts,
            text=f"이미 MR !{mr_iid} 리뷰가 진행 중이에요. 잠시만요…",
        )


@app.event("message")
def handle_channel_message(event: dict, say) -> None:
    """채널 메시지 자동 트리거. GitLab Slack notification이 뿌린 MR 링크를 잡는다.

    GitLab 프로젝트의 Slack integration(Merge request events)을 켜고 봇이 그 채널에
    들어가 있으면, MR이 열릴 때마다 멘션 없이 자동으로 리뷰가 돈다. MR URL이 없는
    일반 대화는 조용히 무시한다. 봇 자신의 메시지(ack·결과 답글)는 Bolt 기본
    ignoring_self_events 미들웨어가 걸러 무한 트리거가 생기지 않는다.
    """
    # 편집/삭제/채널 조인 등은 무시. 일반 메시지(None)와 봇 메시지(bot_message)만 본다.
    if event.get("subtype") not in (None, "bot_message"):
        return

    parsed = _parse_mention(_extract_text(event))
    if parsed is None:
        return  # MR 링크 없는 메시지 — 조용히 무시

    project_id, mr_iid, web_url = parsed
    # GitLab은 MR close/merge에도 채널 알림을 보낸다 — 자동 트리거는 열린 MR만 리뷰한다.
    if not _mr_is_opened(project_id, mr_iid):
        logger.info("skip: 채널 트리거 — MR !%s가 열린 상태가 아님", mr_iid)
        return
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    _dispatch_review(project_id, mr_iid, web_url, channel, thread_ts, say)


def _fetch_open_reviewer_mrs() -> list[dict]:
    """REVIEWER_USERNAME이 리뷰어로 지정된, 열린 MR 목록. 실패 시 빈 리스트.

    각 항목에서 project_id, iid, sha(source branch HEAD), web_url을 쓴다.
    scope=all로 봇 계정이 접근 가능한 모든 프로젝트를 가로질러 조회한다.
    """
    try:
        resp = httpx.get(
            f"{GITLAB_URL}/api/v4/merge_requests",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            params={
                "reviewer_username": REVIEWER_USERNAME,
                "state": "opened",
                "scope": "all",
                "per_page": 100,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("열린 MR 목록 폴링 조회 실패")
        return []
    return data if isinstance(data, list) else []


def _poll_loop() -> None:
    """주기적으로 리뷰어 지정 열린 MR을 폴링해 새로 열린 MR과 새 push를 자동 리뷰한다.

    GitLab Slack 알림은 MR push를 채널에 안 띄우므로(message 자동 트리거 불가) 폴링으로
    보완한다. 첫 순회는 baseline만 잡고(봇 기동 시 기존 MR 일괄 리뷰 방지) 이후 **기동 후
    새로 열린 MR**(seen에 없던 것)과 **source SHA가 바뀐 MR**(새 push)을 트리거한다. 실제
    증분/스킵 판단(새 커밋 0개면 스킵)은 review_runner의 reviewed-sha 마커가 처리하므로,
    폴러는 "신규/변경 감지 → 호출"만 담당한다.

    트레이드오프: 봇 재시작 시 seen 캐시가 비어 그 사이 들어온 push는 baseline에 흡수돼
    한 번 놓칠 수 있다(수동 @멘션으로 커버). in-flight 가드로 멘션과의 중복은 막힌다.
    """
    seen: dict[int, str] = {}  # mr_iid -> 마지막으로 본 source SHA
    first = True
    while True:
        try:
            for mr in _fetch_open_reviewer_mrs():
                iid = mr.get("iid")
                sha = mr.get("sha")
                pid = mr.get("project_id")
                web_url = mr.get("web_url", "")
                if not isinstance(iid, int) or not isinstance(pid, int) or not sha:
                    continue
                prev = seen.get(iid)
                seen[iid] = sha
                if first or prev == sha:
                    continue  # baseline(기동 시 기존 MR)이거나 변경 없음
                # prev is None → 기동 후 새로 열린 MR(open 자동 리뷰)
                # prev != sha  → 기존 MR에 새 push(증분)
                change = f"{prev[:8]}→{sha[:8]}" if prev else f"신규 open {sha[:8]}"
                logger.info("폴링: MR !%s %s → 리뷰 트리거", iid, change)
                _dispatch_review(pid, iid, web_url)
            first = False
        except Exception:
            logger.exception("폴링 루프 오류")
        time.sleep(POLL_INTERVAL_SEC)


def main() -> None:
    if not slack_notifier.enabled():
        logger.warning(
            "SLACK_BOT_TOKEN이 slack_notifier에 보이지 않음 — 리뷰어/assignee DM이 비활성화됩니다."
        )
    if POLL_INTERVAL_SEC > 0:
        logger.info(
            "MR 폴러 시작 — %s초 간격, 리뷰어=%s (push 증분 자동 리뷰)",
            POLL_INTERVAL_SEC,
            REVIEWER_USERNAME,
        )
        threading.Thread(target=_poll_loop, daemon=True).start()
    else:
        logger.info("MR 폴러 비활성화 (POLL_INTERVAL_SEC=0)")
    logger.info("slack_bot 시작 — Socket Mode 연결 시도")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
