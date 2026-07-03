# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""WorkSpaces Applications (AppStream 2.0) image audit (read-only, IAM Tier 0).

``audit_application_images`` inventories the account's own (PRIVATE) and SHARED images plus image
builders, and raises admin/security-relevant findings: stale base images (unpatched OS), pinned/old
AppStream agents, non-AVAILABLE/error states, cross-account SHARED visibility, and — notably —
image builders left RUNNING (which incur cost and are an interactive admin surface).

PUBLIC AWS-provided base images are intentionally skipped (hundreds of them, not the customer's to
audit).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import (
    ApplicationImageAuditReport,
    ApplicationImageBuilderInfo,
    ApplicationImageFinding,
    ApplicationImageInfo,
    ServiceError,
    WorkspaceImageAuditReport,
    WorkspaceImageInfo,
)
from . import pricing
from ._common import paginate, read_only, try_call

# An image whose base was released more than this many days ago likely lacks recent OS patches.
_STALE_BASE_DAYS = 180


def _age_days(value: Any) -> int | None:
    """Whole days between a boto3 datetime and now (UTC); None if unparseable."""
    if not isinstance(value, datetime):
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).days


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _image_info(image: dict[str, Any]) -> ApplicationImageInfo:
    perms = image.get("ImagePermissions") or {}
    apps = [
        a.get("Name", "")
        for a in image.get("Applications", [])
        if a.get("Enabled", True) and a.get("Name")
    ]
    return ApplicationImageInfo(
        name=image.get("Name", "unknown"),
        visibility=image.get("Visibility"),
        platform=image.get("Platform"),
        state=image.get("State"),
        agent_version=image.get("AppstreamAgentVersion"),
        application_count=len(image.get("Applications", [])),
        applications=apps,
        error_count=len(image.get("ImageErrors", [])),
        created=_iso(image.get("CreatedTime")),
        base_image_released=_iso(image.get("PublicBaseImageReleasedDate")),
        base_image_age_days=_age_days(image.get("PublicBaseImageReleasedDate")),
        allow_fleet=perms.get("allowFleet"),
        allow_image_builder=perms.get("allowImageBuilder"),
    )


def _image_findings(info: ApplicationImageInfo) -> list[ApplicationImageFinding]:
    findings: list[ApplicationImageFinding] = []
    if info.state and info.state != "AVAILABLE":
        findings.append(
            ApplicationImageFinding(
                target=info.name, severity="warning", issue=f"Image state is {info.state}."
            )
        )
    if info.error_count:
        findings.append(
            ApplicationImageFinding(
                target=info.name,
                severity="warning",
                issue=f"Image has {info.error_count} reported error(s).",
            )
        )
    if info.agent_version and info.agent_version.upper() != "LATEST":
        findings.append(
            ApplicationImageFinding(
                target=info.name,
                severity="warning",
                issue=(
                    f"AppStream agent is pinned to {info.agent_version}, not LATEST; pinned agents "
                    "miss security/feature updates — rebuild to refresh."
                ),
            )
        )
    if info.base_image_age_days is not None and info.base_image_age_days > _STALE_BASE_DAYS:
        findings.append(
            ApplicationImageFinding(
                target=info.name,
                severity="warning",
                issue=(
                    f"Built on a base image released {info.base_image_age_days} days ago; rebuild "
                    "on a newer base to pick up OS security patches."
                ),
            )
        )
    if info.visibility == "SHARED":
        findings.append(
            ApplicationImageFinding(
                target=info.name,
                severity="info",
                issue="Image is SHARED (cross-account) — review whether the sharing is intended.",
            )
        )
    return findings


