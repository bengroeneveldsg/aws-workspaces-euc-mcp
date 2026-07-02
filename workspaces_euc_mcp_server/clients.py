# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Region/profile-aware boto3 client factory.

Clients are built from the standard boto3 credential chain (``AWS_PROFILE`` / ``AWS_REGION`` /
instance role / SSO). For multi-account / MSP use, pass ``role_arn`` (and optional ``external_id``)
to transparently ``sts:AssumeRole`` into another account — the assumed credentials auto-refresh
before expiry, and no tool code changes (every tool just calls ``factory.client(...)``).
"""

from __future__ import annotations

import threading
from typing import Any

import boto3
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session as _botocore_get_session

from . import consts


class ClientFactory:
    """Builds and caches boto3 clients for the in-scope EUC services."""

    def __init__(
        self,
        region: str | None = None,
        profile: str | None = None,
        role_arn: str | None = None,
        external_id: str | None = None,
        role_session_name: str = "workspaces-euc-mcp-server",
    ) -> None:
        self._region = region
        self._profile = profile
        self._role_arn = role_arn
        self._external_id = external_id
        self._role_session_name = role_session_name
        # The base session holds the caller's own credentials (profile / env / SSO / instance role).
        self._base_session = boto3.Session(profile_name=profile, region_name=region)
        # The effective session: the base one, or a cross-account assumed-role one (MSP / multi-
        # account). Assumed credentials auto-refresh before expiry, so clients keep working.
        self._session = self._build_assumed_session(role_arn) if role_arn else self._base_session
        self._cache: dict[tuple[str, str | None], Any] = {}
        # boto3 client *creation* is not thread-safe; cross-service tools fan out on threads.
        self._lock = threading.Lock()

    def _build_assumed_session(self, role_arn: str) -> boto3.Session:
        """Build a boto3 Session backed by auto-refreshing sts:AssumeRole credentials."""

        external_id = self._external_id
        session_name = self._role_session_name

        def _refresh() -> dict[str, str]:
            sts = self._base_session.client("sts", config=self._config())
            kwargs: dict[str, str] = {"RoleArn": role_arn, "RoleSessionName": session_name}
            if external_id:
                kwargs["ExternalId"] = external_id
            creds = sts.assume_role(**kwargs)["Credentials"]
            return {
                "access_key": creds["AccessKeyId"],
                "secret_key": creds["SecretAccessKey"],
                "token": creds["SessionToken"],
                "expiry_time": creds["Expiration"].isoformat(),
            }

        refreshable = RefreshableCredentials.create_from_metadata(
            metadata=_refresh(),
            refresh_using=_refresh,
            method="sts-assume-role",
        )
        botocore_session = _botocore_get_session()
        botocore_session._credentials = refreshable
        if self._region:
            botocore_session.set_config_variable("region", self._region)
        return boto3.Session(botocore_session=botocore_session)

    @property
    def region(self) -> str | None:
        """Effective region (explicit override, else whatever the base session resolved)."""
        return self._region or self._base_session.region_name

    def _config(self) -> Config:
        return Config(
            user_agent_extra=f"{consts.SERVER_NAME}/{consts.SERVER_VERSION}",
            retries={"max_attempts": 3, "mode": "standard"},
        )

    def client(self, service_name: str, region: str | None = None) -> Any:
        """Return a cached boto3 client for ``service_name`` in the target region.

        Typed ``Any`` because boto3 clients are dynamically generated per service.
        """
        target_region = region or self._region
        key = (service_name, target_region)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self._session.client(
                    service_name,
                    region_name=target_region,
                    config=self._config(),
                )
            return self._cache[key]
