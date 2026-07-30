"""배포 설정 불변식 테스트.

이 레포는 리뷰 동작에 필요한 자산(슬래시 커맨드·서브에이전트·훅)을
claude-config/ 에 vendoring해 이미지에 굽는다. 예전에는 호스트
~/.claude 를 절대경로로 마운트해서 썼는데, 그 구조로 리뷰가 두 번
멈췄다 — 커맨드 파일 유실로 `Unknown command: /review-pr`, 훅 스크립트
단일 파일 마운트 dangling으로 컨테이너 안 전 Bash 차단.

그래서 아래 불변식은 문서 주의사항이 아니라 테스트로 고정한다.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "claude-config"

# /review-pr이 Task로 띄우는 서브에이전트. 하나라도 없으면 리뷰가
# 에러 없이 단일 패스로 붕괴한다(품질 저하로만 드러나 알아채기 어렵다).
REVIEW_AGENTS = (
    "code-reviewer",
    "comment-analyzer",
    "pr-test-analyzer",
    "silent-failure-hunter",
    "type-design-analyzer",
    "code-simplifier",
)

# 서브에이전트에 허용되는 도구의 전체 집합.
# 부모 프로세스의 --allowed-tools 는 Task로 띄운 서브에이전트에
# 전파되지 않는다. 따라서 각 정의에서 따로 좁혀야 하고, 이 좁히기가
# CLAUDE_CODE_OAUTH_TOKEN(claude 인증에 필요해 strip 불가)을
# prompt injection 유출로부터 지키는 실질 방어선이다.
ALLOWED_AGENT_TOOLS = {"Read", "Grep", "Glob", "Bash(git:*)"}


def _frontmatter(path: Path) -> dict:
    """마크다운 앞머리의 YAML frontmatter를 dict로 돌려준다.

    에이전트 파일들은 인용 스타일이 섞여 있다(`["Read", ...]` 와
    `[Read, ...]`). 정규식으로 긁으면 한쪽을 놓치므로 YAML로 파싱한다.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        pytest.fail(f"{path.name}: YAML frontmatter가 없다")
    _, front, _ = text.split("---\n", 2)
    return yaml.safe_load(front)


@pytest.mark.parametrize("name", REVIEW_AGENTS)
def test_review_agent_exists(name):
    path = CONFIG / "agents" / f"{name}.md"
    assert path.is_file(), (
        f"{name}.md 없음 — /review-pr의 서브에이전트가 조용히 증발해 "
        "리뷰가 단일 패스로 붕괴한다"
    )


@pytest.mark.parametrize("name", REVIEW_AGENTS)
def test_review_agent_tools_are_narrowed(name):
    fm = _frontmatter(CONFIG / "agents" / f"{name}.md")
    tools = fm.get("tools")
    assert tools is not None, f"{name}: tools 미지정 = 전체 도구 허용"
    extra = set(tools) - ALLOWED_AGENT_TOOLS
    assert not extra, (
        f"{name}: 허용되지 않은 도구 {sorted(extra)} — 부모 --allowed-tools는 "
        "서브에이전트에 전파되지 않으므로 여기서 좁혀야 한다"
    )


def test_review_pr_command_exists_and_names_all_agents():
    cmd = CONFIG / "commands" / "review-pr.md"
    assert (
        cmd.is_file()
    ), "review-pr.md 없음 — claude가 'Unknown command: /review-pr'로 실패한다"
    body = cmd.read_text(encoding="utf-8")
    missing = [name for name in REVIEW_AGENTS if name not in body]
    assert not missing, f"review-pr.md가 언급하지 않는 에이전트: {missing}"


def test_env_dump_guard_is_vendored():
    guard = CONFIG / "scrub" / "block_env_dump.py"
    assert guard.is_file(), (
        "env 덤프 차단 훅 없음 — 리뷰 출력은 팀이 보는 MR 코멘트로 게시되므로 "
        "injection이 env를 덤프하면 OAuth 토큰이 공개된다"
    )


def test_settings_hook_points_inside_image():
    settings = json.loads((CONFIG / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for matcher in settings["hooks"]["PreToolUse"]
        for hook in matcher["hooks"]
    ]
    assert commands, "PreToolUse 훅이 비어 있다"
    for command in commands:
        assert "/opt/reviewer-claude/" in command, (
            f"훅이 이미지 밖 경로를 참조한다: {command!r} — 호스트 경로 의존은 "
            "마운트 dangling으로 전 Bash 차단 사고를 재발시킨다"
        )


def test_dockerfile_pins_claude_version():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"npm install -g @anthropic-ai/claude-code@\d+\.\d+\.\d+", dockerfile
    ), (
        "claude 버전이 고정되지 않았다 — 재배포마다 최신이 들어와 "
        "배포가 조용한 업그레이드가 된다"
    )


def test_dockerfile_bakes_claude_config():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"^COPY claude-config/ /opt/reviewer-claude/", dockerfile, re.M
    ), "claude-config/를 이미지에 COPY하지 않는다"
    assert re.search(
        r"^ENV CLAUDE_CONFIG_DIR=/opt/reviewer-claude", dockerfile, re.M
    ), "CLAUDE_CONFIG_DIR을 이미지에서 설정하지 않는다"