def audit_application_images_core(
    factory: ClientFactory, region: str | None
) -> ApplicationImageAuditReport:
    errors: list[ServiceError] = []
    appstream = factory.client(consts.APPSTREAM_API, region=region)
    product = consts.PRODUCT_WORKSPACES_APPLICATIONS

    # The account's own images (PRIVATE) and any shared in (SHARED). PUBLIC base images are skipped.
    raw_images: list[dict[str, Any]] = []
    for image_type in ("PRIVATE", "SHARED"):
        raw_images.extend(
            try_call(
                errors,
                product,
                "DescribeImages",
                lambda image_type=image_type: paginate(
                    appstream.describe_images, "Images", Type=image_type
                ),
                default=[],
            )
            or []
        )

    builders_raw = (
        try_call(
            errors,
            product,
            "DescribeImageBuilders",
            lambda: paginate(appstream.describe_image_builders, "ImageBuilders"),
            default=[],
        )
        or []
    )

    app_block_builders_raw = (
        try_call(
            errors,
            product,
            "DescribeAppBlockBuilders",
            lambda: paginate(appstream.describe_app_block_builders, "AppBlockBuilders"),
            default=[],
        )
        or []
    )

    images = [_image_info(img) for img in raw_images]
    findings: list[ApplicationImageFinding] = []
    for info in images:
        findings.extend(_image_findings(info))

    builders: list[ApplicationImageBuilderInfo] = []
    running = 0
    for b in builders_raw:
        builders.append(
            ApplicationImageBuilderInfo(
                name=b.get("Name", "unknown"),
                state=b.get("State"),
                platform=b.get("Platform"),
                instance_type=b.get("InstanceType"),
                agent_version=b.get("AppstreamAgentVersion"),
                created=_iso(b.get("CreatedTime")),
            )
        )
        if b.get("State") == "RUNNING":
            running += 1
            rate = pricing.appstream_hourly_price(
                factory, region, b.get("InstanceType"), "ImageBuilder", b.get("Platform")
            )
            cost_note = (
                f" Current list rate: ${rate:.3f}/hr (~${rate * 730:,.0f}/mo if left running)."
                if rate
                else ""
            )
            findings.append(
                ApplicationImageFinding(
                    target=b.get("Name", "unknown"),
                    severity="warning",
                    issue=(
                        "Image builder is RUNNING — it bills per hour and is an interactive admin "
                        "surface; stop it when not actively building an image." + cost_note
                    ),
                )
            )

    app_block_builders: list[ApplicationImageBuilderInfo] = []
    abb_running = 0
    for b in app_block_builders_raw:
        app_block_builders.append(
            ApplicationImageBuilderInfo(
                name=b.get("Name", "unknown"),
                state=b.get("State"),
                platform=b.get("Platform"),
                instance_type=b.get("InstanceType"),
                created=_iso(b.get("CreatedTime")),
            )
        )
        if b.get("State") == "RUNNING":
            abb_running += 1
            rate = pricing.appstream_hourly_price(
                factory, region, b.get("InstanceType"), "AppBlockBuilder", b.get("Platform")
            )
            cost_note = (
                f" Current list rate: ${rate:.3f}/hr (~${rate * 730:,.0f}/mo if left running)."
                if rate
                else ""
            )
            findings.append(
                ApplicationImageFinding(
                    target=b.get("Name", "unknown"),
                    severity="warning",
                    issue=(
                        "App block builder is RUNNING — it bills per hour like an image builder; "
                        "stop it when not actively packaging an app block." + cost_note
                    ),
                )
            )

    return ApplicationImageAuditReport(
        region=region,
        image_count=len(images),
        image_builder_count=len(builders),
        running_image_builders=running,
        app_block_builder_count=len(app_block_builders),
        running_app_block_builders=abb_running,
        app_block_builders=sorted(app_block_builders, key=lambda b: b.name),
        images=sorted(images, key=lambda i: i.name),
        image_builders=sorted(builders, key=lambda b: b.name),
        findings=findings,
        errors=errors,
        notes=[
            "Covers PRIVATE (your own) and SHARED images plus image builders; PUBLIC AWS base "
            "images are excluded.",
            f"'Stale base' flags an image whose base was released more than {_STALE_BASE_DAYS} "
            "days ago — rebuild to inherit current OS patches.",
        ],
    )


