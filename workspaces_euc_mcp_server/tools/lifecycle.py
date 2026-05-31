# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Guarded lifecycle (write) tools for WorkSpaces Personal, Pools, and Applications — Phase 2,
IAM Tier 2.

These tools are only registered when the server is launched with ``--enable-writes``. Every action
is **safe by default**:

1. **Dry-run by default** — without ``confirm=True`` the tool changes nothing and returns the plan.
2. **Blast-radius cap** — a confirmed bulk action is refused if it targets more than
   ``--max-bulk-targets`` resources.
3. **Least privilege** — only the specific power/running-mode actions, gated by IAM Tier 2.

Destructive operations (terminate/rebuild/restore) are intentionally NOT here; they belong to a
separate, separately-gated module (``--enable-destructive``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import ServiceError, TargetResult, WriteOutcome
from ._common import try_call

_VALID_RUNNING_MODES = {"AUTO_STOP", "ALWAYS_ON"}

# action -> (boto3 method, per-request key) for the batch power operations.
_BATCH_POWER_ACTIONS = {
    "start": ("start_workspaces", "StartWorkspaceRequests"),
    "stop": ("stop_workspaces", "StopWorkspaceRequests"),
    "reboot": ("reboot_workspaces", "RebootWorkspaceRequests"),
}


def _refused_for_blast_radius(
    action: str, workspace_ids: list[str], max_bulk_targets: int
) -> WriteOutcome:
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=workspace_ids,
        max_bulk_targets=max_bulk_targets,
        blast_radius_ok=False,
        plan=(
            f"Refused: {len(workspace_ids)} targets exceed the blast-radius cap of "
            f"{max_bulk_targets}. Re-run with fewer targets or raise --max-bulk-targets."
        ),
        notes=["No changes were made."],
    )


def _dry_run_outcome(
    action: str, workspace_ids: list[str], max_bulk_targets: int, detail: str
) -> WriteOutcome:
    return WriteOutcome(
        action=action,
        dry_run=True,
        confirmed=False,
        requested_targets=workspace_ids,
        max_bulk_targets=max_bulk_targets,
        blast_radius_ok=len(workspace_ids) <= max_bulk_targets,
        plan=detail,
        results=[
            TargetResult(target_id=wid, status="skipped", message="dry run")
            for wid in workspace_ids
        ],
        notes=["Dry run — nothing was changed. Re-run with confirm=true to execute."],
    )


def batch_power_action_core(
    factory: ClientFactory,
    region: str | None,
    action: str,
    workspace_ids: list[str],
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    method_name, request_key = _BATCH_POWER_ACTIONS[action]
    detail = f"{action.capitalize()} {len(workspace_ids)} WorkSpace(s): {', '.join(workspace_ids)}"

    if not workspace_ids:
        return WriteOutcome(
            action=action,
            dry_run=not confirm,
            confirmed=confirm,
            requested_targets=[],
            max_bulk_targets=max_bulk_targets,
            blast_radius_ok=True,
            plan="No target WorkSpaces were provided.",
        )

    if not confirm:
        return _dry_run_outcome(action, workspace_ids, max_bulk_targets, f"Would {detail.lower()}")

    if len(workspace_ids) > max_bulk_targets:
        return _refused_for_blast_radius(action, workspace_ids, max_bulk_targets)

    errors: list[ServiceError] = []
    client = factory.client(consts.WORKSPACES_API, region=region)
    requests = [{"WorkspaceId": wid} for wid in workspace_ids]
    response = try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        method_name,
        lambda: getattr(client, method_name)(**{request_key: requests}),
        default={},
    )
    failed = {fr.get("WorkspaceId"): fr for fr in (response or {}).get("FailedRequests", [])}
    results = [
        TargetResult(
            target_id=wid,
            status="error" if wid in failed else "ok",
            message=failed.get(wid, {}).get("ErrorMessage") if wid in failed else None,
        )
        for wid in workspace_ids
    ]
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=workspace_ids,
        max_bulk_targets=max_bulk_targets,
        blast_radius_ok=True,
        plan=f"{detail} (executed).",
        results=results,
        errors=errors,
    )


