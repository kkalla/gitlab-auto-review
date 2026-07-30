# 리뷰어 196-server 이관 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 리뷰 동작에 필요한 자산을 레포 안으로 들여와 이미지에 굽고, 컨테이너를 macOS 로컬 podman에서 사내 196-server(Docker + Jenkins)로 완전 이관한다.

**Architecture:** 파이프라인 로직(Slack 트리거 3종 / 증분 리뷰 / 실패 알림)은 손대지 않는다. 바뀌는 것은 **config 조달 경로**뿐이다 — 지금은 호스트 `~/.claude`·`~/.claude-work`를 절대경로로 bind-mount하지만, 앞으로는 `claude-config/`를 레포에 vendoring해 `COPY`로 이미지에 굽고 `CLAUDE_CONFIG_DIR`로 가리킨다. 결과적으로 컨테이너 마운트가 0개가 되고, ags-watchtower와 동일한 Jenkins 파이프라인으로 배포된다.

**Tech Stack:** Python 3.11 (FastAPI, httpx, slack-bolt), Claude Code CLI 2.1.220, Docker Compose, Jenkins (declarative pipeline), pytest 8.3.3, PyYAML

**설계 문서:** `docs/superpowers/specs/2026-07-30-server-migration-design.md` — 결정 근거(D1~D4)와 타당성 실측은 여기에 있다.

## Global Constraints

- **claude CLI 버전은 `2.1.220`으로 핀** — 현행 `npm install -g @anthropic-ai/claude-code`(미고정)는 재배포를 조용한 업그레이드로 만든다.
- **컨테이너 내부 config 경로는 `/opt/reviewer-claude`** — 모든 파일·훅 커맨드가 이 경로를 쓴다. 호스트 절대경로(`/Users/max/...`)를 참조하는 것이 하나라도 남으면 안 된다.
- **서브에이전트 6개의 도구는 정확히 `Read`, `Grep`, `Glob`, `Bash(git:*)`** — 부모의 `--allowed-tools`는 Task로 띄운 서브에이전트에 전파되지 않으므로, 이 좁히기가 `CLAUDE_CODE_OAUTH_TOKEN`을 injection 유출로부터 지키는 실질 방어선이다. `Write`/`Edit`/범용 `Bash` 금지.
- **`/review-pr`은 프롬프트 첫 줄에 와야 하고, `claude-config/commands/review-pr.md`가 실재해야 한다** — 없으면 `Unknown command: /review-pr`.
- **Docker compose 프로젝트명은 `gitlab-auto-review`로 고정** — Jenkins 워크스페이스 디렉토리명 변화로 컨테이너가 중복 생성되는 것을 막는다.
- **Jenkins credential id는 `gitlab-auto-review-env`** (Secret file).
- **커밋 메시지는 한국어 + Conventional Commits 프리픽스**(`feat`/`fix`/`docs`/`test`/`chore`/`refactor`) — 기존 git log와 일치시킨다.
- **테스트는 `make test`로 돈다** (격리 `.venv`). 런타임 `requirements.txt`에는 pytest를 넣지 않는다.
- **설정 파일을 정규식으로 검증할 때는 주석 산문까지 매치된다** — 앵커에 `docker compose -p` 같은 커맨드 형태를 포함시키거나, 가능하면 YAML/JSON 파싱을 쓴다.

## File Structure

| 파일 | 책임 | 상태 |
|---|---|---|
| `claude-config/commands/review-pr.md` | `/review-pr` 슬래시 커맨드 정의 | 신규 (호스트에서 복사) |
| `claude-config/agents/*.md` (6개) | Task로 띄우는 서브에이전트 정의 + 도구 좁히기 | 신규 (호스트에서 복사) |
| `claude-config/settings.json` | 리뷰어 전용 최소 설정 — env 덤프 차단 훅만 | 신규 (직접 작성) |
| `claude-config/scrub/block_env_dump.py` | `.env`/`printenv` 출력 차단 PreToolUse 훅 | 신규 (개인 레포에서 복사) |
| `tests/test_deploy_config.py` | 위 자산의 존재·권한·경로를 불변식으로 고정 | 신규 |
| `Dockerfile` | claude 버전 핀, `claude-config/` COPY, `CLAUDE_CONFIG_DIR` | 수정 |
| `docker-compose.yml` | `volumes:` 전체 삭제, `CLAUDE_CONFIG_DIR` env 삭제 | 수정 |
| `Jenkinsfile` | 태그 푸시 → 폴링 → 빌드/배포 → Verify | 신규 |
| `requirements-dev.txt` | 테스트가 직접 쓰는 `PyYAML` 명시 | 수정 |
| `CLAUDE.md`, `README.md` | macOS/podman/마운트 전제 서술 재작성 | 수정 |

`review_runner.py`·`slack_bot.py`·`webhook_server.py`·`slack_notifier.py`는 **변경 없다.** `CLAUDE_CONFIG_DIR`은 Dockerfile `ENV`로 들어가고 `claude_env`가 `os.environ`을 상속하므로 코드가 개입할 필요가 없다(시크릿 접미사가 아니라 strip되지도 않는다).

---

### Task 1: `claude-config/` vendoring + 불변식 테스트

리뷰 자산을 레포로 들여오고, 동시에 그 자산이 사라지거나 권한이 넓어지는 것을 막는 테스트를 세운다. CLAUDE.md에 *"새 배포는 호스트에서 이 좁히기를 다시 적용해야 한다"*고 적혀 있던 부채를 CI 불변식으로 승격하는 단계다.

