# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Tests for opt-in SSO auto-login and its integration with try_call."""

from __future__ import annotations

from botocore.exceptions import BotoCoreError

from workspaces_euc_mcp_server.models import ServiceError
from workspaces_euc_mcp_server.sso import SsoAutoLogin, looks_like_sso_token_error
from workspaces_euc_mcp_server.tools import _common


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_detects_sso_token_errors():
    assert looks_like_sso_token_error(Exception("Token has expired and refresh failed"))
    assert looks_like_sso_token_error(Exception("Error when retrieving token from sso: ..."))
    assert not looks_like_sso_token_error(Exception("AccessDenied"))


def test_disabled_handler_never_logs_in():
    calls: list[str | None] = []
    h = SsoAutoLogin(enabled=False, runner=lambda p: calls.append(p) or "ran")
    assert h.maybe_login() is None
    assert calls == []


def test_enabled_handler_debounces_burst_then_allows_after_cooldown():
    clock = _Clock()
    calls: list[str | None] = []
    h = SsoAutoLogin(
        enabled=True,
        profile="ben-euc",
        cooldown_seconds=60.0,
        runner=lambda p: calls.append(p) or "opened browser",
        clock=clock,
    )
    # First failure triggers a login...
    assert h.maybe_login() == "opened browser"
    # ...a burst within the cooldown does NOT open another browser.
    assert h.maybe_login() is None
    assert h.maybe_login() is None
    # ...but after the cooldown elapses it triggers again.
    clock.t += 61.0
    assert h.maybe_login() == "opened browser"
    assert calls == ["ben-euc", "ben-euc"]


def test_try_call_triggers_auto_login_on_token_error():
    triggered: list[str | None] = []
    handler = SsoAutoLogin(
        enabled=True, profile="p", runner=lambda p: triggered.append(p) or "opened sign-in"
    )
    _common.register_sso_handler(handler)
    try:
        errors: list[ServiceError] = []

        def boom():
            raise BotoCoreError(error="Token has expired and refresh failed")

        # BotoCoreError formats its message from the template; force a token-like message.
        def boom2():
            raise _FakeTokenError("Token has expired and refresh failed")

        _common.try_call(errors, "svc", "op", boom2, default=None)

        assert triggered == ["p"]  # browser sign-in launched
        assert len(errors) == 1
        assert "SSO session expired" in errors[0].message
        assert "opened sign-in" in errors[0].message
    finally:
        _common.register_sso_handler(None)


def test_try_call_hint_when_auto_login_disabled():
    _common.register_sso_handler(None)
    errors: list[ServiceError] = []

    def boom():
        raise _FakeTokenError("the sso session associated with this profile has expired")

    _common.try_call(errors, "svc", "op", boom, default=None)
    assert "aws sso login" in errors[0].message
    assert "Console does NOT refresh" in errors[0].message


def test_create_server_enables_sso_auto_login_by_default():
    from workspaces_euc_mcp_server.server import create_server

    try:
        create_server(region="us-east-1")
        assert _common._SSO_HANDLER is not None
        assert _common._SSO_HANDLER.enabled is True
        # Opt-out still works.
        create_server(region="us-east-1", sso_auto_login=False)
        assert _common._SSO_HANDLER is None
    finally:
        _common.register_sso_handler(None)


class _FakeTokenError(BotoCoreError):
    """A BotoCoreError subclass whose str() is a controllable token-error message."""

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message
