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


def test_no_unexpected_agent_files():
    names = {p.stem for p in (CONFIG / "agents").glob("*.md")}
    assert names == set(REVIEW_AGENTS), (
        f"claude-config/agents/의 실제 파일 {sorted(names)}이 REVIEW_AGENTS와 "
        "다르다 — 새 에이전트 파일은 REVIEW_AGENTS에도 추가해야 tools 좁히기 "
        "테스트가 커버한다"
    )


def test_review_pr_command_tools_are_narrowed():
    fm = _frontmatter(CONFIG / "commands" / "review-pr.md")
    tools = fm.get("allowed-tools")
    assert tools is not None, "review-pr.md: allowed-tools 미지정 = 전체 도구 허용"
    assert "Bash" not in tools, (
        "review-pr.md가 좁혀지지 않은 bare Bash를 선언한다 — Bash(git:*)처럼 "
        "scope된 형태여야 한다(CLAUDE_CODE_OAUTH_TOKEN 유출 표면 확대)"
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


def test_claude_invocation_pins_model():
    """CLI 버전 핀과 짝을 이루는 모델 핀. 미지정이면 계정 기본 모델을 따라가
    재배포·계정 정책 변경 한 번에 리뷰 톤이 조용히 바뀐다(2026-09-02 실제 발생)."""
    source = (ROOT / "review_runner.py").read_text(encoding="utf-8")
    assert re.search(r'^CLAUDE_MODEL = "[^"]+"', source, re.M), (
        "CLAUDE_MODEL 상수가 없다 — 모델이 고정되지 않으면 계정 기본값을 따라간다"
    )
    assert '"--model",\n                CLAUDE_MODEL,' in source, (
        "claude argv에 --model이 없다 — 상수만 있고 CLI에 전달되지 않는다"
    )


def test_dockerfile_bakes_claude_config():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"^COPY claude-config/ /opt/reviewer-claude/", dockerfile, re.M
    ), "claude-config/를 이미지에 COPY하지 않는다"
    assert re.search(
        r"^ENV CLAUDE_CONFIG_DIR=/opt/reviewer-claude", dockerfile, re.M
    ), "CLAUDE_CONFIG_DIR을 이미지에서 설정하지 않는다"


def _compose_service() -> dict:
    compose = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    return compose["services"]["ai-reviewer"]


def test_compose_has_no_host_mounts():
    service = _compose_service()
    assert "volumes" not in service, (
        "호스트 마운트가 부활했다 — config는 이미지에 굽는다(claude-config/). "
        "호스트 절대경로·심링크·단일 파일 마운트 의존이 리뷰 중단 사고 "
        "두 건의 원인이었다"
    )


def test_compose_does_not_override_config_dir():
    """CLAUDE_CONFIG_DIR은 Dockerfile ENV가 담당한다.

    compose가 중복 지정하면 이미지에 구운 경로와 어긋나 config를 못 읽는다.
    compose의 environment는 map/list 두 형태가 모두 유효하므로 둘 다 본다.
    """
    environment = _compose_service().get("environment") or {}
    if isinstance(environment, dict):
        keys = set(environment)
    else:
        keys = {entry.split("=", 1)[0] for entry in environment}
    assert "CLAUDE_CONFIG_DIR" not in keys, (
        "compose가 CLAUDE_CONFIG_DIR을 덮어쓴다 — Dockerfile ENV만 쓸 것"
    )


# 코드가 이름으로 읽지 않고 서브프로세스 상속으로만 소비하는 변수.
# review_runner는 os.environ를 그대로 claude에 물려주므로(비밀 접미사만
# strip) OAuth 토큰은 소스에 이름이 나타나지 않는다 — grep으로는 안 잡힌다.
SUBPROCESS_ONLY_ENV = {"CLAUDE_CODE_OAUTH_TOKEN"}

_ENV_READ_RE = re.compile(
    r'(?:os\.environ(?:\.get)?\(\s*|_required_env\(\s*)"([A-Z_]+)"'
)


def _env_read_by_code() -> set[str]:
    names: set[str] = set()
    for module in ROOT.glob("*.py"):
        names |= set(_ENV_READ_RE.findall(module.read_text(encoding="utf-8")))
    return names


def _env_in_example() -> set[str]:
    names: set[str] = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Z_]+)=", line.strip())
        if match:
            names.add(match.group(1))
    return names


def test_env_example_has_no_dead_vars():
    dead = _env_in_example() - _env_read_by_code() - SUBPROCESS_ONLY_ENV
    assert not dead, (
        f".env.example의 죽은 변수: {sorted(dead)} — 안 쓰는 시크릿을 "
        "새 호스트/credential로 옮기지 않도록 정리할 것"
    )


def test_env_example_documents_all_read_vars():
    undocumented = _env_read_by_code() - _env_in_example()
    assert not undocumented, (
        f".env.example에 없는 변수: {sorted(undocumented)} — 배포 credential에 "
        "빠지면 컨테이너가 부팅에 실패하거나 기능이 조용히 사라진다"
    )


def test_jenkinsfile_uses_fixed_project_name():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    assert "docker compose -p gitlab-auto-review " in jenkinsfile, (
        "고정 프로젝트명이 없다 — Jenkins 워크스페이스 디렉토리명이 바뀌면 "
        "컨테이너가 중복 생성된다"
    )


def test_jenkinsfile_loads_env_from_credential_and_cleans_up():
    jenkinsfile = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    assert "credentialsId: 'gitlab-auto-review-env'" in jenkinsfile, (
        ".env를 Jenkins credential에서 가져오지 않는다"
    )
    assert "rm -f .env" in jenkinsfile, (
        "빌드 후 .env 정리가 없다 — 시크릿이 Jenkins 워크스페이스에 남는다"
    )
