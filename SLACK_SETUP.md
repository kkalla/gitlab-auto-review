# Slack 봇 모드 설정 (Socket Mode)

`slack_bot.py`는 Slack에서 GitLab MR 리뷰를 트리거하는 진입점이다.

> ⚠️ **Slack 앱이 `ags-watchtower`와 분리돼 있다.** 이 봇은 **전용 앱(App ID
> `A0BFM68FM41`)**을 쓴다. `ags-watchtower` 앱은 `98_ags-watchtower` 레포 봇 소유다.
> - 그 앱의 토큰을 여기 `.env`에 넣지 않는다 — 같은 앱 토큰으로 Socket Mode 소켓을 두
>   군데서 열면 Slack이 이벤트를 분산시켜 양쪽 다 간헐적으로 유실된다.
> - 아래 manifest·Interactivity 변경도 **이 앱에만** 한다. `ags-watchtower` 앱에 적용하면
>   그쪽 지식 봇이 쓰는 `channels:history` 등이 빠져 망가진다.
> - 앱 설정: `https://app.slack.com/app-settings/T0A6YUSHNRG/A0BFM68FM41/general`
**Socket Mode**라 공개 inbound URL·포트·리버스 프록시가 필요 없다 — 봇이 Slack으로
아웃바운드 WebSocket을 직접 연다(방화벽/NAT 무관).

트리거는 두 가지다:

- **리뷰 요청 DM + 버튼** — 봇이 주기적으로(`POLL_INTERVAL_SEC`) reviewer 지정 열린 MR을
  확인해, **새로 리뷰어로 지정**되거나 **새 push**가 들어온 MR마다 리뷰어에게 DM을 보낸다(§6).
  DM에는 MR 링크, **Intent 리뷰 / 코드 리뷰** 안내, 「코드 리뷰하기」 버튼이 있다. 리뷰는
  버튼을 눌러야 돈다(자동 실행 안 함).
- **수동·멘션** — `@<봇> <MR URL>`로 직접 멘션하면 바로 리뷰.

```
(DM)   POLL_INTERVAL_SEC마다 열린 MR의 source SHA 확인 → 신규 지정·새 push
         → 변경 파일로 intent/코드 판별 → 리뷰어 DM(링크 + 안내 + 버튼)
         → 「코드 리뷰하기」 클릭 → 리뷰 시작
(멘션) @<봇> <MR URL> → 봇 app_mention 수신 → 리뷰 시작
  → project_id/mr_iid 해석
  → review_runner.py 서브프로세스 (증분은 MR 코멘트 reviewed-sha 마커로 자동)
  → 완료/실패 시 스레드 답글(멘션은 채널 스레드, 버튼은 DM 스레드) + 리뷰어·assignee DM
```

## 1. Slack App 생성 (manifest) — 새로 만들 때만

<https://api.slack.com/apps> → **Create New App** → **From an app manifest** →
워크스페이스 선택 → 아래 YAML 붙여넣기:

```yaml
display_information:
  name: MR Reviewer          # 새로 만들 때 이름은 자유 (ags-watchtower와 겹치지 않게)
  description: GitLab MR 자동 코드 리뷰
  background_color: "#1b2a4a"
features:
  bot_user:
    display_name: MR Reviewer
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read   # @멘션 수신 (수동 트리거)
      - chat:write          # 스레드 답글 / DM 전송
      - users:read          # 사용자 조회
      - users:read.email    # 이메일 → Slack ID (assignee 매핑)
      - im:write            # DM 채널 열기 (conversations.open)
settings:
  event_subscriptions:
    bot_events:
      - app_mention         # 수동: @멘션
  interactivity:
    is_enabled: true        # 리뷰 요청 DM의 「코드 리뷰하기」 버튼 (Socket Mode라 Request URL 불필요)
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

> **이미 쓰는 앱(`A0BFM68FM41`)을 갱신할 때**: manifest를 통째로 붙이지 않는다 — 위 YAML의
> `name`이 현재 앱 이름을 덮어쓴다. 대신 그 앱 설정에서 바뀐 것만 고친다.
> 1. **Interactivity & Shortcuts** → 토글 **On** (리뷰 요청 DM 버튼. Socket Mode라 Request
>    URL 불필요 — 필수 입력이면 placeholder URL 아무거나).
> 2. (정리) **Event Subscriptions**에서 `message.channels`, **OAuth & Permissions**에서
>    `channels:history` 제거 — 채널 알림 트리거가 없어져 안 쓴다.
> 3. **Install App → Reinstall to Workspace**. 재설치해도 `SLACK_BOT_TOKEN` 값은 그대로다.

## 3. 봇을 채널에 초대 + 내 member ID 확인

- 멘션으로 리뷰를 트리거할 채널에서 `/invite @<봇>`.
- 리뷰 요청·완료/실패 DM을 받을 "나"의 member ID: Slack 프로필 → **⋯ 더보기** →
  **멤버 ID 복사** (`U…`) → `REVIEWER_SLACK_ID`에. **비면 리뷰 요청 DM 폴러가 뜨지 않는다**(버튼도 이 ID만 누를 수 있다).

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
@<봇> https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review/-/merge_requests/123
```

봇이 스레드에 ack → 리뷰 완료 시 MR 코멘트 + 스레드 답글 + 리뷰어·assignee DM.

## 6. 리뷰 요청 DM + 「코드 리뷰하기」 버튼 — 폴러

- 봇이 `POLL_INTERVAL_SEC`(기본 300초)마다 GitLab API로 `reviewer=REVIEWER_USERNAME`인
  열린 MR 목록과 각 MR의 source SHA를 가져온다.
- **새로 리뷰어로 지정된 MR**과 **SHA가 바뀐 MR(새 push)**마다 `REVIEWER_SLACK_ID`에게 DM을 보낸다.
- DM의 리뷰 유형은 MR 변경 파일로 판별한다(push마다 다시 판별 — intent 검토 뒤 같은 브랜치에
  구현이 올라오기 때문):
  - **Intent 리뷰** — 변경 파일이 전부 `docs/intents/…` 또는 `…/decisions/ADR-*`. 문서를 직접
    읽고 MR 코멘트로 답한다.
  - **코드 리뷰** — 그 밖의 파일이 하나라도 섞임. 「코드 리뷰하기」로 AI 리뷰를 돌린다.
- 「코드 리뷰하기」를 누르면 기존 파이프라인으로 리뷰가 돌고(증분은 `reviewed-sha` 마커),
  DM의 버튼은 사라지며 결과는 그 DM 스레드에 답글로 온다.
- 끄려면 `.env`에 `POLL_INTERVAL_SEC=0` — 그러면 멘션(§5)만 동작한다.
- 한계: intent 검토 요청은 MR **코멘트 @멘션**으로도 오는데(reviewer 지정은 선택), 폴러는
  reviewer 지정만 본다 — 코멘트로만 부른 요청은 DM이 오지 않는다. push 후 최대 한 폴링
  간격(기본 5분) 지연되고, 봇 재시작 직후의 지정·push는 baseline에 흡수돼 한 번 놓칠 수 있다
  (그땐 수동 `@멘션`).

> Notion 현황 조회(`/task-status`·`/project-status`)는 98_ags-watchtower 레포로
> 분리됐다 — 이 봇/앱엔 없다.

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
