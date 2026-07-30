#!/usr/bin/env python3
"""PreToolUse(Bash) 훅: .env 파일 내용 덤프 차단.

세션에서 .env를 cat/grep하면 그 값이 transcript → secall 아카이브(iCloud)에
평문으로 영구 보존된다 (2026-07-08 크리덴셜 9종 유출 사고의 원인).
변수명만 보는 sanctioned idiom은 통과시킨다.

settings.json 등록: PreToolUse, matcher "Bash". exit 2 = 차단.
"""

import json
import re
import sys

ENV_FILE = re.compile(r"(^|[\s/=])\.env(\.[A-Za-z0-9_.-]+)?\b")
DUMP_CMD = re.compile(
    r"\b(cat|bat|less|more|head|tail|strings|grep|rg|ag|awk|sed|cut)\b"
)
PRINTENV = re.compile(r"\bprintenv\b")
# 변수명만 보는 허용 관용구 (값 미출력)
SANCTIONED = re.compile(
    r"\^?\[A-Z_\]\+=|cut\s+-d=?\s*['\"]?=?['\"]?\s+-f\s*1|grep\s+(-\w*c)"
)

BLOCK_MSG = (
    "[시크릿 하이진] .env 값 출력은 차단됨 — 출력값이 세션 아카이브(iCloud)에 평문으로 영구 보존됨. "
    "변수명만 확인: grep -oE '^[A-Z_]+=' .env | "
    "토큰 유효성은 echo 대신 실제 API 호출로 검증 | "
    "값 자체가 필요하면 사용자에게 직접 요청할 것."
)


def should_block(command):
    if PRINTENV.search(command):
        return True
    if not ENV_FILE.search(command):
        return False
    if not DUMP_CMD.search(command):
        return False  # source/ls 등 비출력 명령은 통과
    return not SANCTIONED.search(command)


def main():
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command", "")
    except (json.JSONDecodeError, AttributeError):
        return 0  # 훅 입력이 이상하면 차단하지 않는다 (fail-open)
    if should_block(command):
        print(BLOCK_MSG, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
