# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Access review for WorkSpaces Applications (read-only, IAM Tier 0).

``review_application_access`` answers "who has access to what": user-pool users (status, auth
type), and which users/groups are assigned to each stack. SAML/OIDC-federated users authenticate
through the IdP and are not enumerable here — the report says so rather than implying completeness.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from .. import consts
from ..clients import ClientFactory
from ..models import ServiceError
from ._common import gather_concurrently, paginate, read_only, try_call


class ApplicationUser(BaseModel):
    """A WorkSpaces Applications user-pool user."""

    user_name: str
    enabled: bool | None = None
    status: str | None = None
    authentication_type: str | None = None
    created: str | None = None


class ApplicationAccessReport(BaseModel):
    """Who has access to WorkSpaces Applications stacks."""

    region: str | None = None
    user_count: int = 0
    users: list[ApplicationUser] = Field(default_factory=list)
    stack_assignments: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Stack name -> user names assigned via user-stack associations.",
    )
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def review_application_access_core(
    factory: ClientFactory, region: str | None
) -> ApplicationAccessReport:
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    product = consts.PRODUCT_WORKSPACES_APPLICATIONS

    def _users() -> tuple[list[ApplicationUser], list[ServiceError]]:
        errors: list[ServiceError] = []
        raw = (
            try_call(
                errors,
                product,
                "DescribeUsers",
                lambda: paginate(appstream.describe_users, "Users", AuthenticationType="USERPOOL"),
                default=[],
            )
            or []
        )
        users = [
            ApplicationUser(
                user_name=u.get("UserName", ""),
                enabled=u.get("Enabled"),
                status=u.get("Status"),
                authentication_type=u.get("AuthenticationType"),
                created=u["CreatedTime"].isoformat() if u.get("CreatedTime") else None,
            )
            for u in raw
        ]
        return users, errors

    def _assignments() -> tuple[dict[str, list[str]], list[ServiceError]]:
        errors: list[ServiceError] = []
        stacks = (
            try_call(
                errors,
                product,
                "DescribeStacks",
                lambda: paginate(appstream.describe_stacks, "Stacks"),
                default=[],
            )
            or []
        )
        assignments: dict[str, list[str]] = {}
        for stack in stacks:
            name = stack.get("Name", "")
            associations = (
                try_call(
                    errors,
                    product,
                    "DescribeUserStackAssociations",
                    lambda name=name: paginate(
                        appstream.describe_user_stack_associations,
                        "UserStackAssociations",
                        StackName=name,
                    ),
                    default=[],
                )
                or []
            )
            assignments[name] = sorted(
                a.get("UserName", "") for a in associations if a.get("UserName")
            )
        return assignments, errors

    (users, user_errors), (assignments, assignment_errors) = gather_concurrently(
        _users, _assignments
    )

    return ApplicationAccessReport(
        region=region,
        user_count=len(users),
        users=sorted(users, key=lambda u: u.user_name),
        stack_assignments=assignments,
        errors=[*user_errors, *assignment_errors],
        notes=[
            "Users cover the WorkSpaces Applications USERPOOL only; SAML/OIDC-federated users "
            "authenticate via the IdP and are not enumerable through this API.",
            "stack_assignments lists explicit user-stack associations; stacks reached via "
            "federation may show no assignments here.",
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register access-review tools on the FastMCP app."""

    async def review_application_access(region: str | None = None) -> dict[str, Any]:
        """Review who has access to WorkSpaces Applications (formerly AppStream 2.0).

        Lists user-pool users (status, enabled, auth type) and which users are assigned to each
        stack — an access review for the streaming estate. SAML/OIDC-federated users are handled
        by your IdP and are not enumerable here (noted in the result). Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = await asyncio.to_thread(
            review_application_access_core, factory, region or factory.region
        )
        return report.model_dump()

    mcp.add_tool(review_application_access, annotations=read_only("Review Applications access"))
