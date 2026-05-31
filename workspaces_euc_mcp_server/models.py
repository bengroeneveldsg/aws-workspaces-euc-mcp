# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Pydantic models for tool inputs and synthesized outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServiceInventory(BaseModel):
    """Per-service rollup within an inventory summary."""

    service: str = Field(description="Current official AWS product name.")
    resource_type: str = Field(description="The kind of resource counted (e.g. WorkSpace, Fleet).")
    count: int = Field(description="Number of resources of this type found in the region.")
    by_state: dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown of resources by their lifecycle state.",
    )


class ServiceError(BaseModel):
    """A non-fatal error encountered while calling one AWS service.

    Collection/diagnosis is best-effort: a permission gap or unsupported region for one service is
    recorded here rather than failing the whole result.
    """

    service: str
    operation: str = Field(description="The AWS API operation that failed.")
    message: str


# Backwards-compatible alias (the inventory tool predates the generic name).
InventoryError = ServiceError


class EucInventorySummary(BaseModel):
    """Cross-service inventory of the Amazon WorkSpaces EUC portfolio in one region."""

    region: str | None = Field(description="AWS region the summary covers.")
    account_scope: str = Field(default="single-account")
    total_resources: int = Field(description="Sum of counts across all services collected.")
    services: list[ServiceInventory] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A single observation from a diagnostic tool."""

    severity: str = Field(description="One of: info, warning, critical.")
    title: str = Field(description="Short human-readable headline.")
    detail: str = Field(description="What was observed and why it matters.")
    recommendation: str | None = Field(default=None, description="Suggested next action, if any.")


class Diagnosis(BaseModel):
    """Synthesized diagnosis for a single EUC resource."""

    target_type: str = Field(description="Kind of resource diagnosed (official product name).")
    target_id: str = Field(description="Identifier of the resource diagnosed.")
    region: str | None = None
    status: str = Field(
        description="Overall verdict: healthy, degraded, unhealthy, unknown, or not_found."
    )
    summary: str = Field(description="One-line plain-language verdict.")
    signals: dict[str, object] = Field(
        default_factory=dict,
        description="Key raw signals observed (state, capacity, connection status, etc.).",
    )
    findings: list[Finding] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)


class DirectoryHealthReport(BaseModel):
    """Health of one or more WorkSpaces-registered directories in a region."""

    region: str | None = None
    directories: list[Diagnosis] = Field(default_factory=list)
    errors: list[ServiceError] = Field(default_factory=list)
