"""The live run's safety guards. The run itself (python -m app.e2e) never happens in CI."""

import pytest

from app.e2e import Refused, assert_sandbox, looks_like_challenge, page_allowed, scrub


def test_sandbox_config_passes():
    assert_sandbox({"env": "sandbox", "sdkUrl": "https://www.sandbox.paypal.com/web-sdk/v6/core"})


@pytest.mark.parametrize("config", [
    {"env": "live", "sdkUrl": "https://www.sandbox.paypal.com/web-sdk/v6/core"},
    {"env": "sandbox", "sdkUrl": "https://www.paypal.com/web-sdk/v6/core"},
    {"env": "sandbox", "sdkUrl": "https://sandbox.paypal.com.evil.example/web-sdk"},
    {"env": "sandbox"},
    {},
])
def test_anything_else_refuses_to_run(config):
    with pytest.raises(Refused):
        assert_sandbox(config)


@pytest.mark.parametrize("url, allowed", [
    ("https://www.sandbox.paypal.com/signin", True),
    ("https://sandbox.paypal.com/", True),
    ("https://www.paypal.com/signin", False),
    ("https://paypal.com/", False),
    ("https://api-m.paypal.com/v1", False),
    ("https://evilsandbox.paypal.com/", False),
    ("https://www.sandbox.paypal.com.evil.example/", True),  # not PayPal at all, so not this guard's job
    ("https://clearline-cbyb.onrender.com/seller", True),
])
def test_only_sandbox_paypal_pages_are_allowed(url, allowed):
    assert page_allowed(url) is allowed


@pytest.mark.parametrize("url, body, expected", [
    ("https://www.sandbox.paypal.com/checkoutnow", "Pay with PayPal", None),
    ("https://www.sandbox.paypal.com/authflow/challenges/x/challenge", "", "challenge page"),
    ("https://www.sandbox.paypal.com/signin", "Security Challenge", "security challenge"),
    ("https://www.sandbox.paypal.com/signin", "Enter the code we sent to your phone", "enter the code we sent"),
])
def test_challenges_are_recognised(url, body, expected):
    assert looks_like_challenge(url, body) == expected


def test_secrets_never_reach_the_report():
    assert scrub("login failed for a@b.c with hunter2", ["a@b.c", "hunter2"]) == "login failed for *** with ***"
