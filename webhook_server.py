"""FastAPI webhook entrypoint for GitLab MR auto-review.

수신한 webhook을 검증/필터링한 뒤, 자격이 되는 MR에 대해서만
review_runner.py를 백그라운드 태스크로 실행한다.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("webhook_server")


def _required_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise RuntimeError(f"필수 환경변수 누락 또는 빈 값: {key}")
    return v


_WEBHOOK_SECRET_RAW = _required_env("WEBHOOK_SECRET")
if len(_WEBHOOK_SECRET_RAW) < 16:
    raise RuntimeError("WEBHOOK_SECRET은 최소 16자 이상이어야 함")
if _WEBHOOK_SECRET_RAW.lower().startswith("change-me"):
    raise RuntimeError(
        "WEBHOOK_SECRET이 .env.example의 더미값으로 보임. 실제 시크릿으로 교체할 것."
    )
WEBHOOK_SECRET = _WEBHOOK_SECRET_RAW

REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "").strip() or "max"
TARGET_ACTIONS = frozenset({"open", "update"})

MAX_REQUEST_BYTES = 1_000_000
# review_runner 외곽 가드. 내부 한도 worst-case:
#   GitLab API ~30s + git clone 120s + git fetch 60s
#   + _ensure_base_reachable deepen 경로 ~600s
#       (DEEPEN_STEPS 2회 × source/target 각 120s = 480s + --unshallow 120s)
#   + claude 600s + post_comment/notify_failure ~30s ≈ 1440s.
# 외곽 가드는 반드시 내부 worst-case보다 커야 review_runner가 자체 타임아웃을
# 처리하고 실패 알림 코멘트를 게시할 수 있다 (작으면 SIGKILL되어 알림 누락).
SUBPROCESS_TIMEOUT_SEC = 1800  # 30분 — 내부 worst-case ~1440s + 여유

# asyncio.create_task 결과를 강참조로 잡아둬야 GC가 mid-run 태스크를 수거하지 않는다.
_RUNNING_TASKS: set[asyncio.Task] = set()
# 동일 MR 중복 webhook 차단 — (project_id, mr_iid)
_IN_FLIGHT_MRS: set[tuple[int, int]] = set()

_REVIEW_RUNNER_PATH = str(Path(__file__).resolve().parent / "review_runner.py")

app = FastAPI(title="gitlab-ai-reviewer")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _verify_token(header_token: str | None) -> bool:
    """타이밍 공격 방어 — 상수 시간 비교."""
    if not isinstance(header_token, str):
        return False
    return hmac.compare_digest(header_token, WEBHOOK_SECRET)


def _coerce_positive_int(v: object) -> int | None:
    if isinstance(v, bool):  # bool은 int 서브클래스라 먼저 거른다
        return None
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str):
        try:
            n = int(v)
        except ValueError:
            return None
        return n if n > 0 else None
    return None


@app.post("/webhook/gitlab")
async def handle(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
) -> Response:
    # 1) 토큰 검증 — spec: "본문 없이 401만 반환"
    if not _verify_token(x_gitlab_token):
        logger.warning("rejected webhook: invalid X-Gitlab-Token header")
        return Response(status_code=401)

    # 2) Content-Length 가드
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                logger.warning("rejected webhook: payload too large (%s bytes)", content_length)
                return JSONResponse(
                    status_code=413,
                    content={"status": "rejected", "reason": "payload too large"},
                )
        except ValueError:
            pass

    # 3) JSON 파싱 가드
    try:
        payload = await request.json()
    except Exception:
        logger.warning("rejected webhook: invalid JSON body")
        return JSONResponse(status_code=400, content={"status": "rejected", "reason": "invalid JSON"})

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=200, content={"status": "skipped", "reason": "payload not an object"}
        )

    # 4) 액션 필터 (type 가드 포함)
    object_attrs = payload.get("object_attributes")
    if not isinstance(object_attrs, dict):
        return {"status": "skipped", "reason": "missing or invalid object_attributes"}

    action = object_attrs.get("action")
    if action not in TARGET_ACTIONS:
        logger.info("skip: action=%s not in %s", action, sorted(TARGET_ACTIONS))
        return {"status": "skipped", "reason": f"action={action}"}

    # 5) 리뷰어 필터
    reviewers_raw = payload.get("reviewers")
    if not isinstance(reviewers_raw, list):
        return {"status": "skipped", "reason": "reviewers not a list"}

    reviewer_usernames = [
        r.get("username") for r in reviewers_raw if isinstance(r, dict)
    ]
    if REVIEWER_USERNAME not in reviewer_usernames:
        logger.info(
            "skip: reviewer '%s' not among %s", REVIEWER_USERNAME, reviewer_usernames
        )
        return {"status": "skipped", "reason": "reviewer not matched"}

    # 6) ID 정합성
    project = payload.get("project")
    project_id = _coerce_positive_int(project.get("id")) if isinstance(project, dict) else None
    mr_iid = _coerce_positive_int(object_attrs.get("iid"))
    if project_id is None or mr_iid is None:
        logger.warning("skip: missing/invalid project.id or object_attributes.iid")
        return {"status": "skipped", "reason": "missing or invalid project_id/mr_iid"}

    # 7) 동일 MR 중복 차단
    key = (project_id, mr_iid)
    if key in _IN_FLIGHT_MRS:
        logger.info("skip: review already in progress for project=%s mr=%s", project_id, mr_iid)
        return {"status": "skipped", "reason": "review in progress"}

    _IN_FLIGHT_MRS.add(key)
    logger.info("dispatch: project_id=%s mr_iid=%s", project_id, mr_iid)
    task = asyncio.create_task(_run_review(project_id, mr_iid))
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)
    task.add_done_callback(lambda _t, k=key: _IN_FLIGHT_MRS.discard(k))

    return {"status": "review started"}


async def _run_review(project_id: int, mr_iid: int) -> None:
    """review_runner.py를 별도 프로세스로 실행. 실패해도 서버는 계속 동작.

    자식 stdout/stderr는 부모(컨테이너 stdout/stderr)에 그대로 상속 — 자식이 실시간으로
    찍는 진행 로그가 즉시 docker logs에 보이도록 한다. PIPE로 캡처하지 않는다.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",  # 자식 Python의 stdout/stderr 버퍼링 끄기 (실시간 flush)
            _REVIEW_RUNNER_PATH,
            str(project_id),
            str(mr_iid),
            # stdout/stderr 명시 안 함 = 부모에 상속
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=SUBPROCESS_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.error(
                "review_runner timeout after %ss (project=%s mr=%s) — terminating",
                SUBPROCESS_TIMEOUT_SEC,
                project_id,
                mr_iid,
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return

        if proc.returncode != 0:
            logger.error(
                "review_runner failed (project=%s mr=%s rc=%s) — 자세한 사유는 위쪽 stderr 로그 참고",
                project_id,
                mr_iid,
                proc.returncode,
            )
        else:
            logger.info("review_runner ok (project=%s mr=%s)", project_id, mr_iid)
    except Exception:
        logger.exception("review_runner crashed (project=%s mr=%s)", project_id, mr_iid)
