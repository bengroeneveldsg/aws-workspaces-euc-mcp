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


class InventoryError(BaseModel):
    """A non-fatal error encountered while collecting one service's inventory.

    Collection is best-effort: a permission gap or unsupported region for one service is recorded
    here rather than failing the whole summary.
    """

    service: str
    operation: str = Field(description="The AWS API operation that failed.")
    message: str


class EucInventorySummary(BaseModel):
    """Cross-service inventory of the Amazon WorkSpaces EUC portfolio in one region."""

    region: str | None = Field(description="AWS region the summary covers.")
    account_scope: str = Field(default="single-account")
    total_resources: int = Field(description="Sum of counts across all services collected.")
    services: list[ServiceInventory] = Field(default_factory=list)
    errors: list[InventoryError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