**Files:**
- Create: `claude-config/commands/review-pr.md` (호스트 `~/.claude/commands/review-pr.md` 복사)
- Create: `claude-config/agents/{code-reviewer,comment-analyzer,pr-test-analyzer,silent-failure-hunter,type-design-analyzer,code-simplifier}.md` (호스트 `~/.claude/agents/` 복사)
- Create: `claude-config/scrub/block_env_dump.py` (`~/00_Projects/95_personal-memory/scrub/block_env_dump.py` 복사)
- Create: `claude-config/settings.json`
- Create: `tests/test_deploy_config.py`
- Modify: `requirements-dev.txt`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: `tests/test_deploy_config.py`의 모듈 상수 `ROOT: Path`(레포 루트), `CONFIG: Path`(= `ROOT / "claude-config"`), `REVIEW_AGENTS: tuple[str, ...]`(에이전트 6개 이름). Task 2·3·4·5가 같은 파일에 테스트를 덧붙이며 이 상수들을 재사용한다.

- [ ] **Step 1: `PyYAML`을 dev 의존성에 명시**

테스트가 에이전트 frontmatter를 YAML로 파싱한다. 현재 `pyyaml`은 `uvicorn[standard]`의 전이 의존으로 우연히 깔려 있을 뿐이므로 직접 의존을 명시한다.

`requirements-dev.txt`를 아래로 만든다:

```
# 개발/테스트 전용 의존성. 런타임 Docker 이미지에는 포함되지 않는다
# (Dockerfile은 requirements.txt만 COPY/install).
-r requirements.txt
pytest==8.3.3
# tests/test_deploy_config.py가 에이전트 frontmatter를 파싱한다.
# uvicorn[standard]의 전이 의존으로도 들어오지만, 직접 쓰므로 명시한다.
PyYAML==6.0.3
```

`6.0.3`은 현재 `.venv`에 이미 설치된 버전이다 — 낮은 버전을 핀하면 불필요한 다운그레이드가 난다.

- [ ] **Step 2: 실패하는 테스트를 작성**

`tests/test_deploy_config.py`를 아래 내용으로 만든다.

```python
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
    assert cmd.is_file(), (
        "review-pr.md 없음 — claude가 'Unknown command: /review-pr'로 실패한다"
    )
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
```

- [ ] **Step 3: 테스트를 돌려 실패를 확인**

Run: `make test`
Expected: FAIL — `claude-config/`가 아직 없으므로 `test_review_agent_exists`(6건), `test_review_pr_command_exists_and_names_all_agents`, `test_env_dump_guard_is_vendored`가 `assert ... is_file()`에서 실패하고, `test_review_agent_tools_are_narrowed`(6건)과 `test_settings_hook_points_inside_image`는 `FileNotFoundError`로 에러가 난다.

- [ ] **Step 4: 자산을 레포로 복사**

```bash
mkdir -p claude-config/commands claude-config/agents claude-config/scrub
cp ~/.claude/commands/review-pr.md claude-config/commands/
for a in code-reviewer comment-analyzer pr-test-analyzer \
         silent-failure-hunter type-design-analyzer code-simplifier; do
  cp ~/.claude/agents/$a.md claude-config/agents/
done
cp ~/00_Projects/95_personal-memory/scrub/block_env_dump.py claude-config/scrub/
```

복사된 에이전트 6개는 모두 `tools`가 `Read, Grep, Glob, Bash(git:*)`로 좁혀진 상태여야 한다(2026-07-30 확인). 만약 넓은 것이 있으면 **복사본을 고친다** — 호스트 원본은 개인 대화 세션이 쓰므로 건드리지 않는다.

- [ ] **Step 5: `claude-config/settings.json` 작성**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /opt/reviewer-claude/scrub/block_env_dump.py"
          }
        ]
      }
    ]
  }
}
```

개인 훅(memory-tick `SessionStart`/`Stop`)은 넣지 않는다 — 리뷰어에 필요 없고, 호스트 경로 의존을 다시 끌어들인다. `python3`는 bare 커맨드로 둔다(이미지의 PATH가 3.11을 가리키고, 이 스크립트는 표준 라이브러리만 쓴다).

- [ ] **Step 6: 테스트를 돌려 통과를 확인**

Run: `make test`
Expected: PASS (신규 15건 + 기존 테스트 전부)

- [ ] **Step 7: 좁히기 테스트가 실제로 물리는지 확인 (뮤테이션)**

테스트가 공허하지 않은지 일부러 깨뜨려 본다.

```bash
sed -i '' 's/tools: \["Read", "Grep", "Glob", "Bash(git:\*)"\]/tools: ["Read", "Grep", "Glob", "Bash", "Write"]/' claude-config/agents/code-reviewer.md
make test
```
Expected: FAIL — `test_review_agent_tools_are_narrowed[code-reviewer]`가 `허용되지 않은 도구 ['Bash', 'Write']`로 실패.

되돌린다:
```bash
git checkout claude-config/agents/code-reviewer.md 2>/dev/null || \
  cp ~/.claude/agents/code-reviewer.md claude-config/agents/
make test
```
Expected: PASS

- [ ] **Step 8: 커밋**

```bash
git add claude-config tests/test_deploy_config.py requirements-dev.txt
git commit -m "feat: 리뷰 자산을 claude-config/로 vendoring + 불변식 테스트

/review-pr 커맨드와 서브에이전트 6개, env 덤프 차단 훅을 레포 안으로
들여온다. 지금까지는 호스트 ~/.claude를 절대경로로 마운트해 썼는데,
그 구조로 리뷰가 두 번 멈췄다(커맨드 파일 유실, 훅 단일 파일 마운트
dangling).

