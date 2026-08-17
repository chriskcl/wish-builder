"""Public M1 contracts and strict admission decoder."""

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
from .serialization import canonical_json_bytes, canonical_sha256


__all__ = [
    "ApprovalSet",
    "DEFAULT_DECODE_LIMITS",
    "DecodeLimits",
    "DecodeResult",
    "Diagnostic",
    "DiagnosticPath",
    "ExecutionManifest",
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
]
