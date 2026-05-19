"""GitLab MR 자동 리뷰 실행기.

webhook_server.py에서 자식 프로세스로 호출된다.

1. GitLab API로 MR 메타데이터 + diff 수집
2. Claude Code CLI의 `/review-pr` 슬래시 커맨드로 리뷰 생성
3. 결과를 MR 노트(코멘트)로 게시
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("review_runner")

GITLAB_URL = os.environ["GITLAB_URL"].rstrip("/")
PRIVATE_TOKEN = os.environ["GITLAB_TOKEN"]

# diff 토큰 폭주 방지용 한도
MAX_FILES = 10
MAX_DIFF_CHARS_PER_FILE = 2000
CLAUDE_TIMEOUT_SEC = 120


def get_mr_diff(project_id: int, mr_iid: int) -> str:
    headers = {"PRIVATE-TOKEN": PRIVATE_TOKEN}
    with httpx.Client(timeout=30.0) as client:
        mr_resp = client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
            headers=headers,
        )
        mr_resp.raise_for_status()
        mr = mr_resp.json()

        diffs_resp = client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/diffs",
            headers=headers,
        )
        diffs_resp.raise_for_status()
        diffs = diffs_resp.json()

    parts: list[str] = [
        f"MR 제목: {mr.get('title', '(제목 없음)')}",
        f"설명: {mr.get('description') or '없음'}",
        "",
    ]
    for d in diffs[:MAX_FILES]:
        new_path = d.get("new_path") or d.get("old_path") or "(unknown)"
        raw_diff = d.get("diff") or ""
        parts.append(f"### {new_path}")
        parts.append("```diff")
        parts.append(raw_diff[:MAX_DIFF_CHARS_PER_FILE])
        parts.append("```")
        parts.append("")

    if len(diffs) > MAX_FILES:
        parts.append(f"_({len(diffs) - MAX_FILES}개 파일은 길이 제한으로 생략됨)_")

    return "\n".join(parts)


def run_claude_review(diff_text: str) -> str:
    # /review-pr 슬래시 커맨드를 사용. 첫 줄은 반드시 슬래시 커맨드여야 한다.
    prompt = (
        "/review-pr\n"
        "\n"
        "아래는 GitLab Merge Request의 diff 정보야. PR 리뷰하듯이 분석해줘.\n"
        "출력은 한국어 마크다운으로.\n"
        "\n"
        f"{diff_text}\n"
    )
    result = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude 실행 실패 (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def post_comment(project_id: int, mr_iid: int, review: str) -> None:
    body = f"🤖 **AI 자동 코드 리뷰**\n\n{review}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes",
            headers={"PRIVATE-TOKEN": PRIVATE_TOKEN},
            json={"body": body},
        )
        resp.raise_for_status()


def main(project_id: int, mr_iid: int) -> int:
    logger.info("MR !%s 리뷰 시작 (project=%s)", mr_iid, project_id)
    diff = get_mr_diff(project_id, mr_iid)
    review = run_claude_review(diff)
    post_comment(project_id, mr_iid, review)
    logger.info("MR !%s 리뷰 게시 완료", mr_iid)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python review_runner.py <project_id> <mr_iid>", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main(int(sys.argv[1]), int(sys.argv[2])))
    except Exception:
        logger.exception("review_runner 실패")
        sys.exit(1)