서브에이전트 도구 좁히기는 CLAUDE.md의 주의사항으로만 있었다 —
부모 --allowed-tools가 서브에이전트에 전파되지 않으므로 이 좁히기가
strip 불가한 CLAUDE_CODE_OAUTH_TOKEN의 실질 방어선이다. 테스트로
승격해 조용한 권한 확대를 막는다."
```

---

### Task 2: Dockerfile — claude 버전 핀 + config 굽기

**Files:**
- Modify: `Dockerfile:18` (npm install 행), 그 아래 `COPY`/`ENV` 추가
- Modify: `tests/test_deploy_config.py` (테스트 2건 추가)

**Interfaces:**
- Consumes: Task 1의 `claude-config/` 디렉토리, `tests/test_deploy_config.py`의 `ROOT` 상수
- Produces: 이미지 안 `/opt/reviewer-claude/` 경로와 `CLAUDE_CONFIG_DIR` 환경변수. Task 3이 compose에서 같은 env를 **제거**하는 근거가 된다.

- [ ] **Step 1: 실패하는 테스트를 작성**

`tests/test_deploy_config.py` 끝에 덧붙인다:

```python
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
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인**

Run: `make test`
Expected: FAIL — `test_dockerfile_pins_claude_version`("버전이 고정되지 않았다"), `test_dockerfile_bakes_claude_config`("COPY하지 않는다") 2건.

- [ ] **Step 3: Dockerfile 수정**

`Dockerfile:18`을 바꾼다:

```dockerfile
# 버전 핀 필수 — 미고정이면 빌드마다 최신이 들어와 재배포가 곧 조용한
# claude 업그레이드가 된다. 2.1.220은 리뷰가 실제로 돌던 버전이다.
# 올릴 때는 이 값을 의도적으로 커밋한다.
RUN npm install -g @anthropic-ai/claude-code@2.1.220
```

`COPY webhook_server.py …` 행 **다음에** 추가한다:

```dockerfile
# 리뷰 전용 claude config를 이미지에 굽는다 — 호스트 마운트 없음.
#
# 예전에는 호스트 ~/.claude·~/.claude-work를 절대경로로 마운트했는데,
# 그 구조가 두 번의 리뷰 중단을 만들었다(커맨드 파일 유실 →
# `Unknown command: /review-pr`, 훅 스크립트 단일 파일 bind-mount의
# inode 교체 dangling → 컨테이너 안 전 Bash 차단). 이제 리뷰에 필요한
# 자산은 전부 레포 안에 있고 tests/test_deploy_config.py가 지킨다.
#
# 이 디렉토리는 쓰기 가능해야 한다 — claude가 .claude.json,
# shell-snapshots/, sessions/를 여기에 쓴다. 이미지 레이어 위 경로라
# 쓰기가 되고, 컨테이너 재생성 시 초기화된다. 보존할 상태는 없다
# (증분 리뷰 기준점은 MR 코멘트의 reviewed-sha 마커가 보관한다).
COPY claude-config/ /opt/reviewer-claude/
ENV CLAUDE_CONFIG_DIR=/opt/reviewer-claude
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인**

Run: `make test`
Expected: PASS

- [ ] **Step 5: 이미지가 실제로 빌드되고 config가 보이는지 확인**

```bash
podman build -t reviewer-selfcontained:test .
podman run --rm reviewer-selfcontained:test sh -c \
  'claude --version && ls /opt/reviewer-claude/commands /opt/reviewer-claude/agents'
```
Expected: `2.1.220 (Claude Code)` 출력, `review-pr.md`와 에이전트 6개가 나열됨.

- [ ] **Step 6: 커밋**

```bash
git add Dockerfile tests/test_deploy_config.py
git commit -m "feat: claude 버전 핀 + claude-config를 이미지에 굽기

npm install이 미고정이라 재배포가 조용한 claude 업그레이드였다.
실제로 돌던 2.1.220으로 고정한다.

claude-config/를 /opt/reviewer-claude로 COPY하고 CLAUDE_CONFIG_DIR을
이미지에서 설정한다 — 호스트 마운트 의존이 사라진다."
```

---

### Task 3: docker-compose.yml — 호스트 마운트 제거

**Files:**
- Modify: `docker-compose.yml` (`volumes:` 블록 전체 및 `environment.CLAUDE_CONFIG_DIR` 삭제)
- Modify: `tests/test_deploy_config.py` (테스트 1건 추가)

**Interfaces:**
- Consumes: Task 2의 Dockerfile `ENV CLAUDE_CONFIG_DIR`(compose에서 지워도 컨테이너에 남는 근거), `tests/test_deploy_config.py`의 `ROOT`
- Produces: 마운트 0개인 compose. Task 5의 Jenkinsfile이 이 파일을 `docker compose -p gitlab-auto-review up -d --build`로 띄운다.

- [ ] **Step 1: 실패하는 테스트를 작성**

`tests/test_deploy_config.py` 끝에 덧붙인다. 정규식이 아니라 **YAML 파싱**을 쓴다 — 이 파일은 주석이 많고, 정규식은 주석 산문까지 매치된다.

```python
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
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인**

Run: `make test`
Expected: FAIL 2건 — `test_compose_has_no_host_mounts`가 "호스트 마운트가 부활했다"(현재 `volumes:` 3항목 존재), `test_compose_does_not_override_config_dir`가 "compose가 CLAUDE_CONFIG_DIR을 덮어쓴다"(현재 `environment: {CLAUDE_CONFIG_DIR: /Users/max/.claude-work}` 존재).

- [ ] **Step 3: compose 수정**

