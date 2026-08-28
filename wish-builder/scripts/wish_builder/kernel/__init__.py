"""Pure deterministic Wish Builder kernel."""

from .dag import DagError, DagNode, TaskDag
from .state import (
    ApplyReason,
    ApplyResult,
    AttemptProjection,
    KernelSnapshot,
    StateTransition,
    StateTransitionError,
    TaskProjection,
    apply_journal_event,
    apply_transition,
    replay,
    replay_journal_events,
    validate_transition,
)
from .validation import (
    admit_manifest_bytes,
    admit_manifest_primitive,
    diagnostics_bytes,
    diagnostics_sha256,
    render_diagnostics,
    validate_manifest,
    validate_manifest_bytes,
    validate_manifest_shape,
)


__all__ = [
    "ApplyReason",
    "ApplyResult",
    "AttemptProjection",
    "DagError",
    "DagNode",
    "KernelSnapshot",
    "StateTransition",
    "StateTransitionError",
    "TaskDag",
    "TaskProjection",
    "apply_journal_event",
    "apply_transition",
    "admit_manifest_bytes",
    "admit_manifest_primitive",
    "diagnostics_bytes",
    "diagnostics_sha256",
    "render_diagnostics",
    "replay",
    "replay_journal_events",
    "validate_manifest",
    "validate_manifest_bytes",
    "validate_manifest_shape",
    "validate_transition",
]
