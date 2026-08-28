"""Public M1 contracts and strict admission decoder."""

from .compatibility import *  # noqa: F403
from .compatibility import __all__ as _compatibility_exports
from .compatibility_decoder import *  # noqa: F403
from .compatibility_decoder import __all__ as _compatibility_decoder_exports
from .decoder import (
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    decode_manifest_bytes,
    decode_manifest_primitive,
    strict_decode_manifest,
)
from .diagnostics import (
    DecodeResult,
    Diagnostic,
    DiagnosticPath,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
    combine_reports,
)
from .execution import ExecutionManifestModel, is_execution_manifest_model
from .manifest_v2 import *  # noqa: F403
from .manifest_v2 import __all__ as _manifest_v2_exports
from .manifest_v2_decoder import *  # noqa: F403
from .manifest_v2_decoder import __all__ as _manifest_v2_decoder_exports
from .models import (
    ApprovalSet,
    ExecutionManifest,
    GateApproval,
    Manifest,
    Requirement,
    RequirementStatus,
    RiskLevel,
    Task,
    TaskStatus,
    ValidationPhase,
)
from .qualification_evidence import *  # noqa: F403
from .qualification_evidence import __all__ as _qualification_evidence_exports
from .qualification_evidence_decoder import *  # noqa: F403
from .qualification_evidence_decoder import (
    __all__ as _qualification_evidence_decoder_exports,
)
from .runtime import *  # noqa: F403
from .runtime import __all__ as _runtime_exports
from .runtime_decoder import *  # noqa: F403
from .runtime_decoder import __all__ as _runtime_decoder_exports
from .task_packet import *  # noqa: F403
from .task_packet import __all__ as _task_packet_exports
from .serialization import canonical_json_bytes, canonical_sha256

__all__ = [
    "ApprovalSet",
    "DEFAULT_DECODE_LIMITS",
    "DecodeLimits",
    "DecodeResult",
    "Diagnostic",
    "DiagnosticPath",
    "ExecutionManifest",
    "ExecutionManifestModel",
    "GateApproval",
    "Manifest",
    "Requirement",
    "RequirementStatus",
    "RiskLevel",
    "ReasonCode",
    "Severity",
    "Task",
    "TaskStatus",
    "ValidationIssue",
    "ValidationPhase",
    "ValidationReport",
    "ValidationStage",
    "canonical_json_bytes",
    "canonical_sha256",
    "combine_reports",
    "decode_manifest_bytes",
    "decode_manifest_primitive",
    "strict_decode_manifest",
    "is_execution_manifest_model",
    *_compatibility_exports,
    *_compatibility_decoder_exports,
    *_manifest_v2_exports,
    *_manifest_v2_decoder_exports,
    *_qualification_evidence_exports,
    *_qualification_evidence_decoder_exports,
    *_runtime_exports,
    *_runtime_decoder_exports,
    *_task_packet_exports,
]