`docker-compose.yml`의 `environment:` 블록(주석 포함)과 `volumes:` 블록(주석 포함)을 **통째로 삭제**한다. macOS Keychain / podman virtiofs / 호스트 절대경로 / 심링크 dangling을 설명하는 주석 약 40줄이 전부 무효가 되므로 함께 지운다.

결과는 아래와 같아야 한다:

```yaml
services:
  ai-reviewer:
    build: .
    container_name: gitlab-ai-reviewer
    # PID 1을 tini로 교체해 reparent된 좀비를 거둔다.
    # 봇/uvicorn(=원래 PID 1)은 reaper가 아니라, claude가 CLAUDE_TIMEOUT_SEC로
    # SIGKILL될 때 그 자식 git 프로세스들이 orphan→PID 1로 reparent되며 좀비로
    # 쌓였다(운영 중 timeout 다발 시 수십 개 누적 확인). tini는 이를 자동 reap.
    init: true
    # Slack 봇 모드(기본 CMD=slack_bot.py)는 아웃바운드 WebSocket만 쓰므로 공개
    # inbound 포트가 필요 없다. webhook 모드로 되돌릴 때만 아래 command + ports를 켠다:
    #   command: ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
    #   ports:
    #     - "8080:8080"
    #   (그 경우 .env에 WEBHOOK_SECRET도 다시 넣을 것 — Slack 봇 모드에선 안 쓴다.)
    command: ["python", "slack_bot.py"]
    # 마운트 없음 — 리뷰용 claude config는 이미지에 구워져 있다(claude-config/,
    # Dockerfile의 COPY + ENV CLAUDE_CONFIG_DIR). 호스트 경로에 의존하지 않으므로
    # 어느 호스트에서든 동일하게 뜬다.
    env_file:
      - .env
    restart: unless-stopped
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인**

Run: `make test`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add docker-compose.yml tests/test_deploy_config.py
git commit -m "refactor: 호스트 마운트 제거 — config는 이미지에 구움

volumes 3항목과 CLAUDE_CONFIG_DIR env를 삭제한다. macOS Keychain·
podman virtiofs·호스트 절대경로·심링크 dangling을 설명하던 주석
약 40줄도 함께 무효가 되어 정리했다.

마운트 0개가 되어 ags-watchtower와 같은 형태가 된다."
```

---

### Task 4: `.env.example` ↔ 코드 env 양방향 대조 테스트

죽은 변수(코드가 안 읽는데 example에 있음)와 미문서화 변수(코드가 읽는데 example에 없음)를 둘 다 잡는다. 이 테스트는 **작성 시점에 이미 통과한다** — 앞으로의 드리프트를 막는 회귀 가드이므로, 뮤테이션으로 RED를 반드시 확인한다.

**Files:**
- Modify: `tests/test_deploy_config.py` (테스트 2건 + 헬퍼 추가)

**Interfaces:**
- Consumes: `tests/test_deploy_config.py`의 `ROOT`, `re`
- Produces: 없음 (검증 전용)

- [ ] **Step 1: 테스트를 작성**

`tests/test_deploy_config.py` 끝에 덧붙인다:

```python
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
```

- [ ] **Step 2: 통과를 확인 (현재 상태가 깨끗함)**

Run: `make test`
Expected: PASS — 현재 `.env.example`은 코드가 읽는 9개(`GITLAB_URL`, `GITLAB_TOKEN`, `WEBHOOK_SECRET`, `REVIEWER_USERNAME`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `REVIEWER_SLACK_ID`, `POLL_INTERVAL_SEC`, `LOG_LEVEL`)와 `CLAUDE_CODE_OAUTH_TOKEN`만 담고 있다.

- [ ] **Step 3: 양방향 모두 뮤테이션으로 RED를 확인**

죽은 변수 방향:
```bash
printf 'NOTION_TOKEN=dummy\n' >> .env.example
make test
```
Expected: FAIL — `test_env_example_has_no_dead_vars`가 `죽은 변수: ['NOTION_TOKEN']`으로 실패.

```bash
git checkout .env.example
```

미문서화 방향:
```bash
sed -i '' '/^REVIEWER_SLACK_ID=/d' .env.example
make test
```
Expected: FAIL — `test_env_example_documents_all_read_vars`가 `없는 변수: ['REVIEWER_SLACK_ID']`로 실패.

```bash
git checkout .env.example
make test
```
Expected: PASS

- [ ] **Step 4: 커밋**

```bash
git add tests/test_deploy_config.py
git commit -m "test: .env.example과 코드가 읽는 env를 양방향 대조

죽은 변수(라이브 .env에 남은 NOTION_* 같은 잔재가 example로 새는 것)와
누락(코드가 읽는데 example에 없어 배포 credential에서 빠지는 것)을
둘 다 잡는다.

CLAUDE_CODE_OAUTH_TOKEN은 예외 처리 — review_runner가 os.environ을
claude에 그대로 물려주므로 소스에 이름이 나타나지 않는다."
```

---

### Task 5: Jenkinsfile

ags-watchtower의 파이프라인을 이 레포에 맞춰 가져온다. Jenkins가 호스트 docker.sock을 쓰므로 파이프라인 안의 `docker compose up`이 곧 196-server 배포다.

**Files:**
- Create: `Jenkinsfile`
- Modify: `tests/test_deploy_config.py` (테스트 2건 추가)

**Interfaces:**
- Consumes: Task 3의 `docker-compose.yml`(서비스명 `ai-reviewer`), `tests/test_deploy_config.py`의 `ROOT`
- Produces: Jenkins job이 소비하는 파이프라인 정의. Task 8의 컷오버가 이 파일을 전제로 job을 만든다.

