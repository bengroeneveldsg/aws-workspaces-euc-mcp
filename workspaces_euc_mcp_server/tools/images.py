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
)
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
            findings.append(
                ApplicationImageFinding(
                    target=b.get("Name", "unknown"),
                    severity="warning",
                    issue=(
                        "Image builder is RUNNING — it bills per hour and is an interactive admin "
                        "surface; stop it when not actively building an image."
                    ),
                )
            )

    return ApplicationImageAuditReport(
        region=region,
        image_count=len(images),
        image_builder_count=len(builders),
        running_image_builders=running,
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

    mcp.add_tool(
        audit_application_images, annotations=read_only("Audit WorkSpaces Applications images")
    )
