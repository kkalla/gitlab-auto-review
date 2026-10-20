"""Slack Socket Mode 봇 — GitLab MR 리뷰를 트리거한다.

`webhook_server.py`(FastAPI 공개 엔드포인트)의 대안 진입점이다. **Socket Mode**라
공개 inbound 포트·URL·리버스 프록시가 필요 없다 — 봇이 Slack으로 아웃바운드
WebSocket을 직접 연다(방화벽/NAT 무관).

트리거 2종:
    1. @멘션(수동)        — app_mention 이벤트, MR URL을 함께 멘션하면 바로 리뷰
    2. 리뷰 요청 DM + 버튼 — 폴러가 리뷰어 지정 열린 MR의 신규 지정·새 push를 감지하면
                             리뷰어에게 DM(MR 링크 + intent/코드 리뷰 안내 + 「코드 리뷰하기」
                             버튼)을 보낸다. 리뷰는 버튼을 눌러야 돈다(자동 실행 안 함)

Notion 조회 슬래시 커맨드(/task-status·/project-status)는 98_ags-watchtower로
분리됐다 — 이 봇은 MR 리뷰 전용이다.

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

import json
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

# 리뷰 요청 폴링. 봇이 GitLab API로 리뷰어 지정 열린 MR의 source SHA를
# POLL_INTERVAL_SEC마다 확인해 신규 지정·새 push를 리뷰어에게 DM으로 알린다.
#   REVIEWER_USERNAME: 폴링 대상 MR 필터(reviewer_username). 실패 알림 @멘션과 동일 값.
#   POLL_INTERVAL_SEC: 폴링 간격(초). 0이면 폴러 비활성화(@멘션만).
REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "max").strip()
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "300"))
# 리뷰 요청 DM 수신자이자 「코드 리뷰하기」 버튼을 누를 수 있는 유일한 사용자.
# 비면 폴러를 띄우지 않는다(DM 보낼 곳이 없음).
REVIEWER_SLACK_ID = os.environ.get("REVIEWER_SLACK_ID", "").strip()

REVIEW_ACTION_ID = "run_code_review"

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


def _post(channel: str | None, thread_ts: str | None, text: str) -> None:
    """스레드 답글 게시 (best-effort). channel이 없으면 no-op."""
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

    rc=0은 "리뷰 게시"만이 아니다 — 닫힌 MR·fork MR·새 커밋 0개도 코멘트 없이 0으로
    끝나므로 성공 답글은 게시 여부를 단정하지 않는다.
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
            f"⚠️ 리뷰가 시간 초과로 종료됐어요 (MR !{mr_iid}). 컨테이너 로그를 확인해 주세요. "
            "다시 돌리려면 봇을 MR URL과 함께 @멘션해 주세요.",
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
            f"✅ 리뷰 처리가 끝났어요 — <{web_url}|MR !{mr_iid}>의 코멘트를 확인해 주세요. "
            "(닫힌 MR이거나 새 커밋이 없으면 코멘트 없이 끝나요)",
        )
    else:
        # review_runner가 실패해도 자체적으로 MR에 ⚠️ 코멘트 + 리뷰어 DM을 남긴다.
        _post(
            channel,
            thread_ts,
            f"⚠️ 리뷰가 실패로 끝났어요 (MR !{mr_iid}, rc={rc}). "
            f"<{web_url}|MR>의 실패 알림 코멘트와 로그를 확인해 주세요. "
            "다시 돌리려면 봇을 MR URL과 함께 @멘션해 주세요.",
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

    두 경로가 공유한다: app_mention(수동)·DM 「코드 리뷰하기」 버튼. 같은 MR이 동시에
    들어와도 먼저 잡은 쪽만 실행된다. 결과는 channel/thread_ts 스레드 답글 + review_runner의
    MR 코멘트·리뷰어/assignee DM으로 알린다.
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
                "예: `@<이 봇> https://git.example.com/group/repo/-/merge_requests/123`"
            ),
        )
        return

    project_id, mr_iid, web_url = parsed
    if not _dispatch_review(project_id, mr_iid, web_url, channel, thread_ts, say):
        say(
            thread_ts=thread_ts,
            text=f"이미 MR !{mr_iid} 리뷰가 진행 중이에요. 잠시만요…",
        )


@app.action(REVIEW_ACTION_ID)
def handle_review_button(ack, body: dict, client) -> None:
    """리뷰 요청 DM의 「코드 리뷰하기」 버튼. 기존 자동 리뷰를 돌려 MR에 코멘트를 남긴다."""
    ack()
    # DM은 REVIEWER_SLACK_ID에게만 가지만, 버튼 payload는 누른 사람 기준으로 다시 확인한다.
    if not REVIEWER_SLACK_ID or (body.get("user") or {}).get("id") != REVIEWER_SLACK_ID:
        logger.warning("리뷰 버튼: 허용되지 않은 사용자 클릭 무시 (%s)", body.get("user"))
        return
    try:
        target = json.loads(body["actions"][0]["value"])
        project_id, mr_iid, web_url = int(target["p"]), int(target["i"]), str(target["u"])
        channel = body["channel"]["id"]
        message = body["message"]
    except (KeyError, IndexError, TypeError, ValueError):
        logger.exception("리뷰 버튼 payload 해석 실패")
        return

    ts = message["ts"]
    started = _dispatch_review(project_id, mr_iid, web_url, channel, ts)
    note = (
        f"🔍 코드 리뷰를 시작했어요 (MR !{mr_iid}). 결과는 이 스레드로 알려 드려요."
        if started
        else f"이미 MR !{mr_iid} 리뷰가 진행 중이에요."
    )
    # 버튼을 지워 같은 DM에서 재클릭을 막는다(새 push가 오면 새 DM에 새 버튼이 붙는다).
    blocks = [b for b in message.get("blocks", []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    try:
        client.chat_update(channel=channel, ts=ts, text=message.get("text", ""), blocks=blocks)
    except Exception:
        logger.exception("리뷰 버튼: DM 갱신 실패")


# 페이지네이션 상한 (per_page=100이면 2000건, 기본 20이면 400건). 넘으면 일부만 본 결과로 판단하지
# 않도록 None을 돌려 호출부가 "조회 불가"로 다루게 한다.
_MAX_GITLAB_PAGES = 20


def _gitlab_get_all(url: str, params: dict, per_page: int | None = 100) -> list | None:
    """GitLab 목록 API를 X-Next-Page를 따라 끝까지 읽는다. 상한 초과·비목록 응답이면 None.

    per_page=None이면 GitLab 기본값(20)에 맡긴다. HTTP 오류는 httpx.HTTPStatusError로
    그대로 던진다(호출부가 폴백 등 판단).
    """
    size = {"per_page": per_page} if per_page else {}
    items: list = []
    page = "1"
    for _ in range(_MAX_GITLAB_PAGES):
        resp = httpx.get(
            url,
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            params={**params, **size, "page": page},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return None
        items += data
        page = resp.headers.get("X-Next-Page", "")
        if not page:
            return items
    logger.warning("GitLab 페이지 상한(%s) 초과 — 결과 버림: %s", _MAX_GITLAB_PAGES, url)
    return None


def _fetch_changed_paths(project_id: int, mr_iid: int) -> list[str]:
    """MR 변경 파일 경로 목록. 실패·상한 초과 시 빈 리스트(→ 리뷰 유형 판별 불가).

    `/diffs`(GitLab 15.7+)를 먼저, HTTP 오류면 `/changes`로 재시도한다 — 404(구버전)뿐 아니라
    5xx도: 사내 GitLab은 `/diffs`에 per_page>20을 주면 500을 낸다(2026-10-20 E2E 실측).
    그래서 `/diffs`는 per_page를 보내지 않고 기본값(20)으로 페이지를 넘긴다.
    """
    base = f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    try:
        try:
            files = _gitlab_get_all(f"{base}/diffs", {}, per_page=None)
        except httpx.HTTPStatusError as e:
            logger.warning("MR /diffs 조회 HTTP %s — /changes로 재시도", e.response.status_code)
            resp = httpx.get(f"{base}/changes", headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            # overflow=true면 GitLab이 변경 목록을 잘랐다 — 일부만 보고 판별하지 않는다.
            files = None if not isinstance(data, dict) or data.get("overflow") else data.get("changes")
    except Exception:
        logger.exception("MR 변경 파일 조회 실패 (project=%s mr=%s)", project_id, mr_iid)
        return []
    if not isinstance(files, list):
        return []
    return [f.get("new_path") or f.get("old_path") or "" for f in files if isinstance(f, dict)]


def _escape_mrkdwn(text: str) -> str:
    """Slack mrkdwn 제어 문자 이스케이프 — MR 제목이 링크 문법을 깨지 않게."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "¦")