- [ ] **Step 1: 실패하는 테스트를 작성**

`tests/test_deploy_config.py` 끝에 덧붙인다. 앵커에 `docker compose -p`라는 **커맨드 형태**를 포함시킨다 — 프로젝트명만 찾으면 주석 산문에 걸린다.

```python
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
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인**

Run: `make test`
Expected: FAIL — `Jenkinsfile`이 없어 두 테스트 모두 `FileNotFoundError`.

- [ ] **Step 3: `Jenkinsfile` 작성**

```groovy
// 새 tag 푸시 시 196-server에 배포하는 파이프라인.
//
// 트리거는 폴링이다 — 사내망 Jenkins는 inbound가 막혀 있어 GitLab이 웹훅을
// 쏠 수 없으므로 Jenkins가 당겨온다. Job 설정에서 tag만 잡도록 해야 한다:
//   - Repository URL: https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review.git
//     (사내 GitLab. 이 레포는 origin=GitHub도 있지만 사내망 Jenkins가 공인망을
//      폴링할 이유가 없고 ags-watchtower와도 어긋난다.)
//   - Branch Specifier: refs/tags/*
//   - Advanced > Refspec: +refs/tags/*:refs/remotes/origin/tags/*
// (pollSCM 트리거는 아래 선언으로 첫 빌드 후 자동 등록된다.)
//
// Jenkins 컨테이너는 host docker.sock을 쓰므로 여기서의 compose up은 곧
// 호스트(196-server) 배포다. .env는 레포에 없다 — Secret file credential
// 'gitlab-auto-review-env'를 복사해 쓴다. 값 수정은 Jenkins UI에서 credential
// 재업로드 (Manage Jenkins > Credentials > gitlab-auto-review-env > Update).
// credential 변경은 pollSCM이 감지하지 못하므로 그때는 수동 빌드가 필요하다.
pipeline {
    agent any
    options {
        disableConcurrentBuilds()
        timestamps()
    }
    triggers {
        pollSCM('H/3 * * * *')
    }
    stages {
        stage('Deploy') {
            steps {
                // -p로 프로젝트명 고정 — 워크스페이스 디렉토리명에 따라
                // 컨테이너가 중복 생성되는 것을 막는다.
                withCredentials([file(credentialsId: 'gitlab-auto-review-env', variable: 'REVIEWER_ENV')]) {
                    sh '''
                        cp "$REVIEWER_ENV" .env
                        docker compose -p gitlab-auto-review up -d --build
                    '''
                }
            }
        }
        stage('Verify') {
            steps {
                // 부팅 직후 crash-loop(토큰 오류·필수 env 누락 등)를 걸러내는
                // 최소 검증. 이 레포는 부팅 시 GITLAB_URL 스킴과 필수 env를
                // 검증해 즉시 종료하므로 crash-loop이 정확히 잡힌다.
                //
                // 컨테이너명을 하드코딩하지 않고 compose에게 묻는다 — 하드코딩하면
                // 서비스가 늘 때 여기가 조용히 낡는다. 개수 대조는 -p 오타로
                // 0개를 보고 통과하는 공허한 성공을 막는다.
                //
                // RestartCount를 함께 보는 이유: 부팅 후 10초 동안은 크래시루프도
                // "running"으로 보인다(restart: unless-stopped가 즉시 되살리므로).
                // 재시작 1회면 실패로 본다 — 재배포는 항상 컨테이너를 재생성하므로
                // 정상 배포의 카운트는 0이다.
                sh '''
                    sleep 10
                    expected=$(docker compose -p gitlab-auto-review config --services | wc -l)
                    cids=$(docker compose -p gitlab-auto-review ps -aq)
                    actual=$(printf '%s' "$cids" | grep -c . || true)
                    if [ "$actual" -ne "$expected" ]; then
                        echo "FAIL: 컨테이너 $actual개 / compose 서비스 $expected개"
                        exit 1
                    fi
                    rc=0
                    for cid in $cids; do
                        name=$(docker inspect -f '{{.Name}}' "$cid")
                        state=$(docker inspect -f '{{.State.Status}}' "$cid")
                        restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
                        if [ "$state" != "running" ] || [ "$restarts" -gt 0 ]; then
                            echo "FAIL:$name state=$state restarts=$restarts"
                            docker logs --tail 30 "$cid" 2>&1 || true
                            rc=1
                        fi
                    done
                    exit $rc
                '''
            }
        }
    }
    post {
        always {
            // 시크릿이 워크스페이스에 남지 않게
            sh 'rm -f .env'
        }
        success {
            sh 'docker image prune -f'
        }
    }
}
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인**

Run: `make test`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add Jenkinsfile tests/test_deploy_config.py
git commit -m "feat: 196-server 배포 Jenkinsfile 추가

ags-watchtower와 동일한 패턴 — 태그 푸시 → pollSCM H/3 →
docker compose -p gitlab-auto-review up -d --build → Verify.
.env는 Secret file credential 'gitlab-auto-review-env'에서 온다.

