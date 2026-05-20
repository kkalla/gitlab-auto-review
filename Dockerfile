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

COPY webhook_server.py review_runner.py ./

EXPOSE 8080

CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
