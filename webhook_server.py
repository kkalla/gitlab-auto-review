"""FastAPI webhook entrypoint for GitLab MR auto-review.

수신한 webhook을 검증/필터링한 뒤, 자격이 되는 MR에 대해서만
review_runner.py를 백그라운드 태스크로 실행한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("webhook_server")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "max")
TARGET_ACTIONS = frozenset({"open", "update"})

app = FastAPI(title="gitlab-ai-reviewer")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/gitlab")
async def handle(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
) -> dict[str, str]:
    if x_gitlab_token != WEBHOOK_SECRET:
        logger.warning("rejected webhook: invalid X-Gitlab-Token header")
        raise HTTPException(status_code=401, detail="invalid token")

    payload = await request.json()

    object_attrs = payload.get("object_attributes") or {}
    action = object_attrs.get("action")
    reviewers = payload.get("reviewers") or []

    if action not in TARGET_ACTIONS:
        logger.info("skip: action=%s not in %s", action, sorted(TARGET_ACTIONS))
        return {"status": "skipped", "reason": f"action={action}"}

    if not any(
        isinstance(r, dict) and r.get("username") == REVIEWER_USERNAME
        for r in reviewers
    ):
        logger.info(
            "skip: reviewer '%s' not among %s",
            REVIEWER_USERNAME,
            [r.get("username") for r in reviewers if isinstance(r, dict)],
        )
        return {"status": "skipped", "reason": "reviewer not matched"}

    project = payload.get("project") or {}
    project_id = project.get("id")
    mr_iid = object_attrs.get("iid")

    if project_id is None or mr_iid is None:
        logger.warning("skip: missing project.id or object_attributes.iid in payload")
        return {"status": "skipped", "reason": "missing project_id or mr_iid"}

    logger.info("dispatch: project_id=%s mr_iid=%s", project_id, mr_iid)
    asyncio.create_task(_run_review(int(project_id), int(mr_iid)))

    return {"status": "review started"}


async def _run_review(project_id: int, mr_iid: int) -> None:
    """review_runner.py를 별도 프로세스로 실행. 실패해도 서버는 계속 동작."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python",
            "review_runner.py",
            str(project_id),
            str(mr_iid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "review_runner failed (project=%s mr=%s rc=%s)\nstdout: %s\nstderr: %s",
                project_id,
                mr_iid,
                proc.returncode,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )
        else:
            logger.info("review_runner ok (project=%s mr=%s)", project_id, mr_iid)
    except Exception:
        logger.exception("review_runner crashed (project=%s mr=%s)", project_id, mr_iid)