def modify_running_mode_core(
    factory: ClientFactory,
    region: str | None,
    workspace_id: str,
    running_mode: str,
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    action = "modify_running_mode"
    running_mode = running_mode.upper()
    if running_mode not in _VALID_RUNNING_MODES:
        return WriteOutcome(
            action=action,
            dry_run=True,
            confirmed=False,
            requested_targets=[workspace_id],
            max_bulk_targets=max_bulk_targets,
            blast_radius_ok=True,
            plan=f"Invalid running mode '{running_mode}'; use {sorted(_VALID_RUNNING_MODES)}.",
            notes=["No changes were made."],
        )

    detail = f"Set running mode of {workspace_id} to {running_mode}"
    if not confirm:
        return _dry_run_outcome(action, [workspace_id], max_bulk_targets, f"Would {detail.lower()}")

    errors: list[ServiceError] = []
    client = factory.client(consts.WORKSPACES_API, region=region)
    try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "ModifyWorkspaceProperties",
        lambda: client.modify_workspace_properties(
            WorkspaceId=workspace_id,
            WorkspaceProperties={"RunningMode": running_mode},
        ),
        default={},
    )
    status = "error" if errors else "ok"
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=[workspace_id],
        max_bulk_targets=max_bulk_targets,
        blast_radius_ok=True,
        plan=f"{detail} (executed)." if not errors else f"{detail} (failed).",
        results=[
            TargetResult(
                target_id=workspace_id,
                status=status,
                message=errors[0].message if errors else None,
            )
        ],
        errors=errors,
    )


def _single_target_outcome(
    action: str,
    target_id: str,
    confirm: bool,
    max_bulk_targets: int,
    detail: str,
    service: str,
    operation: str,
    execute: Callable[[], Any],
) -> WriteOutcome:
    """Run a guarded single-target write (start/stop/update of one pool or fleet)."""
    if not confirm:
        return _dry_run_outcome(action, [target_id], max_bulk_targets, f"Would {detail.lower()}")

    errors: list[ServiceError] = []
    try_call(errors, service, operation, execute, default={})
    status = "error" if errors else "ok"
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=[target_id],
        max_bulk_targets=max_bulk_targets,
        blast_radius_ok=True,
        plan=f"{detail} (executed)." if not errors else f"{detail} (failed).",
        results=[
            TargetResult(
                target_id=target_id,
                status=status,
                message=errors[0].message if errors else None,
            )
        ],
        errors=errors,
    )