_REVIEW_TYPE_GUIDE = {
    "intent": (
        "📝 *Intent 리뷰* 요청이에요. intent·ADR 문서만 바뀐 MR이라 문서를 직접 읽고 "
        "MR 코멘트로 답해 주세요. 코드 리뷰가 필요하면 아래 버튼을 눌러도 돼요."
    ),
    "code": "💻 *코드 리뷰* 요청이에요. 아래 버튼을 누르면 AI 자동 리뷰를 돌려 MR에 코멘트를 남겨요.",
    None: "❔ 리뷰 유형을 판별하지 못했어요(변경 파일 조회 실패). MR에서 직접 확인해 주세요.",
}


def _notify_review_request(mr: dict, reason: str) -> bool:
    """리뷰어에게 리뷰 요청 DM — MR 링크 + intent/코드 리뷰 안내 + 「코드 리뷰하기」 버튼.

    전송 성공 여부를 반환한다 — 폴러는 성공했을 때만 SHA를 기록해 실패분을 다음 순회에 재시도.
    """
    project_id, mr_iid, web_url = mr["project_id"], mr["iid"], mr.get("web_url", "")
    review_type = slack_notifier.classify_review_type(_fetch_changed_paths(project_id, mr_iid))
    title = _escape_mrkdwn(str(mr.get("title", ""))[:200])
    guide = _REVIEW_TYPE_GUIDE[review_type]
    text = f"🔔 리뷰 요청 — <{web_url}|MR !{mr_iid}> {title}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{text}\n_{reason}_\n\n{guide}"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": REVIEW_ACTION_ID,
                    "text": {"type": "plain_text", "text": "코드 리뷰하기"},
                    "style": "primary",
                    "value": json.dumps({"p": project_id, "i": mr_iid, "u": web_url}),
                }
            ],
        },
    ]
    sent = slack_notifier.send_dm(REVIEWER_SLACK_ID, text, blocks)
    if not sent:
        logger.warning("리뷰 요청 DM 전송 실패 (MR !%s) — 다음 순회에 재시도", mr_iid)
    return sent