Verify는 compose 서비스 개수 대조 + state/RestartCount로 부팅 직후
crash-loop을 잡는다(부팅 10초간은 크래시루프도 running으로 보인다)."
```

---

### Task 6: 문서 갱신

CLAUDE.md의 여러 절이 macOS Keychain·podman virtiofs·호스트 절대경로 마운트를 전제로 쓰여 있고, 이관 후에는 **사실이 아니게 된다.** 낡은 문서는 다음 사람(과 다음 세션의 나)을 잘못된 진단으로 보내므로 같은 브랜치에서 고친다.

**Files:**
- Modify: `CLAUDE.md` (Auth model 절, Claude invocation rules 절, Run/debug 절, Required env vars 절)
- Modify: `README.md` (환경변수 표에서 `WEBHOOK_SECRET` 필수 여부, 배포 절차)

**Interfaces:**
- Consumes: Task 1~5의 최종 상태
- Produces: 없음 (문서)

- [ ] **Step 1: `CLAUDE.md`의 Auth model 절 재작성**

현재 이 절은 "macOS Keychain을 컨테이너가 못 읽으므로 토큰 env var가 필요하다"와 "`~/.claude-work`·`~/.claude`를 호스트 절대경로에 마운트해야 한다"를 근거로 설명한다. 앞의 것은 **여전히 유효**(토큰은 계속 필요)하고, 뒤의 것은 **전부 무효**다.

다음 내용으로 바꾼다:
- `CLAUDE_CODE_OAUTH_TOKEN`은 계속 필수이며 `claude setup-token`으로 발급한다 (유지)
- 마운트는 없다. 리뷰용 config는 `claude-config/`가 이미지에 구워진다 (`COPY` + `ENV CLAUDE_CONFIG_DIR=/opt/reviewer-claude`)
- 호스트 절대경로·심링크·`:ro`/`rw`·podman virtiofs atomic-rename·`/seed` 트릭 서술 전부 삭제
- `claude setup-token`은 브라우저 OAuth라 서버에서 발급할 수 없다 → macOS에서 발급해 Jenkins credential로 옮긴다 (~1년 만료). `claude -p`가 `Not logged in`이면 1순위 용의자

- [ ] **Step 2: `CLAUDE.md`의 Claude invocation rules 절에서 서브에이전트 서술 수정**

*"**A new deployment must reapply this narrowing on its host** (it lives outside this repo)"* 문장을 삭제하고 다음으로 대체한다:

> 서브에이전트 6개는 `claude-config/agents/`에 vendoring되어 이미지에 구워지고, `tests/test_deploy_config.py`가 `Read`/`Grep`/`Glob`/`Bash(git:*)` 외의 도구를 거부한다. 호스트 설정에 의존하지 않으므로 새 배포에서 다시 좁힐 필요가 없다.

- [ ] **Step 3: `CLAUDE.md`의 Run/debug 절 갱신**

podman machine / `~/.claude-work` 마운트 전제 명령을 서버 배포 절차로 바꾼다:

```bash
# 배포 (196-server) — 태그 푸시 후 Jenkins가 3분 내 폴링해서 배포한다
git tag v1.x.y && git push gitlab v1.x.y
# 로그
ssh 196-server 'docker compose -p gitlab-auto-review logs -f ai-reviewer'
# 로컬 이미지 검증 (서버 손대기 전)
podman build -t reviewer-selfcontained:test . && \
  podman run --rm reviewer-selfcontained:test sh -c \
    'claude --version && ls /opt/reviewer-claude/commands /opt/reviewer-claude/agents'
```

- [ ] **Step 4: `CLAUDE.md`·`README.md`의 `WEBHOOK_SECRET` 서술 정정**

`WEBHOOK_SECRET`은 `webhook_server.py`만 읽고 `slack_bot.py`(기본 CMD)는 그 모듈을 import하지 않는다. README의 *"부팅 시 `WEBHOOK_SECRET` 길이/패턴 … 검증한다"*가 Slack 봇 모드에도 적용되는 것처럼 읽히므로 **webhook 모드 한정**임을 명시한다. 배포 credential에는 넣지 않는다.

- [ ] **Step 5: 테스트를 돌려 회귀가 없는지 확인**

Run: `make test`
Expected: PASS (문서 변경이므로 영향 없음. `.env.example`을 건드렸다면 Task 4의 대조 테스트가 잡는다.)

- [ ] **Step 6: 커밋**

```bash
git add CLAUDE.md README.md
git commit -m "docs: 이관 후 사실이 아닌 서술 정정

CLAUDE.md의 Auth model·invocation rules·Run/debug 절이 macOS Keychain·
podman virtiofs·호스트 절대경로 마운트를 전제로 쓰여 있었다. 마운트가
사라졌으므로 해당 근거 서술을 걷어내고 이미지 굽기로 대체한다.

'새 배포는 호스트에서 서브에이전트 좁히기를 다시 적용해야 한다'는
경고는 테스트가 대신하므로 삭제한다.

WEBHOOK_SECRET이 Slack 봇 모드에도 필수인 것처럼 읽히던 서술을
webhook 모드 한정으로 정정한다."
```

---

### Task 7: 로컬 E2E 게이트 (서버 손대기 전)

서버·Jenkins를 건드리기 전에, 새 self-contained 이미지가 **실제로 `/review-pr`을 인식하고 서브에이전트를 띄우는지** 확인한다. 이 게이트를 통과하지 못하면 컷오버로 넘어가지 않는다.

**Files:** 없음 (검증 전용)

**Interfaces:**
- Consumes: Task 2의 이미지, Task 1의 `claude-config/`, 로컬 `.env`
- Produces: 컷오버 진행 판단

- [ ] **Step 1: 이미지 빌드**

```bash
podman build -t reviewer-selfcontained:test .
```
Expected: 성공. 실패 시 nodesource/npm 도달 문제인지 로그로 확인한다.

- [ ] **Step 2: 슬래시 커맨드가 인식되는지 확인 (MR 부작용 없음)**

일회용 git 저장소를 만들어 claude를 직접 호출한다. GitLab에 아무것도 게시하지 않는다.

```bash
podman run --rm --env-file .env reviewer-selfcontained:test sh -c '
  git init -q /tmp/t && cd /tmp/t &&
  git config user.email t@example.com && git config user.name t &&
  printf "def f():\n    pass\n" > a.py && git add -A && git commit -qm init &&
  printf "def f():\n    return 1\n" > a.py && git add -A && git commit -qm change &&
  claude -p "/review-pr

