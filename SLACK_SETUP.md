# Slack 봇 모드 설정 (Socket Mode)

`slack_bot.py`는 Slack에서 GitLab MR 리뷰를 트리거하는 진입점이다.
**Socket Mode**라 공개 inbound URL·포트·리버스 프록시가 필요 없다 — 봇이 Slack으로
아웃바운드 WebSocket을 직접 연다(방화벽/NAT 무관).

트리거는 세 가지다:

- **자동·채널알림** — GitLab Slack notification이 MR 열릴 때 채널에 링크를 뿌리면, 봇이
  그 메시지를 보고 멘션 없이 리뷰를 시작한다(§6). 주로 **MR open** 시.
- **자동·폴링** — 봇이 주기적으로(`POLL_INTERVAL_SEC`) reviewer 지정 열린 MR의 source SHA를
  확인해 **새 push(증분)**를 자동 리뷰한다(§7). GitLab Slack 알림은 push를 채널에 안
  띄우므로, MR에 커밋이 쌓일 때마다 자동 재리뷰하려면 이게 필요하다.
- **수동·멘션** — `@ags-watchtower <MR URL>`로 직접 멘션. 특정 MR만 골라 리뷰할 때.

```
(자동·채널) GitLab 채널 알림 → 봇 message 수신 → MR 링크 추출
(자동·폴링) POLL_INTERVAL_SEC마다 열린 MR의 source SHA 확인 → 변경분(새 push)
(수동·멘션) @ags-watchtower <MR URL> → 봇 app_mention 수신
  → project_id/mr_iid 해석
  → review_runner.py 서브프로세스 (증분은 MR 코멘트 reviewed-sha 마커로 자동)
  → 완료/실패 시 (멘션·채널은 스레드 답글 +) 리뷰어·assignee DM
```

## 1. Slack App 생성 (manifest)

<https://api.slack.com/apps> → **Create New App** → **From an app manifest** →
워크스페이스 선택 → 아래 YAML 붙여넣기:

```yaml
display_information:
  name: AGS Watchtower
  description: GitLab MR 자동 코드 리뷰 + Notion 프로젝트·태스크 현황 조회
  background_color: "#1b2a4a"
features:
  bot_user:
    display_name: AGS Watchtower
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read   # @멘션 수신 (수동 트리거)
      - channels:history    # 채널 메시지 수신 (GitLab 알림 자동 트리거)
      - chat:write          # 스레드 답글 / DM 전송
      - users:read          # 사용자 조회
      - users:read.email    # 이메일 → Slack ID (assignee 매핑)
      - im:write            # DM 채널 열기 (conversations.open)
settings:
  event_subscriptions:
    bot_events:
      - app_mention         # 수동: @멘션
      - message.channels    # 자동: 봇이 든 public 채널의 메시지
  interactivity:
    is_enabled: false       # 버튼 미사용 — 멘션/채널 메시지 트리거만
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

## 2. 토큰 두 개 발급

- **App-Level Token (`xapp-…`)**: Settings → **Basic Information** → *App-Level Tokens*
  → **Generate** → 스코프 `connections:write` → 값을 `SLACK_APP_TOKEN`에.
- **Bot User OAuth Token (`xoxb-…`)**: Settings → **Install App** → 워크스페이스에 설치
  → *Bot User OAuth Token* → 값을 `SLACK_BOT_TOKEN`에.

> Socket Mode를 manifest로 못 켰다면 Settings → **Socket Mode** → *Enable* 토글.

> **이미 앱을 만들어 둔 경우(스코프/이벤트를 추가했을 때)**: Settings → **App
> Manifest**에 위 YAML을 다시 붙여 저장한 뒤, **Install App → Reinstall to
> Workspace**로 재설치해야 새 스코프(`channels:history`)·이벤트(`message.channels`)가
> 적용된다. 재설치해도 `SLACK_BOT_TOKEN` 값은 그대로다.

## 3. 봇을 채널에 초대 + 내 member ID 확인

- 리뷰를 트리거할 채널에서 `/invite @ags-watchtower`.
- 완료/실패 DM을 받을 "나"의 member ID: Slack 프로필 → **⋯ 더보기** →
  **멤버 ID 복사** (`U…`) → `REVIEWER_SLACK_ID`에.

## 4. `.env` 채우고 실행

```bash
cp .env.example .env
# GITLAB_URL, GITLAB_TOKEN, REVIEWER_USERNAME
# SLACK_BOT_TOKEN, SLACK_APP_TOKEN, REVIEWER_SLACK_ID 채우기
# CLAUDE_CODE_OAUTH_TOKEN ← 아래에서 발급 / POLL_INTERVAL_SEC(기본 300, 폴러 주기)
```

**Claude 인증 토큰 발급 (필수, 호스트에서 1회):**

컨테이너의 `claude`는 호스트 macOS Keychain을 못 읽으므로 `~/.claude` 마운트만으로는
인증이 안 된다(`Not logged in`). 호스트에서 구독 기반 **장기 토큰**(약 1년 유효)을 발급해
`.env`에 넣는다 — `ANTHROPIC_API_KEY`는 쓰지 않는다.

```bash
claude setup-token        # 브라우저 OAuth → 출력된 토큰을 복사
# .env에  CLAUDE_CODE_OAUTH_TOKEN=<복사한 토큰>  추가
```

> 토큰이 노출됐거나 만료되면 `claude setup-token`을 다시 실행해 회전하고 `.env`를
> 갱신한다. (`claude /login`의 단기 토큰은 ~8시간 만에 만료되니 봇용으로 쓰지 말 것.)

**macOS + Podman (권장 타깃):**

```bash
# 최초 1회: Linux VM 준비 (claude+node+git이라 리소스 넉넉히)
podman machine init --cpus 4 --memory 4096   # 이미 있으면 생략
podman machine start

