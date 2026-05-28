# 격리 테스트 환경. 호스트 conda(cv2 등) 오염을 차단하는 전용 venv에서 pytest 실행.
# 런타임 Docker 이미지는 pytest를 의도적으로 제외하므로 테스트는 여기서만 돈다.
VENV   := .venv
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: venv test test-rebuild clean-venv

# venv 없으면 생성 + dev 의존성 설치 (멱등: requirements 바뀔 때만 재설치)
venv: $(VENV)/.installed

$(VENV)/.installed: requirements-dev.txt requirements.txt
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	touch $@

# 격리 venv에서 테스트 실행
test: venv
	$(PYTEST)

# venv 갈아엎고 처음부터 재설치 후 테스트
test-rebuild: clean-venv test

clean-venv:
	rm -rf $(VENV)
