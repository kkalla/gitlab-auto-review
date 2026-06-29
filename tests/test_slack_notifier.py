"""slack_notifier의 순수 함수 테스트 (네트워크 없음).

parse_mr_url은 slack_bot이 멘션 텍스트에서 MR을 식별하는 핵심 파서다.
slack_bolt 미설치 환경에서도 돌도록 slack_notifier(httpx만 의존)에 두었다.
"""

import slack_notifier


def test_parse_basic_url_with_port_and_subgroup():
    text = "https://git.sparklingsoda.ai:8443/vision/gitlab-auto-review/-/merge_requests/123"
    assert slack_notifier.parse_mr_url(text) == ("vision/gitlab-auto-review", 123)


def test_parse_with_surrounding_mention_text():
    text = "<@U123> please review https://gitlab.example.com/group/sub/repo/-/merge_requests/7 thanks"
    assert slack_notifier.parse_mr_url(text) == ("group/sub/repo", 7)


def test_parse_slack_angle_bracket_wrapping():
    # Slack은 URL을 <url> 또는 <url|label>로 감싼다.
    text = "리뷰 부탁 <https://gitlab.example.com/group/repo/-/merge_requests/42>"
    assert slack_notifier.parse_mr_url(text) == ("group/repo", 42)


def test_parse_slack_link_with_label():
    text = "<https://gitlab.example.com/group/repo/-/merge_requests/42|MR !42>"
    assert slack_notifier.parse_mr_url(text) == ("group/repo", 42)


def test_parse_trailing_path_after_iid():
    text = "https://gitlab.example.com/group/repo/-/merge_requests/55/diffs"
    assert slack_notifier.parse_mr_url(text) == ("group/repo", 55)


def test_parse_no_mr_url_returns_none():
    assert slack_notifier.parse_mr_url("https://gitlab.example.com/group/repo") is None
    assert slack_notifier.parse_mr_url("그냥 텍스트, MR 없음") is None
    assert slack_notifier.parse_mr_url("") is None


def test_parse_issue_url_is_not_matched():
    text = "https://gitlab.example.com/group/repo/-/issues/123"
    assert slack_notifier.parse_mr_url(text) is None


def test_enabled_false_without_token(monkeypatch):
    # 모듈 로드 시점 토큰을 비워 enabled()=False 확인 (conftest는 SLACK_* 미설정).
    monkeypatch.setattr(slack_notifier, "SLACK_BOT_TOKEN", "")
    assert slack_notifier.enabled() is False