podman compose up -d --build
podman compose logs -f ai-reviewer    # "Socket Mode 연결 시도" + "MR 폴러 시작" 로그 확인
```

> `podman compose`는 `docker-compose`나 `podman-compose` 중 설치된 provider를
> 호출하는 래퍼다. 없으면 `brew install docker-compose` 또는
> `pip install podman-compose` 중 하나를 깔면 된다. macOS엔 SELinux가 없어
> compose의 `${HOME}/.claude` 마운트에 `:z`/`:Z`를 붙이지 않는다.

**Docker를 쓰는 경우:** 위 `podman compose`를 `docker compose`로 바꾸면 된다.

## 5. 사용 (수동 트리거)

채널/스레드에서:

```
@ags-watchtower https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review/-/merge_requests/123
```

봇이 스레드에 ack → 리뷰 완료 시 MR 코멘트 + 스레드 답글 + 리뷰어·assignee DM.

## 6. (자동 트리거) GitLab 알림으로 멘션 없이 리뷰

MR이 열릴 때마다 자동으로 리뷰가 돌게 하려면, GitLab이 MR 링크를 채널에 뿌리고
봇이 그 링크를 잡게 한다. 멘션도 추가 inbound 포트도 필요 없다.

1. **봇을 알림 채널에 초대**: `/invite @ags-watchtower` (§3에서 했으면 생략).
2. **GitLab Slack notification 켜기**: 대상 프로젝트 → **Settings → Integrations →
   **Slack notifications** → *Active* 체크.
   - **Webhook**: Slack incoming webhook URL (Slack 앱의 *Incoming Webhooks* 또는
     워크스페이스 관리자에게 발급받음).
   - **Trigger**: **Merge request**만 체크. **Comment(Note)는 반드시 끈다** — 봇이 단
     코멘트가 다시 채널 알림 → 봇 재트리거로 이어지는 **무한 루프**를 막는다. Push/Pipeline도
     노이즈라 끈다.
   - **Channel**: 봇이 들어가 있는 채널명(`#mr-review` 등).
3. 이제 MR이 열리면 GitLab이 채널에 `<...|repo!123 제목>` 링크를 올리고, 봇이 그
   메시지의 링크를 추출해 자동으로 리뷰를 시작한다.

> **동작 원리**: 봇은 채널 `message` 이벤트를 듣고 메시지 본문·attachments에서 MR
> URL을 찾는다. MR 링크가 없는 일반 대화는 조용히 무시하고, 봇 자신의 답글은 Slack
> Bolt가 걸러 무한 트리거가 생기지 않는다.
>
> **private 채널**이면 manifest의 스코프에 `groups:history`, 이벤트에
> `message.groups`를 추가하고 앱을 재설치한다(public 채널은 위 설정 그대로).
>
> 자동 트리거를 끄려면 GitLab Slack notification을 끄거나 봇을 채널에서 내보내면
> 된다 — 수동 `@멘션`은 그대로 동작한다.

## 7. (자동 트리거) push 증분 — 폴러

§6의 채널 알림은 보통 **MR이 열릴 때만** 오고, MR에 **새 커밋이 push될 때**는 GitLab이
채널에 안 띄운다. 그래서 push 증분(커밋 추가 시 자동 재리뷰)은 봇 내장 **폴러**가 맡는다.

- 봇이 `POLL_INTERVAL_SEC`(기본 300초)마다 GitLab API로 `reviewer=REVIEWER_USERNAME`인
  열린 MR 목록과 각 MR의 source SHA를 가져온다.
- SHA가 직전 확인 때와 다르면(= 새 push) 리뷰를 트리거한다. 실제로 **새 커밋만** 리뷰할지
  스킵할지는 review_runner의 `reviewed-sha` 마커가 판단한다(증분 리뷰).
- **추가 설정 없음** — `reviewer_username`이 `REVIEWER_USERNAME`인 열린 MR이면 자동 대상이다.
  GitLab Slack notification(§6)도, 봇 채널 초대도 필요 없다(폴러는 GitLab API를 직접 호출).
