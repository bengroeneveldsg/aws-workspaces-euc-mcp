# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Opt-in AWS SSO auto-login.

When enabled (``--sso-auto-login``), and a tool call fails because the SSO token has expired, the
server launches ``aws sso login`` for the configured profile — which opens the user's browser to
the approval screen — so the user never has to drop to a terminal. The interactive browser approval
itself is still required (that is inherent to the OAuth flow). The server never stores credentials;
it only invokes the AWS CLI, which writes to the standard SSO token cache.
"""

from __future__ import annotations

import os
import re
import shutil

# `subprocess` is used solely to launch `aws sso login` with a fixed argv (shell=False) below.
import subprocess  # nosec B404
import time
from collections.abc import Callable

from loguru import logger

# Marker phrases (lowercased) that indicate an expired / unretrievable SSO token.
_SSO_TOKEN_ERROR_MARKERS = (
    "token has expired",
    "expired and refresh failed",
    "error when retrieving token from sso",
    "ssotokenload",
    "unauthorizedssotoken",
    "the sso session associated with this profile has expired",
    "sso session",
    "tokenretrievalerror",
)

# Conservative profile-name charset so a configured profile can never be argv-injected.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.:@/-]+$")


def looks_like_sso_token_error(exc: BaseException) -> bool:
    """True if an exception message looks like an expired/unretrievable SSO token."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _SSO_TOKEN_ERROR_MARKERS)


def _default_runner(profile: str | None) -> str:
    """Launch `aws sso login` (opens the browser). Returns a human-readable status string."""
    aws = shutil.which("aws")
    if not aws:
        return "AWS CLI not found on PATH — run `aws sso login` manually to re-authenticate."
    args = [aws, "sso", "login"]
    if profile:
        if not _PROFILE_RE.match(profile):
            return "the configured AWS profile name is invalid — sign in manually."
        args += ["--profile", profile]
    try:
        # Fixed argv, shell=False, profile validated against _PROFILE_RE — no injection surface.
        subprocess.Popen(  # nosec B603
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:  # pragma: no cover - environment dependent
        return f"could not launch `aws sso login`: {exc}"
    suffix = f" --profile {profile}" if profile else ""
    return f"opened browser sign-in (`aws sso login{suffix}`) — approve it, then re-run."


class SsoAutoLogin:
    """Debounced launcher for `aws sso login`, triggered on detected token expiry."""

    def __init__(
        self,
        *,
        profile: str | None = None,
        enabled: bool = False,
        cooldown_seconds: float = 60.0,
        runner: Callable[[str | None], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.enabled = enabled
        self._profile = profile
        self._cooldown = cooldown_seconds
        self._runner = runner or _default_runner
        self._clock = clock or time.monotonic
        self._last: float | None = None

    def maybe_login(self) -> str | None:
        """Trigger a login if enabled and not within the cooldown; returns a status or None."""
        if not self.enabled:
            return None
        now = self._clock()
        if self._last is not None and (now - self._last) < self._cooldown:
            # A burst of failing calls (e.g. one report) should open the browser only once.
            return None
        self._last = now
        profile = self._profile or os.environ.get("AWS_PROFILE")
        status = self._runner(profile)
        logger.info("SSO auto-login triggered: {}", status)
        return status