def audit_workspace_images_core(
    factory: ClientFactory, region: str | None
) -> WorkspaceImageAuditReport:
    """Audit WorkSpaces Personal custom images: state, age, and cross-account sharing."""
    errors: list[ServiceError] = []
    workspaces = factory.client(consts.WORKSPACES_API, region=region)

    raw = (
        try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaceImages",
            lambda: paginate(workspaces.describe_workspace_images, "Images"),
            default=[],
        )
        or []
    )

    images: list[WorkspaceImageInfo] = []
    findings: list[ApplicationImageFinding] = []
    for img in raw:
        image_id = img.get("ImageId", "")
        perms = try_call(
            errors,
            consts.PRODUCT_WORKSPACES_PERSONAL,
            "DescribeWorkspaceImagePermissions",
            lambda image_id=image_id: paginate(
                workspaces.describe_workspace_image_permissions,
                "ImagePermissions",
                ImageId=image_id,
            ),
            default=[],
        )
        shared = [p.get("SharedAccountId", "") for p in (perms or []) if p.get("SharedAccountId")]
        created = img.get("Created")
        info = WorkspaceImageInfo(
            image_id=image_id,
            name=img.get("Name"),
            state=img.get("State"),
            operating_system=(img.get("OperatingSystem") or {}).get("Type"),
            created=_iso(created),
            age_days=_age_days(created),
            owner_account=img.get("OwnerAccountId"),
            shared_with_accounts=shared,
        )
        images.append(info)
        label = info.name or image_id
        if info.state == "ERROR":
            findings.append(
                ApplicationImageFinding(
                    target=label, severity="warning", issue="Image is in ERROR state."
                )
            )
        if shared:
            findings.append(
                ApplicationImageFinding(
                    target=label,
                    severity="info",
                    issue=f"Image is shared with {len(shared)} account(s): {', '.join(shared)} "
                    "— review whether the sharing is intended.",
                )
            )
        if info.age_days is not None and info.age_days > _STALE_BASE_DAYS:
            findings.append(
                ApplicationImageFinding(
                    target=label,
                    severity="info",
                    issue=f"Image was created {info.age_days} days ago; consider refreshing it "
                    "so new WorkSpaces launch with current OS patches.",
                )
            )

    return WorkspaceImageAuditReport(
        region=region,
        image_count=len(images),
        images=sorted(images, key=lambda i: i.name or i.image_id),
        findings=findings,
        errors=errors,
        notes=[
            "Covers the account's own WorkSpaces Personal custom images. Age is time since image "
            "creation — unlike WorkSpaces Applications images there is no public-base-release "
            "signal, so treat age as a refresh prompt rather than a patch-level fact.",
        ],
    )


def register(mcp: Any, factory: ClientFactory) -> None:
    """Register the WorkSpaces Applications image-audit tool on the FastMCP app."""

    async def audit_application_images(region: str | None = None) -> dict[str, Any]:
        """Audit WorkSpaces Applications (AppStream 2.0) images and image builders.

        Inventories your PRIVATE and SHARED images (skipping PUBLIC AWS base images) plus image
        builders, and raises admin/security findings: stale base images (likely unpatched OS),
        pinned/old AppStream agents, non-AVAILABLE or errored images, SHARED (cross-account)
        visibility, and image builders left RUNNING (cost + interactive admin surface). Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = audit_application_images_core(factory, region or factory.region)
        return report.model_dump()

    async def audit_workspace_images(region: str | None = None) -> dict[str, Any]:
        """Audit WorkSpaces Personal custom images (state, age, cross-account sharing).

        Lists the account's own WorkSpaces Personal images with state, OS, creation age, and which
        accounts each image is shared with; flags ERROR-state images, cross-account sharing, and
        aging images worth refreshing. For WorkSpaces Applications (AppStream) images use
        audit_application_images. Read-only.

        Args:
            region: AWS region. Defaults to the server's configured region.
        """
        report = await asyncio.to_thread(
            audit_workspace_images_core, factory, region or factory.region
        )
        return report.model_dump()

    mcp.add_tool(
        audit_application_images, annotations=read_only("Audit WorkSpaces Applications images")
    )
    mcp.add_tool(audit_workspace_images, annotations=read_only("Audit WorkSpaces Personal images"))