- 끄려면 `.env`에 `POLL_INTERVAL_SEC=0` — 그러면 채널 알림(§6)·멘션(§5)만 동작한다.
- 트레이드오프: push 후 최대 한 폴링 간격(기본 5분)만큼 지연된다. 봇 재시작 직후의 push는
  baseline에 흡수돼 한 번 놓칠 수 있다(그땐 수동 `@멘션`으로 처리).

## 8. (부가 기능) /task-status·/project-status — Notion 현황

MR 리뷰와 별개로, Notion 현황을 슬래시 커맨드 **두 개**로 조회한다.
Socket Mode라 **Request URL 없이** 등록만 하면 된다.

- **`/task-status`** — Tasks DB 태스크 리포트. 프로젝트 티어로 묶는다:
  ⚠️ 정합성 이슈(종료 프로젝트 미완료) → 🔴 지연(살아있는 프로젝트의 진짜 지연,
  N일 지남 표시) → 진행중 프로젝트 → 예정(스케줄 확정·미시작) → 프로젝트 미연결 →
  일정 없음. 티어 안은 지연→차단→진행중→대기 순 + 아이템 이모지(🔴🚧🔵⏸️).
  `프로젝트별`/`담당자별` 인자를 주면 티어 대신 그 축으로 묶어 본다.
- **`/project-status`** — Projects DB 프로젝트 현황(진행중/예정/종료) + 프로젝트별
  태스크 완료율.

1. **Slack 앱에 커맨드 2개 등록**: [api.slack.com/apps](https://api.slack.com/apps) →
   해당 앱 → **Slash Commands → Create New Command**. **`/task-status`는 신규
   등록이 필요하다** — 기존에 `/project-status`만 등록해 둔 앱이라면
   `/task-status`를 추가로 만들지 않으면 커맨드가 워크스페이스에 안 보인다.
   - Command: `/task-status`
     - Short Description: `Notion 태스크 현황 (프로젝트 티어 정렬)`
     - Usage Hint: `[지연|차단|진행|대기|완료|담당자이름] [프로젝트별|담당자별] [public]`
   - Command: `/project-status`
     - Short Description: `Notion 프로젝트 현황 (진행중/예정/종료)`
     - Usage Hint: `[public]`
   - Socket Mode가 켜져 있으면 Request URL은 입력하지 않는다.
2. 앱 **재설치**(Reinstall to Workspace) — `commands` 스코프가 자동 추가된다.
3. **Notion 통합 발급**: [notion.so/my-integrations](https://www.notion.so/my-integrations)에서
   internal integration 생성 → secret을 `.env`의 `NOTION_TOKEN`에.
   capability에 **사용자 정보 읽기**를 켜야 Assignee/Owner 이름이 나온다.
4. **DB에 통합 연결**: Tasks DB와 **Projects DB 둘 다** 우상단 `⋯` → 연결 →
   통합 선택. DB가 다르면 `.env`의 `NOTION_TASKS_DB_ID` /
   `NOTION_PROJECTS_DB_ID`를 바꾼다.

사용:

```
/task-status                 # 태스크 리포트 (나에게만 보임)
/task-status 지연             # 지연 태스크만
/task-status kkalla          # 담당자 이름 부분일치
/task-status 프로젝트별        # 프로젝트별로 묶어 보기
/task-status 담당자별 지연     # 담당자별 그룹 + 지연 필터
/task-status public          # 채널 전체 공개로 게시
/project-status              # 프로젝트 현황 (나에게만 보임)
/project-status public       # 채널 전체 공개로 게시
```

`NOTION_TOKEN`이 비어 있으면 두 커맨드는 안내 문구만 답하고, 봇의 다른
기능(리뷰 트리거)엔 영향이 없다.

## assignee DM이 안 오는 경우

assignee Slack 매핑은 **GitLab 이메일 → Slack `users.lookupByEmail`**로 동작한다.
다음 중 하나면 해당 assignee DM은 조용히 생략된다(리뷰 자체는 정상):

- GitLab 프로필의 **공개 이메일(`public_email`)이 비어 있음** — 일반 PAT로는
  비공개 이메일을 못 읽는다. assignee가 프로필에서 공개 이메일을 설정해야 한다.
- GitLab 공개 이메일과 **Slack 계정 이메일이 다름** — 같은 이메일이어야 매핑된다.

리뷰어 본인(`REVIEWER_SLACK_ID`)은 이메일 매핑 없이 항상 DM된다.

## webhook 모드로 되돌리려면

이미지에는 `webhook_server.py`도 함께 들어 있다. `docker-compose.yml`에서
`command`로 uvicorn을 띄우고 `ports: ["8080:8080"]`을 열면 기존 GitLab webhook
모드로 동작한다(`.env`에 `WEBHOOK_SECRET` 필요). 자세한 건 `docker-compose.yml`
주석 참고.
