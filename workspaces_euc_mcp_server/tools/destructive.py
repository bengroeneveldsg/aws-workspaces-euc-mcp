# Copyright bengroeneveldsg. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Destructive WorkSpaces Personal operations — Phase 3, IAM Tier 3.

These data-impacting / irreversible operations are gated more strictly than the Phase 2 lifecycle
tools. They are registered ONLY when the server is launched with both ``--enable-writes`` and
``--enable-destructive``, and every execution requires, in addition to ``confirm=true``:

* an exact **typed acknowledgement** phrase (e.g. ``acknowledge="TERMINATE"``), and
* staying within the ``--max-bulk-targets`` blast-radius cap.

Default behaviour is still a dry-run that changes nothing.
"""

from __future__ import annotations

from typing import Any

from .. import consts
from ..clients import ClientFactory
from ..models import ServiceError, TargetResult, WriteOutcome
from ._common import try_call, writes

# action -> (boto3 method, per-request key, required acknowledgement phrase, impact note)
_BATCH_DESTRUCTIVE = {
    "terminate": (
        "terminate_workspaces",
        "TerminateWorkspaceRequests",
        "TERMINATE",
        "PERMANENT and IRREVERSIBLE: the WorkSpace(s) and their data are deleted.",
    ),
    "rebuild": (
        "rebuild_workspaces",
        "RebuildWorkspaceRequests",
        "REBUILD",
        "Disruptive: the root volume is reset to the bundle and the user volume is restored from "
        "the last snapshot; data since the last snapshot is lost.",
    ),
}


def _dry_run(action: str, ids: list[str], max_bulk: int, detail: str, impact: str) -> WriteOutcome:
    return WriteOutcome(
        action=action,
        dry_run=True,
        confirmed=False,
        requested_targets=ids,
        max_bulk_targets=max_bulk,
        blast_radius_ok=len(ids) <= max_bulk,
        plan=f"Would {detail.lower()}",
        results=[TargetResult(target_id=i, status="skipped", message="dry run") for i in ids],
        notes=[
            impact,
            "Dry run — nothing was changed. Re-run with confirm=true and the exact acknowledge "
            "phrase to execute.",
        ],
    )


def _refuse(
    action: str, ids: list[str], max_bulk: int, plan: str, *, acknowledgement: str | None = None
) -> WriteOutcome:
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=ids,
        max_bulk_targets=max_bulk,
        blast_radius_ok=acknowledgement is not None or len(ids) <= max_bulk,
        plan=plan,
        acknowledgement_required=acknowledgement,
        notes=["No changes were made."],
    )


def _execute_outcome(
    action: str,
    ids: list[str],
    max_bulk: int,
    detail: str,
    errors: list[ServiceError],
    failed: dict[str, dict],
) -> WriteOutcome:
    results = [
        TargetResult(
            target_id=i,
            status="error" if i in failed else "ok",
            message=failed.get(i, {}).get("ErrorMessage") if i in failed else None,
        )
        for i in ids
    ]
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=ids,
        max_bulk_targets=max_bulk,
        blast_radius_ok=True,
        plan=f"{detail} (executed).",
        results=results,
        errors=errors,
    )


def batch_destructive_core(
    factory: ClientFactory,
    region: str | None,
    action: str,
    workspace_ids: list[str],
    confirm: bool,
    acknowledge: str,
    max_bulk_targets: int,
) -> WriteOutcome:
    method_name, request_key, required_phrase, impact = _BATCH_DESTRUCTIVE[action]
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
        return _dry_run(action, workspace_ids, max_bulk_targets, detail, impact)

    if len(workspace_ids) > max_bulk_targets:
        return _refuse(
            action,
            workspace_ids,
            max_bulk_targets,
            plan=(
                f"Refused: {len(workspace_ids)} targets exceed the blast-radius cap of "
                f"{max_bulk_targets}."
            ),
        )

    if acknowledge.strip() != required_phrase:
        return _refuse(
            action,
            workspace_ids,
            max_bulk_targets,
            plan=(
                f"Refused: this {action} is destructive and requires the exact acknowledgement "
                f"phrase. {impact}"
            ),
            acknowledgement=required_phrase,
        )

    errors: list[ServiceError] = []
    client = factory.client(consts.WORKSPACES_API, region=region)
    requests = [{"WorkspaceId": wid} for wid in workspace_ids]

    def execute() -> Any:
        return getattr(client, method_name)(**{request_key: requests})

    response = try_call(
        errors, consts.PRODUCT_WORKSPACES_PERSONAL, method_name, execute, default={}
    )
    failed = {fr.get("WorkspaceId"): fr for fr in (response or {}).get("FailedRequests", [])}
    return _execute_outcome(action, workspace_ids, max_bulk_targets, detail, errors, failed)


def restore_workspace_core(
    factory: ClientFactory,
    region: str | None,
    workspace_id: str,
    confirm: bool,
    acknowledge: str,
    max_bulk_targets: int,
) -> WriteOutcome:
    action = "restore"
    required_phrase = "RESTORE"
    impact = "Disruptive: the WorkSpace is restored from its last snapshot; unsynced data is lost."
    detail = f"Restore WorkSpace {workspace_id}"
    ids = [workspace_id]

    if not confirm:
        return _dry_run(action, ids, max_bulk_targets, detail, impact)

    if acknowledge.strip() != required_phrase:
        return _refuse(
            action,
            ids,
            max_bulk_targets,
            plan=f"Refused: restore is destructive and requires the exact acknowledgement phrase. "
            f"{impact}",
            acknowledgement=required_phrase,
        )

    errors: list[ServiceError] = []
    client = factory.client(consts.WORKSPACES_API, region=region)
    try_call(
        errors,
        consts.PRODUCT_WORKSPACES_PERSONAL,
        "RestoreWorkspace",
        lambda: client.restore_workspace(WorkspaceId=workspace_id),
        default={},
    )
    status = "error" if errors else "ok"
    return WriteOutcome(
        action=action,
        dry_run=False,
        confirmed=True,
        requested_targets=ids,
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


def register(mcp: Any, factory: ClientFactory, *, max_bulk_targets: int) -> None:
    """Register destructive tools. Only call this when destructive ops are enabled."""

    async def terminate_workspaces(
        workspace_ids: list[str],
        confirm: bool = False,
        acknowledge: str = "",
        region: str | None = None,
    ) -> dict[str, Any]:
        """Permanently terminate (delete) WorkSpaces Personal desktops. IRREVERSIBLE.

        Dry-run by default. To execute you must pass confirm=true AND acknowledge="TERMINATE", and
        stay within the blast-radius cap. Deleted WorkSpaces and their data cannot be recovered.

        Args:
            workspace_ids: WorkSpace IDs to terminate.
            confirm: Set true to execute (still requires acknowledge).
            acknowledge: Must be exactly "TERMINATE" to proceed.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = batch_destructive_core(
            factory,
            region or factory.region,
            "terminate",
            workspace_ids,
            confirm,
            acknowledge,
            max_bulk_targets,
        )
        return outcome.model_dump()

    async def rebuild_workspaces(
        workspace_ids: list[str],
        confirm: bool = False,
        acknowledge: str = "",
        region: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild WorkSpaces Personal desktops (resets root volume; user volume from snapshot).

        Dry-run by default. To execute you must pass confirm=true AND acknowledge="REBUILD". Data
        written since the last snapshot is lost.

        Args:
            workspace_ids: WorkSpace IDs to rebuild.
            confirm: Set true to execute (still requires acknowledge).
            acknowledge: Must be exactly "REBUILD" to proceed.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = batch_destructive_core(
            factory,
            region or factory.region,
            "rebuild",
            workspace_ids,
            confirm,
            acknowledge,
            max_bulk_targets,
        )
        return outcome.model_dump()

    async def restore_workspace(
        workspace_id: str,
        confirm: bool = False,
        acknowledge: str = "",
        region: str | None = None,
    ) -> dict[str, Any]:
        """Restore a WorkSpaces Personal desktop from its last snapshot.

        Dry-run by default. To execute you must pass confirm=true AND acknowledge="RESTORE".

        Args:
            workspace_id: The WorkSpace ID to restore.
            confirm: Set true to execute (still requires acknowledge).
            acknowledge: Must be exactly "RESTORE" to proceed.
            region: AWS region. Defaults to the server's configured region.
        """
        outcome = restore_workspace_core(
            factory, region or factory.region, workspace_id, confirm, acknowledge, max_bulk_targets
        )
        return outcome.model_dump()

    mcp.add_tool(terminate_workspaces, annotations=writes("Terminate WorkSpaces", destructive=True))
    mcp.add_tool(rebuild_workspaces, annotations=writes("Rebuild WorkSpaces", destructive=True))
    mcp.add_tool(restore_workspace, annotations=writes("Restore WorkSpace", destructive=True))