이 저장소의 마지막 커밋을 짧게 리뷰해줘. 한국어로." \
    --allowed-tools "Read,Glob,Grep,Bash(git:*),Task" 2>&1 | head -60'
```

Expected: 리뷰 텍스트가 출력된다. 다음이 보이면 **실패**다:
- `Unknown command: /review-pr` → `claude-config/commands/review-pr.md`가 이미지에 없거나 `CLAUDE_CONFIG_DIR`이 안 걸렸다
- `Not logged in` → `.env`의 `CLAUDE_CODE_OAUTH_TOKEN` 만료/누락
- 훅 관련 차단 메시지 → `claude-config/settings.json`의 훅 경로가 이미지 안 경로가 아니다

- [ ] **Step 3: env 덤프 차단 훅이 이미지 안에서 동작하는지 확인**

```bash
podman run --rm reviewer-selfcontained:test sh -c '
  printf "%s" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"printenv\"}}" |
  python3 /opt/reviewer-claude/scrub/block_env_dump.py; echo "exit=$?"'
```
Expected: `exit=2` (차단). 통과 가능한 명령도 확인:
```bash
podman run --rm reviewer-selfcontained:test sh -c '
  printf "%s" "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git diff\"}}" |
  python3 /opt/reviewer-claude/scrub/block_env_dump.py; echo "exit=$?"'
```
Expected: `exit=0`

- [ ] **Step 4: 실제 MR 1건으로 전체 파이프라인 확인**

낮은 위험도의 MR을 하나 골라(리뷰 코멘트가 실제로 게시된다) 일회성으로 돌린다:

```bash
podman run --rm --env-file .env reviewer-selfcontained:test \
  python review_runner.py <project_id> <mr_iid>
```
Expected: MR에 리뷰 코멘트 게시 + `reviewed-sha` 마커 포함 + 리뷰어/assignee에게 Slack DM. `⚠️ AI 자동 코드 리뷰 실패` 코멘트가 달리면 그 코멘트의 stage/원인을 보고 해결한 뒤 재시도한다.

- [ ] **Step 5: 결과를 기록**

이 게이트 결과를 브랜치의 MR 설명(또는 커밋 메시지)에 남긴다 — 서버에서 문제가 생겼을 때 "이미지 자체는 검증됐다"는 기준점이 된다. 코드 변경이 없으므로 커밋은 없다.

---

### Task 8: 서버 컷오버

**macOS 정지가 먼저다.** 둘 다 뜨면 폴러가 양쪽에서 돌아 같은 MR을 두 번 리뷰한다 — in-flight 가드는 프로세스 내부 상태라 호스트가 갈리면 무효다.

**Files:**
- Modify: `~/00_Projects/compose.yaml` (git 미추적, `include`에서 이 레포 제거)

**Interfaces:**
- Consumes: Task 5의 `Jenkinsfile`, Task 7의 게이트 통과
- Produces: 196-server에서 도는 `gitlab-ai-reviewer`

- [ ] **Step 1: 브랜치를 머지하고 태그를 푸시할 준비**

Task 1~6의 커밋을 `main`에 머지한다(MR 또는 ff 머지). Jenkins job이 태그를 폴링하므로 태그는 머지 후에 붙인다.

- [ ] **Step 2: macOS 리뷰어 정지 (제거하지 않음)**

```bash
cd ~/00_Projects && podman compose -p automation-stack stop ai-reviewer
podman ps --filter name=gitlab-ai-reviewer   # 목록에서 사라져야 한다
```

- [ ] **Step 3: `~/00_Projects/compose.yaml`의 `include`에서 이 레포 제거**

`- path: 97_gitlab-auto-review/docker-compose.yml` 행을 지운다. 이 파일은 git 미추적이므로 커밋 대상이 아니다. 롤백을 위해 지운 행을 메모해 둔다.

- [ ] **Step 4: Jenkins credential 생성**

Manage Jenkins > Credentials > Secret file, id `gitlab-auto-review-env`. 업로드할 `.env`는 로컬 `.env`에서 다음을 정리한 것이다:

- **제외**: `NOTION_TOKEN`, `NOTION_TASKS_DB_ID`, `NOTION_PROJECTS_DB_ID` (96 레포로 분리된 뒤 이 레포가 읽지 않는다), `WEBHOOK_SECRET` (`webhook_server.py`만 읽고 기본 CMD는 Slack 봇 모드다)
- **명시**: `POLL_INTERVAL_SEC` (현재 라이브 `.env`에 없어 기본값 300으로 돌고 있다)
- **포함**: `GITLAB_URL`, `GITLAB_TOKEN`, `REVIEWER_USERNAME`, `CLAUDE_CODE_OAUTH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `REVIEWER_SLACK_ID`, `POLL_INTERVAL_SEC`, `LOG_LEVEL`

- [ ] **Step 5: Jenkins job 생성**

Pipeline job `gitlab-auto-review-deploy`:
- Pipeline script from SCM, SCM: Git
- Repository URL: `https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review.git` (자격증명은 watchtower job과 동일한 것 사용)
- Branch Specifier: `refs/tags/*`
- Advanced > Refspec: `+refs/tags/*:refs/remotes/origin/tags/*`
- Script Path: `Jenkinsfile`

- [ ] **Step 6: 태그 푸시 → 배포 확인**

