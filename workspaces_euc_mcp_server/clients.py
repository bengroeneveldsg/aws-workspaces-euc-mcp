# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Region/profile-aware boto3 client factory.

Single-account today: clients are built from the standard boto3 credential chain
(``AWS_PROFILE`` / ``AWS_REGION`` / instance role / SSO). The ``role_arn`` parameter is reserved
so cross-account ``sts:AssumeRole`` (MSP / multi-account) can be added later inside this factory
without changing any tool code.
"""

from __future__ import annotations

import boto3
from botocore.config import Config

from . import consts


class ClientFactory:
    """Builds and caches boto3 clients for the in-scope EUC services."""

    def __init__(
        self,
        region: str | None = None,
        profile: str | None = None,
        role_arn: str | None = None,
    ) -> None:
        self._region = region
        self._profile = profile
        self._role_arn = role_arn  # Reserved for future multi-account support.
        self._session = boto3.Session(profile_name=profile, region_name=region)
        self._cache: dict[tuple[str, str | None], object] = {}

    @property
    def region(self) -> str | None:
        """Effective region (explicit override, else whatever the session resolved)."""
        return self._region or self._session.region_name

    def _config(self) -> Config:
        return Config(
            user_agent_extra=f"{consts.SERVER_NAME}/{consts.SERVER_VERSION}",
            retries={"max_attempts": 3, "mode": "standard"},
        )

    def client(self, service_name: str, region: str | None = None):
        """Return a cached boto3 client for ``service_name`` in the target region."""
        target_region = region or self._region
        key = (service_name, target_region)
        if key not in self._cache:
            self._cache[key] = self._session.client(
                service_name,
                region_name=target_region,
                config=self._config(),
            )
        return self._cache[key]