def _fetch_open_reviewer_mrs() -> list[dict] | None:
    """REVIEWER_USERNAME이 리뷰어로 지정된, 열린 MR 목록. 조회 실패 시 None.

    실패를 빈 리스트로 돌리면 "열린 MR 0개"와 구분되지 않아, baseline이 빈 채로 잡혀
    다음 순회에 기존 MR 전부가 신규 지정으로 오인된다(DM 폭주). 그래서 None으로 구분한다.
    scope=all로 봇 계정이 접근 가능한 모든 프로젝트를 가로질러 조회한다.
    """
    try:
        return _gitlab_get_all(
            f"{GITLAB_URL}/api/v4/merge_requests",
            {"reviewer_username": REVIEWER_USERNAME, "state": "opened", "scope": "all"},
        )
    except Exception:
        logger.exception("열린 MR 목록 폴링 조회 실패")
        return None


def _poll_once(seen: dict[tuple[int, int], str], baseline: bool) -> bool:
    """폴링 1회. 조회에 성공하면 True(호출부가 baseline 완료로 본다), 실패면 False.

    seen((project_id, iid) → 마지막으로 DM한 source SHA)을 제자리 갱신한다.
    - baseline: 기록만 하고 DM은 안 보낸다(봇 기동 시 기존 MR 일괄 DM 방지).
    - 신규 지정(seen에 없음)·새 push(SHA 변경)는 DM, **전송 성공 시에만** 기록 → 실패분 재시도.
    - 목록에서 빠진 MR(리뷰어 해제·close·merge)은 지운다 → 재지정되면 다시 DM.
    - fork MR은 review_runner가 스킵하므로 DM하지 않는다.
    """
    mrs = _fetch_open_reviewer_mrs()
    if mrs is None:
        return False  # 조회 실패 — seen을 건드리지 않는다
    current: set[tuple[int, int]] = set()
    for mr in mrs:
        iid, pid, sha = mr.get("iid"), mr.get("project_id"), mr.get("sha")
        if not isinstance(iid, int) or not isinstance(pid, int) or not sha:
            continue
        if mr.get("source_project_id", pid) != pid:
            continue  # fork MR
        key = (pid, iid)
        current.add(key)
        prev = seen.get(key)
        if baseline:
            seen[key] = sha
            continue
        if prev == sha:
            continue
        # prev is None → 새로 리뷰어로 지정된 MR / prev != sha → 기존 MR에 새 push
        reason = f"새 push {prev[:8]}→{sha[:8]}" if prev else "리뷰어로 지정됨"
        logger.info("폴링: MR !%s %s → 리뷰 요청 DM", iid, reason)
        if _notify_review_request(mr, reason):
            seen[key] = sha
    for key in seen.keys() - current:
        del seen[key]
    return True


