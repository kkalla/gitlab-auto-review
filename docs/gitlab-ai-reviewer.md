# GitLab AI Auto Reviewer

## 목표
GitLab 사내 인스턴스에서 **내가 리뷰어로 지정되면 자동으로 코드 리뷰 후 코멘트를 남기는 시스템** 구축.

리뷰 생성은 Claude Code CLI의 `/review-pr` 슬래시 커맨드를 사용한다.

---

## 핵심 결정사항

- **Claude API 미사용** → Claude Code CLI (`claude -p`) + Subscription으로 처리
- **실행 환경**: Docker 컨테이너
- **인증 방식**: 호스트에서 `claude login` 후 `~/.claude` 볼륨 마운트

---

## 아키텍처

```
GitLab MR (리뷰어: max 지정)
    → Webhook POST 이벤트
    → FastAPI 웹훅 서버 (Docker)
    → review_runner.py 실행
        → GitLab API로 MR diff 수집
        → claude -p "..." 로 리뷰 생성
        → GitLab API로 MR 코멘트 작성
```

---

## 파일 구조

```
gitlab-ai-reviewer/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── webhook_server.py
└── review_runner.py
```

---

## 구현 코드

### Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080"]
```

### docker-compose.yml

```yaml
services:
  ai-reviewer:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ~/.claude:/root/.claude:ro   # 호스트 credentials 마운트 (핵심)
    environment:
      - GITLAB_URL=https://your-gitlab.com
      - GITLAB_TOKEN=your-personal-access-token
      - WEBHOOK_SECRET=your-webhook-secret
      - REVIEWER_USERNAME=max
    restart: unless-stopped
```

### requirements.txt

```
fastapi
uvicorn
httpx
```

### webhook_server.py

```python
from fastapi import FastAPI, Request, Header, HTTPException
import asyncio, os

app = FastAPI()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
REVIEWER_USERNAME = os.environ.get("REVIEWER_USERNAME", "max")

@app.post("/webhook/gitlab")
async def handle(request: Request, x_gitlab_token: str = Header(None)):
    if x_gitlab_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401)

    payload = await request.json()

    reviewers = payload.get("reviewers", [])
    action = payload.get("object_attributes", {}).get("action")

    if action not in ("update", "open"):
        return {"status": "skipped"}
    if not any(r["username"] == REVIEWER_USERNAME for r in reviewers):
        return {"status": "skipped"}

    project_id = payload["project"]["id"]
    mr_iid = payload["object_attributes"]["iid"]

    # 백그라운드 실행 (웹훅 응답 먼저 반환)
    asyncio.create_task(run_review_async(project_id, mr_iid))

    return {"status": "review started"}

async def run_review_async(project_id: int, mr_iid: int):
    proc = await asyncio.create_subprocess_exec(
        "python", "review_runner.py", str(project_id), str(mr_iid)
    )
    await proc.wait()
```

### review_runner.py

```python
import subprocess, httpx, sys, os

GITLAB_URL = os.environ["GITLAB_URL"]
PRIVATE_TOKEN = os.environ["GITLAB_TOKEN"]

def get_mr_diff(project_id: int, mr_iid: int) -> str:
    headers = {"PRIVATE-TOKEN": PRIVATE_TOKEN}
    with httpx.Client() as client:
        mr = client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
            headers=headers
        ).json()
        diffs = client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/diffs",
            headers=headers
        ).json()

    diff_text = f"MR 제목: {mr['title']}\n설명: {mr.get('description','없음')}\n\n"
    for d in diffs[:10]:
        diff_text += f"### {d['new_path']}\n```diff\n{d['diff'][:2000]}\n```\n\n"
    return diff_text

def run_claude_review(diff_text: str) -> str:
    # /review-pr 슬래시 커맨드를 사용하여 리뷰 수행
    prompt = f"""/review-pr

아래는 GitLab Merge Request의 diff 정보야. 이걸 PR 리뷰하듯이 분석해줘.

{diff_text}
"""
    result = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude 실행 실패: {result.stderr}")
    return result.stdout

def post_comment(project_id: int, mr_iid: int, review: str):
    with httpx.Client() as client:
        client.post(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes",
            headers={"PRIVATE-TOKEN": PRIVATE_TOKEN},
            json={"body": f"🤖 **AI 자동 코드 리뷰**\n\n{review}"}
        )

if __name__ == "__main__":
    project_id, mr_iid = int(sys.argv[1]), int(sys.argv[2])
    print(f"MR !{mr_iid} 리뷰 시작...")
    diff = get_mr_diff(project_id, mr_iid)
    review = run_claude_review(diff)
    post_comment(project_id, mr_iid, review)
    print("완료!")
```

---

## 배포 순서

```bash
# 1. 호스트에서 Claude 로그인 (최초 1회)
claude login

# 2. credentials 확인
ls ~/.claude/

# 3. 빌드 & 실행
docker compose up -d

# 4. 로그 확인
docker compose logs -f

# 5. 동작 테스트
curl -X POST http://localhost:8080/webhook/gitlab \
  -H "X-Gitlab-Token: your-webhook-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "object_attributes": {"action": "update", "iid": 1},
    "project": {"id": 10},
    "reviewers": [{"username": "max"}]
  }'
```

---

## GitLab Webhook 설정

- 경로: GitLab 프로젝트 → Settings → Webhooks
- URL: `http://your-server:8080/webhook/gitlab`
- Secret Token: `docker-compose.yml`의 `WEBHOOK_SECRET` 값과 동일하게
- Trigger: `Merge request events` 체크

---

## 주의사항 / 트러블슈팅

| 이슈 | 원인 | 해결 |
|---|---|---|
| `~/.claude` 권한 에러 | read-only 마운트 충돌 | `docker-compose.yml`에서 `:ro` 제거 |
| Claude 세션 만료 | OAuth 토큰 갱신 필요 | 호스트에서 `claude login` 재실행 (컨테이너 재시작 불필요) |
| Webhook 이벤트 누락 | 리뷰어 지정 액션 분기 | `action` 값 로그로 확인 후 조건 추가 |
| 동시 MR 리뷰 충돌 | Subscription rate limit | `asyncio.Semaphore`로 동시 실행 수 제한 |

---

## 미구현 / 향후 개선 아이디어

- [ ] 인라인 코멘트 (줄 단위 코멘트, GitLab discussions API 활용)
- [ ] 특정 파일 확장자만 리뷰 (`.py`, `.ts` 등 필터링)
- [ ] 리뷰 결과에 따라 Approve / Request Changes 자동 처리
- [ ] 동시 MR 큐 처리 (`asyncio.Semaphore` 또는 Celery)
- [ ] 리뷰 프롬프트를 프로젝트별로 커스터마이징
