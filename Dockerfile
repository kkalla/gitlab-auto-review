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

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_server.py review_runner.py slack_bot.py slack_notifier.py ./

# 기본 진입점 = Slack Socket Mode 봇 (공개 inbound 포트 불필요 — 봇이 아웃바운드
# WebSocket을 연다). webhook 모드로 돌리려면 compose에서 command를 아래로 덮어쓴다:
#   command: ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
# (그 경우 ports: ["8080:8080"]도 함께 노출할 것.)
CMD ["python", "slack_bot.py"]