def _poll_loop() -> None:
    """주기적으로 _poll_once를 돌린다. 첫 **성공** 순회를 baseline으로 잡는다.

    리뷰 실행은 DM 버튼이 맡는다 — 증분/스킵 판단은 review_runner의 reviewed-sha 마커.
    push마다 리뷰 유형을 다시 판별한다 — intent 검토 뒤 같은 MR에 구현이 올라오기 때문.

    트레이드오프: 봇 재시작 시 seen 캐시가 비어 그 사이 지정·push는 baseline에 흡수돼
    한 번 놓칠 수 있다(수동 @멘션으로 커버).
    """
    # iid는 프로젝트별 번호라 단독 키로 쓰면 다른 프로젝트의 같은 번호 MR끼리 SHA를 덮어써
    # 매 순회 "새 push"로 오인한다 → (project_id, iid)로 키를 잡는다.
    seen: dict[tuple[int, int], str] = {}
    baselined = False
    while True:
        try:
            if _poll_once(seen, baseline=not baselined):
                baselined = True
        except Exception:
            logger.exception("폴링 루프 오류")
        time.sleep(POLL_INTERVAL_SEC)


def main() -> None:
    if not slack_notifier.enabled():
        logger.warning(
            "SLACK_BOT_TOKEN이 slack_notifier에 보이지 않음 — 리뷰어/assignee DM이 비활성화됩니다."
        )
    if POLL_INTERVAL_SEC > 0 and not REVIEWER_SLACK_ID:
        logger.warning("REVIEWER_SLACK_ID 비어 있음 — 리뷰 요청 DM 폴러를 띄우지 않습니다.")
    elif POLL_INTERVAL_SEC > 0:
        logger.info(
            "MR 폴러 시작 — %s초 간격, 리뷰어=%s (신규 지정·push → 리뷰 요청 DM)",
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
