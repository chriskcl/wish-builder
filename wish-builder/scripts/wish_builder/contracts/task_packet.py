"""Canonical generated task packets shared by dispatch and qualification."""

from __future__ import annotations

from .manifest_v2 import ExecutionManifestV2, ManifestTask
from .runtime import ExecutionIdentity
from .serialization import canonical_json_bytes


WORKER_INSTRUCTIONS = (
    "Use the active Trellis task context and the current attempt worktree.",
    "Implement only the approved task and inspect repository evidence before editing.",
    "Run the declared checks, self-review the diff, and report structured completion.",
    "Do not expand scope, change approved architecture, merge, deploy, or access secrets.",
)


def generated_task_packet_bytes(
    manifest: ExecutionManifestV2,
    task: ManifestTask,
    trellis_task_id: str,
    identity: ExecutionIdentity,
) -> bytes:
    """Build the exact non-template packet sent to a backend worker."""

    if type(manifest) is not ExecutionManifestV2:
        raise TypeError("manifest must be an ExecutionManifestV2")
    if type(task) is not ManifestTask or task not in manifest.tasks:
        raise ValueError("task must belong to the execution manifest")
    if type(trellis_task_id) is not str or not trellis_task_id:
        raise ValueError("trellis_task_id must be nonempty")
    if type(identity) is not ExecutionIdentity or not identity.is_attempt:
        raise ValueError("identity must be a complete attempt identity")
    if identity.run_id != manifest.run_id or identity.task_id != task.id:
        raise ValueError("identity does not match the manifest task")
    mapping = {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
    if mapping.get(task.id) != trellis_task_id:
        raise ValueError("trellis_task_id does not match the manifest mapping")

    packet = {
        "execution": {
            "base_branch": manifest.base_branch,
            "dispatch_id": identity.correlation_id,
            "identity": identity.to_primitive(),
            "manifest_digest": manifest.canonical_sha256(),
            "run_id": manifest.run_id,
        },
        "instructions": list(WORKER_INSTRUCTIONS),
        "kind": "wish_builder_task_packet",
        "policy": {
            "capability_digest": manifest.capability_digest,
            "launch_profile_digest": manifest.launch_profile_digest,
            "policy_digest": manifest.policy_digest,
            "protected_paths": list(manifest.protected_paths),
            "scheduler_mode": manifest.scheduler_mode.value,
            "worker_backend": manifest.provider.value,
        },
        "schema_version": 1,
        "task": {
            **task.to_primitive(),
            "trellis_task_id": trellis_task_id,
        },
        "trellis": {
            "graph_digest": manifest.trellis_graph_digest,
            "parent_task_id": manifest.trellis_parent_task_id,
            "revision": manifest.trellis_revision,
        },
    }
    return canonical_json_bytes(packet)


__all__ = ["WORKER_INSTRUCTIONS", "generated_task_packet_bytes"]