```bash
git tag v2.0.0 && git push gitlab v2.0.0
```
3분 내 빌드가 시작된다(또는 Jenkins에서 "지금 빌드"). Deploy·Verify 스테이지가 모두 초록이어야 한다. Verify가 빨간색이면 그 로그의 `docker logs --tail 30` 출력에서 부팅 실패 사유(필수 env 누락, `GITLAB_URL` 스킴 등)를 읽는다.

- [ ] **Step 7: E2E 검증**

Slack에서 리뷰 대상 MR에 대해:
```
@ags-watchtower <MR URL>
```
Expected: 스레드 ack 답글 → 잠시 후 MR에 리뷰 코멘트 게시 → Slack DM 수신.

서버 로그로도 확인:
```bash
ssh 196-server 'docker compose -p gitlab-auto-review logs --tail 80 ai-reviewer'
```
`Unknown command: /review-pr`이나 훅 차단 메시지가 없어야 한다.

- [ ] **Step 8: 롤백 (필요 시에만)**

Step 6~7이 실패하고 원인이 빠르게 잡히지 않으면:
```bash
ssh 196-server 'docker compose -p gitlab-auto-review down'
# ~/00_Projects/compose.yaml 의 include 행 복원 후
cd ~/00_Projects && podman compose up -d ai-reviewer
```
macOS `.env`와 이미지는 Step 9까지 보존하므로 즉시 복귀 가능하다.

- [ ] **Step 9: 며칠 관찰 후 macOS 자산 정리**

폴링·채널알림·멘션 세 트리거가 모두 정상 동작한 것을 확인한 뒤:
```bash
podman rm gitlab-ai-reviewer
podman rmi localhost/97_gitlab-auto-review_ai-reviewer reviewer-selfcontained:test
```
`~/.claude/commands/review-pr.md`와 호스트 에이전트 6개는 **지우지 않는다** — 개인 대화 세션에서도 `/review-pr`을 쓴다. 레포의 `claude-config/`와 드리프트하는 것은 의도된 분기다(레포가 리뷰어의 유일한 진실원).

**컷오버 창의 알려진 손실**: 폴러는 기동 시 첫 순회를 baseline으로 잡아 기존 MR을 일괄 리뷰하지 않는 대신, 정지~기동 사이의 push 증분을 한 번 놓친다. 기존 설계에 이미 있는 특성이며 수동 멘션으로 커버한다.

---

## Self-Review

**Spec coverage:**

| 스펙 절 | 담당 태스크 |
|---|---|
| §5 아키텍처 (config 조달 경로만 변경) | Task 1~3 |
| §6.1 `claude-config/` | Task 1 |
| §6.2 Dockerfile (핀 + COPY + ENV) | Task 2 |
| §6.3 `settings.json` 최소 구성 | Task 1 Step 5 |
| §6.4 compose `volumes:` 삭제 | Task 3 |
| §6.5 Jenkinsfile | Task 5 |
| §6.6 credential 정리 (`NOTION_*`·`WEBHOOK_SECRET` 제외, `POLL_INTERVAL_SEC` 명시) | Task 8 Step 4 (수동) + Task 4 (드리프트 회귀 가드) |
| §7 남는 macOS 의존 (토큰 갱신 절차) | Task 6 Step 1 (CLAUDE.md에 기록) |
| §8 컷오버 / 롤백 | Task 8 |
| §9 테스트 5종 | Task 1(에이전트·커맨드), 2(Dockerfile), 3(compose), 4(env), 5(Jenkinsfile) |
| §10 리스크 | Task 7이 이미지 리스크를, Task 8 Step 8이 배포 리스크를 게이트 |
| §11 후속 (CLAUDE.md 재작성) | Task 6 — 스펙은 "범위 밖"이라 했지만, 낡은 문서를 남기면 다음 진단을 오염시키므로 이 계획에 포함시켰다 |

**Placeholder scan:** "적절히", "필요시 처리", "위 내용에 대한 테스트" 같은 서술 없음. 모든 코드 단계에 실제 코드 블록이 있다. Task 7 Step 4와 Task 8 Step 6의 `<project_id>`/`<mr_iid>`/`v2.0.0`은 실행 시점에 사람이 고르는 값이므로 플레이스홀더가 아니라 인자다.

**Type consistency:** `tests/test_deploy_config.py`의 상수는 Task 1에서 정의되고(`ROOT`, `CONFIG`, `REVIEW_AGENTS`, `ALLOWED_AGENT_TOOLS`) Task 2~5가 그대로 재사용한다. 헬퍼는 `_frontmatter`(Task 1), `_compose_service`(Task 3), `_env_read_by_code`·`_env_in_example`(Task 4)로 이름이 겹치지 않는다. `import re`는 Task 1의 헤더에 이미 있어 Task 2·4가 추가 import 없이 쓴다. compose 서비스명은 전 태스크에서 `ai-reviewer`, 프로젝트명은 `gitlab-auto-review`, 컨테이너명은 `gitlab-ai-reviewer`로 일관된다.

**한 가지 의도적 순서 결정:** Task 3(compose 마운트 제거)이 Task 7(로컬 E2E)보다 앞에 온다. 그래서 Task 3~6 구간에서는 macOS 로컬 컨테이너를 `podman compose up`으로 되살릴 수 없다(마운트가 없어져 호스트 config를 못 읽는다 — 이미지에 굽힌 것으로만 동작한다). 이 구간에 리뷰가 필요하면 Task 7 Step 4의 일회성 `review_runner.py` 실행으로 처리한다. 롤백이 필요하면 `git revert` 또는 `git checkout ffbde58 -- docker-compose.yml`로 이전 compose를 되살린다.
