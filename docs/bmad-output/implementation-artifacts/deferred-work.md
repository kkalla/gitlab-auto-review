# Deferred Work

## Deferred from: code review of spec-gitlab-mr-auto-reviewer (2026-05-20)

- **[LOW] SIGTERM graceful shutdown 부재** — `webhook_server.py`. 컨테이너가 SIGTERM 받았을 때 진행 중인 `claude`/`review_runner` 자식 프로세스를 명시적으로 정리하지 않음. 초기 구현 허용 범위로 판단. 운영 중 컨테이너 재시작/스케일링 빈도가 잦아지면 재검토 (spec의 `Ask First` — "동시 MR 처리 정책"과 함께 묶어서 다룰 후보).
- **[MEDIUM] `~/.claude` 마운트 권한 정책** — `docker-compose.yml:10` + `README.md:34-36`. README가 권한 에러 시 `:ro` 제거를 권장하지만 컨테이너 침해 시 호스트 OAuth 자격증명 변조 가능. **사유**: Decision #1로 `--allowed-tools "Read"` 화이트리스트가 적용되어 RCE 표면이 충분히 좁아졌다고 판단, 운영 후 재검토. 재검토 시 옵션: 별도 서비스 계정 Claude 세션 / 토큰 파일만 좁게 마운트.
