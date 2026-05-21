"""pytest 공통 설정.

review_runner는 import 시점에 필수 환경변수를 읽으므로(`os.environ[...]`),
테스트 수집 전에 더미 값을 채워 둔다. conftest.py는 테스트 모듈 import보다
먼저 실행되므로 여기서 설정하면 안전하다.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("GITLAB_URL", "https://gitlab.example.com")
os.environ.setdefault("GITLAB_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-0123456789")

# 저장소 루트를 import 경로에 추가 — review_runner.py가 루트에 있다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
