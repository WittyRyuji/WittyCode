"""``core.chatgpt.errors``: what the user is told when sign-in or a request fails."""

from __future__ import annotations

import pytest

from desktop.core.chatgpt import errors
from desktop.core.chatgpt.errors import AuthError, ChatGPTError, PlanNotEnabled, friendly_failure


@pytest.mark.parametrize("code", sorted(errors._SIGN_IN_TEXTS))
def test_every_sign_in_failure_code_has_its_own_chinese_sentence(code):
    text = friendly_failure(AuthError("English detail for the log", code=code))
    assert text == errors._SIGN_IN_TEXTS[code]
    assert "English detail" not in text and any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_the_codes_the_oauth_module_raises_are_all_worded():
    import re
    from pathlib import Path

    folder = Path(errors.__file__).parent
    source = (folder / "oauth.py").read_text(encoding="utf-8") + (folder / "accounts.py").read_text(encoding="utf-8")
    raised = set(re.findall(r'code="([a-z_]+)"', source))
    # (codes of OpenAI's own answers and of the network are worded elsewhere)
    ours = raised - {"network", "plan_not_enabled", "invalid_grant", "invalid_client"}
    assert not (ours - set(errors._SIGN_IN_TEXTS)), f"sign-in codes without a sentence: {sorted(ours - set(errors._SIGN_IN_TEXTS))}"


def test_a_plan_that_was_not_allowed_says_how_to_allow_it():
    assert "重新登录" in friendly_failure(PlanNotEnabled("scope missing"))


def test_a_dead_sign_in_asks_for_a_new_one_and_a_live_one_shows_the_reason():
    assert "重新登录" in friendly_failure(AuthError("tokens gone", terminal=True))
    assert "weird" in friendly_failure(AuthError("weird thing"))


def test_the_documented_usage_codes_each_say_what_to_do():
    for code in ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible", "subscription_sharing_usage_unavailable"):
        assert friendly_failure(ChatGPTError("x", code=code)) == errors._TEXTS[code]
    assert "用量" in friendly_failure(ChatGPTError("x", code="subscription_sharing_usage_limit_exceeded"))


def test_other_failures_are_worded_by_their_status_and_the_network():
    assert "连不上 OpenAI" in friendly_failure(ChatGPTError("timed out", code="network"))
    assert "重新登录" in friendly_failure(ChatGPTError("no", status=401))
    assert "地区或权限" in friendly_failure(ChatGPTError("blocked", status=403))
    assert "稍后再试" in friendly_failure(ChatGPTError("slow down", status=429))
    assert "HTTP 503" in friendly_failure(ChatGPTError("down", status=503))
    assert friendly_failure(ChatGPTError("something specific")) == "something specific"
    assert friendly_failure(RuntimeError("boom")) == "RuntimeError: boom"


def test_the_message_for_the_log_carries_the_facts_openai_asks_for():
    text = str(ChatGPTError("limit", code="subscription_sharing_usage_limit_exceeded", status=429, request_id="req_9"))
    assert "429" in text and "req_9" in text and "subscription_sharing_usage_limit_exceeded" in text


def test_the_wait_the_server_asks_for_is_kept_in_seconds_whatever_unit_it_names():
    from desktop.core.chatgpt.errors import error_from_body

    assert error_from_body(429, b"{}", {"retry-after-ms": "1500"}).retry_after == 1.5
    assert error_from_body(429, b"{}", {"retry-after": "3"}).retry_after == 3.0
    assert error_from_body(429, b"{}", {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}).retry_after is None
    assert error_from_body(429, b"{}", {}).retry_after is None


def test_a_message_that_aws_capitalises_is_read_too():
    from desktop.core.chatgpt.errors import error_from_body

    assert error_from_body(403, b'{"Message": "Invalid API Key format"}', {}).message == "Invalid API Key format"


def test_aws_names_its_error_and_its_request_in_headers():
    from desktop.core.chatgpt.errors import error_from_body

    error = error_from_body(400, b'{"message": "bad"}', {"x-amzn-errortype": "ValidationException:http://internal.amazon.com/coral/", "x-amzn-requestid": "r-1"})
    assert (error.code, error.request_id, error.message) == ("ValidationException", "r-1", "bad")
    assert error_from_body(400, b'{"error": {"code": "own_code", "message": "bad"}}', {"x-amzn-errortype": "Other"}).code == "own_code", "a code in the body is the better one"