def pool_power_action_core(
    factory: ClientFactory,
    region: str | None,
    action: str,
    pool_id: str,
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    method_name = "start_workspaces_pool" if action == "start_pool" else "stop_workspaces_pool"
    operation = "StartWorkspacesPool" if action == "start_pool" else "StopWorkspacesPool"
    detail = f"{'Start' if action == 'start_pool' else 'Stop'} WorkSpaces Pool {pool_id}"
    client = factory.client(consts.WORKSPACES_API, region=region)
    return _single_target_outcome(
        action,
        pool_id,
        confirm,
        max_bulk_targets,
        detail,
        consts.PRODUCT_WORKSPACES_POOLS,
        operation,
        lambda: getattr(client, method_name)(PoolId=pool_id),
    )


def update_pool_capacity_core(
    factory: ClientFactory,
    region: str | None,
    pool_id: str,
    desired_user_sessions: int,
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    action = "update_pool_capacity"
    if desired_user_sessions < 0:
        return WriteOutcome(
            action=action,
            dry_run=True,
            confirmed=False,
            requested_targets=[pool_id],
            max_bulk_targets=max_bulk_targets,
            blast_radius_ok=True,
            plan="Invalid capacity: desired_user_sessions must be >= 0.",
            notes=["No changes were made."],
        )
    detail = f"Set WorkSpaces Pool {pool_id} desired user sessions to {desired_user_sessions}"
    client = factory.client(consts.WORKSPACES_API, region=region)
    return _single_target_outcome(
        action,
        pool_id,
        confirm,
        max_bulk_targets,
        detail,
        consts.PRODUCT_WORKSPACES_POOLS,
        "UpdateWorkspacesPool",
        lambda: client.update_workspaces_pool(
            PoolId=pool_id, Capacity={"DesiredUserSessions": desired_user_sessions}
        ),
    )


def fleet_power_action_core(
    factory: ClientFactory,
    region: str | None,
    action: str,
    fleet_name: str,
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    method_name = "start_fleet" if action == "start_fleet" else "stop_fleet"
    operation = "StartFleet" if action == "start_fleet" else "StopFleet"
    detail = f"{'Start' if action == 'start_fleet' else 'Stop'} fleet {fleet_name}"
    client = factory.client(consts.APPSTREAM_API, region=region)
    return _single_target_outcome(
        action,
        fleet_name,
        confirm,
        max_bulk_targets,
        detail,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        operation,
        lambda: getattr(client, method_name)(Name=fleet_name),
    )


def update_fleet_capacity_core(
    factory: ClientFactory,
    region: str | None,
    fleet_name: str,
    desired_instances: int,
    confirm: bool,
    max_bulk_targets: int,
) -> WriteOutcome:
    action = "update_fleet_capacity"
    if desired_instances < 0:
        return WriteOutcome(
            action=action,
            dry_run=True,
            confirmed=False,
            requested_targets=[fleet_name],
            max_bulk_targets=max_bulk_targets,
            blast_radius_ok=True,
            plan="Invalid capacity: desired_instances must be >= 0.",
            notes=["No changes were made."],
        )
    detail = f"Set fleet {fleet_name} desired instances to {desired_instances}"
    client = factory.client(consts.APPSTREAM_API, region=region)
    return _single_target_outcome(
        action,
        fleet_name,
        confirm,
        max_bulk_targets,
        detail,
        consts.PRODUCT_WORKSPACES_APPLICATIONS,
        "UpdateFleet",
        lambda: client.update_fleet(
            Name=fleet_name, ComputeCapacity={"DesiredInstances": desired_instances}
        ),
    )


def register(
    mcp: Any,
    factory: ClientFactory,
    *,
    max_bulk_targets: int,
    enable_destructive: bool = False,
) -> None:
    """Register guarded lifecycle tools. Only call this when writes are enabled."""

    async def start_workspaces(
        workspace_ids: list[str], confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Start (power on) one or more WorkSpaces Personal desktops.

        Dry-run by default: returns the plan and changes nothing unless confirm=true. Confirmed
        bulk actions are refused above the configured blast-radius cap.

        Args:
            workspace_ids: WorkSpace IDs to start.
            confirm: Set true to actually start them; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = batch_power_action_core(
            factory, region or factory.region, "start", workspace_ids, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def stop_workspaces(
        workspace_ids: list[str], confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Stop (power off) one or more AutoStop WorkSpaces Personal desktops.

        Dry-run by default; confirm=true to execute. Confirmed bulk actions are refused above the
        blast-radius cap.

        Args:
            workspace_ids: WorkSpace IDs to stop.
            confirm: Set true to actually stop them; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = batch_power_action_core(
            factory, region or factory.region, "stop", workspace_ids, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def reboot_workspaces(
        workspace_ids: list[str], confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Reboot one or more WorkSpaces Personal desktops.

        Dry-run by default; confirm=true to execute. Confirmed bulk actions are refused above the
        blast-radius cap. Rebooting disconnects the user.

        Args:
            workspace_ids: WorkSpace IDs to reboot.
            confirm: Set true to actually reboot them; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = batch_power_action_core(
            factory, region or factory.region, "reboot", workspace_ids, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def modify_workspace_running_mode(
        workspace_id: str, running_mode: str, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Change a WorkSpace's running mode (AUTO_STOP or ALWAYS_ON).

        Dry-run by default; confirm=true to execute. Use after recommend_running_mode.

        Args:
            workspace_id: The WorkSpace ID to modify.
            running_mode: AUTO_STOP or ALWAYS_ON.
            confirm: Set true to actually apply the change; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = modify_running_mode_core(
            factory, region or factory.region, workspace_id, running_mode, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def start_workspaces_pool(
        pool_id: str, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Start a WorkSpaces Pool. Dry-run by default; confirm=true to execute.

        Args:
            pool_id: The WorkSpaces Pool ID.
            confirm: Set true to actually start it; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = pool_power_action_core(
            factory, region or factory.region, "start_pool", pool_id, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def stop_workspaces_pool(
        pool_id: str, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Stop a WorkSpaces Pool. Dry-run by default; confirm=true to execute.

        Args:
            pool_id: The WorkSpaces Pool ID.
            confirm: Set true to actually stop it; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = pool_power_action_core(
            factory, region or factory.region, "stop_pool", pool_id, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def update_workspaces_pool_capacity(
        pool_id: str, desired_user_sessions: int, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Set a WorkSpaces Pool's desired user-session capacity.

        Dry-run by default; confirm=true to execute. Changing capacity affects cost.

        Args:
            pool_id: The WorkSpaces Pool ID.
            desired_user_sessions: Target number of concurrent user sessions (>= 0).
            confirm: Set true to actually apply the change; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = update_pool_capacity_core(
            factory,
            region or factory.region,
            pool_id,
            desired_user_sessions,
            confirm,
            max_bulk_targets,
        )
        return outcome.model_dump()

    async def start_application_fleet(
        fleet_name: str, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Start a WorkSpaces Applications (formerly AppStream 2.0) fleet. Dry-run by default.

        Args:
            fleet_name: The fleet name.
            confirm: Set true to actually start it; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = fleet_power_action_core(
            factory, region or factory.region, "start_fleet", fleet_name, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def stop_application_fleet(
        fleet_name: str, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Stop a WorkSpaces Applications (formerly AppStream 2.0) fleet. Dry-run by default.

        Args:
            fleet_name: The fleet name.
            confirm: Set true to actually stop it; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = fleet_power_action_core(
            factory, region or factory.region, "stop_fleet", fleet_name, confirm, max_bulk_targets
        )
        return outcome.model_dump()

    async def update_application_fleet_capacity(
        fleet_name: str, desired_instances: int, confirm: bool = False, region: str | None = None
    ) -> dict[str, Any]:
        """Set a WorkSpaces Applications (formerly AppStream 2.0) fleet's desired instance capacity.

        Dry-run by default; confirm=true to execute. Changing capacity affects cost.

        Args:
            fleet_name: The fleet name.
            desired_instances: Target number of fleet instances (>= 0).
            confirm: Set true to actually apply the change; otherwise a dry-run plan is returned.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = update_fleet_capacity_core(
            factory,
            region or factory.region,
            fleet_name,
            desired_instances,
            confirm,
            max_bulk_targets,
        )
        return outcome.model_dump()

    mcp.add_tool(start_workspaces)
    mcp.add_tool(stop_workspaces)
    mcp.add_tool(reboot_workspaces)
    mcp.add_tool(modify_workspace_running_mode)
    mcp.add_tool(start_workspaces_pool)
    mcp.add_tool(stop_workspaces_pool)
    mcp.add_tool(update_workspaces_pool_capacity)
    mcp.add_tool(start_application_fleet)
    mcp.add_tool(stop_application_fleet)
    mcp.add_tool(update_application_fleet_capacity)
