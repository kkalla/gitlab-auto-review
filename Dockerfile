FROM python:3.11-slim

# Node.js 20 + Claude Code CLI + tzdata 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 타임존 KST 고정 (logger asctime, git/claude 메시지 모두 영향)
ENV TZ=Asia/Seoul
RUN ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 버전 핀 필수 — 미고정이면 빌드마다 최신이 들어와 재배포가 곧 조용한
# claude 업그레이드가 된다. 2.1.220은 리뷰가 실제로 돌던 버전이다.
# 올릴 때는 이 값을 의도적으로 커밋한다.
RUN npm install -g @anthropic-ai/claude-code@2.1.220

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_server.py review_runner.py slack_bot.py slack_notifier.py ./

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

# 기본 진입점 = Slack Socket Mode 봇 (공개 inbound 포트 불필요 — 봇이 아웃바운드
# WebSocket을 연다). webhook 모드로 돌리려면 compose에서 command를 아래로 덮어쓴다:
#   command: ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
# (그 경우 ports: ["8080:8080"]도 함께 노출할 것.)
CMD ["python", "slack_bot.py"]
