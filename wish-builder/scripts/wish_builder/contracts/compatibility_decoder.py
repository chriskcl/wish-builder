"""Strict decoder for Trellis compatibility and backend qualification bundles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from .compatibility import (
    ADAPTER_QUALIFICATION_SCHEMA_VERSION,
    BACKEND_CAPABILITY_SCHEMA_VERSION,
    BACKEND_QUALIFICATION_SCHEMA_VERSION,
    MAX_COMPATIBILITY_EVIDENCE,
    MAX_COMPATIBILITY_PACKAGES,
    MAX_COMPATIBILITY_PLATFORMS,
    MAX_COMPATIBILITY_PROVIDERS,
    MAX_QUALIFICATION_SIBLINGS,
    MAX_SAFE_JSON_INTEGER,
    OPERATION_ORDER,
    QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
    TRELLIS_COMPATIBILITY_SCHEMA_VERSION,
    AdapterQualificationStatus,
    BackendQualificationBundle,
    CapabilityFeatures,
    CompatibilityPolicy,
    DisjointSiblingOverlapEvidence,
    EvidenceScope,
    IntegrationCapabilities,
    LaunchProfile,
    NpmProvenance,
    OperationGuarantee,
    Platform,
    PlatformCompatibility,
    Provider,
    ProviderCompatibility,
    Qualification,
    QualificationArtifact,
    QualificationScenario,
    QualificationScenarioEvidence,
    QualificationStatus,
    SdkPin,
    TrellisAdapterQualification,
    TrellisCompatibility,
    TrellisGraphAdapterQualification,
    TrellisPackage,
    TrellisPackageRole,
    TrellisProjectionAdapterQualification,
)
from .decoder import (
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    _audit_shape,
    _decode_json_bytes,
    _issue,
    _normalized_contract_string,
)
from .diagnostics import (
    DecodeResult,
    DiagnosticPath,
    ReasonCode,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from .models import HASH_RE, MAX_PATH_LENGTH, MAX_TEXT_LENGTH


@dataclass(frozen=True, slots=True)
class _CompatibilityDecodeError(Exception):
    path: DiagnosticPath
    rule_id: str
    reason_code: ReasonCode
    message: str


def _fail(
    path: DiagnosticPath,
    rule_id: str,
    reason_code: ReasonCode,
    message: str,
) -> None:
    raise _CompatibilityDecodeError(path, rule_id, reason_code, message)


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


def _object(
    value: object,
    path: DiagnosticPath,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _fail(
            path,
            "schema.object_type",
            ReasonCode.WRONG_CONTAINER_TYPE,
            "Expected a JSON object.",
        )
    assert type(value) is dict
    unknown = sorted(set(value) - fields, key=_utf8_key)
    if unknown:
        _fail(
            path + (unknown[0],),
            "schema.unknown_field",
            ReasonCode.UNKNOWN_FIELD,
            "Unknown fields are not admitted by the compatibility schema.",
        )
    missing = sorted(fields - set(value), key=_utf8_key)
    if missing:
        _fail(
            path + (missing[0],),
            "schema.required_field",
            ReasonCode.MISSING_FIELD,
            "A required field is missing.",
        )
    return value


def _array(
    value: object,
    path: DiagnosticPath,
    *,
    nonempty: bool,
    maximum: int,
) -> list[object]:
    if type(value) is not list:
        _fail(
            path,
            "schema.array_type",
            ReasonCode.WRONG_CONTAINER_TYPE,
            "Expected a JSON array.",
        )
    assert type(value) is list
    if nonempty and not value:
        _fail(
            path,
            "value.nonempty_array",
            ReasonCode.EMPTY_COLLECTION,
            "The array must not be empty.",
        )
    if len(value) > maximum:
        _fail(
            path,
            "value.collection_limit",
            ReasonCode.ITEM_LIMIT_EXCEEDED,
            f"The array exceeds {maximum} entries.",
        )
    return value


def _string(
    value: object,
    path: DiagnosticPath,
    *,
    limit: int = MAX_TEXT_LENGTH,
) -> str:
    if type(value) is not str:
        _fail(
            path,
            "schema.string_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected a string.",
        )
    assert type(value) is str
    normalized = _normalized_contract_string(value)
    if not normalized.strip():
        _fail(
            path,
            "value.nonempty_string",
            ReasonCode.EMPTY_STRING,
            "The string must not be empty.",
        )
    if len(normalized) > limit:
        _fail(
            path,
            "value.string_length",
            ReasonCode.STRING_LIMIT_EXCEEDED,
            f"The string exceeds the field limit of {limit} characters.",
        )
    return normalized


def _boolean(value: object, path: DiagnosticPath) -> bool:
    if type(value) is not bool:
        _fail(
            path,
            "schema.boolean_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected a boolean.",
        )
    return value


def _integer(value: object, path: DiagnosticPath) -> int:
    if type(value) is not int:
        _fail(
            path,
            "schema.integer_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected an integer; booleans are not integers at this boundary.",
        )
    if not 1 <= value <= MAX_SAFE_JSON_INTEGER:
        _fail(
            path,
            "value.safe_positive_integer",
            ReasonCode.INTEGER_OUT_OF_RANGE,
            "Expected a positive safe JSON integer.",
        )
    return value


def _exact_schema_version(
    value: object,
    path: DiagnosticPath,
    *,
    expected: int,
    label: str,
) -> int:
    if type(value) is not int:
        _fail(
            path,
            "schema.integer_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected an integer schema version.",
        )
    if value != expected:
        _fail(
            path,
            "schema.version",
            ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            f"Only {label} schema version {expected} is supported.",
        )
    return value


def _compatibility_schema_version(value: object, path: DiagnosticPath) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=BACKEND_QUALIFICATION_SCHEMA_VERSION,
        label="backend qualification",
    )


def _trellis_compatibility_schema_version(value: object, path: DiagnosticPath) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=TRELLIS_COMPATIBILITY_SCHEMA_VERSION,
        label="Trellis compatibility",
    )


def _adapter_qualification_schema_version(value: object, path: DiagnosticPath) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=ADAPTER_QUALIFICATION_SCHEMA_VERSION,
        label="adapter qualification",
    )


def _qualification_artifact_schema_version(value: object, path: DiagnosticPath) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
        label="qualification artifact",
    )


def _v1_schema_version(value: object, path: DiagnosticPath) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=1,
        label="nested contract",
    )


def _backend_capability_schema_version(
    value: object, path: DiagnosticPath
) -> int:
    return _exact_schema_version(
        value,
        path,
        expected=BACKEND_CAPABILITY_SCHEMA_VERSION,
        label="backend capability",
    )


E = TypeVar("E")


def _enum(value: object, enum_type: type[E], path: DiagnosticPath) -> E:
    text = _string(value, path, limit=128)
    try:
        return enum_type(text)  # type: ignore[call-arg]
    except ValueError:
        allowed = sorted(item.value for item in enum_type)  # type: ignore[attr-defined]
        _fail(
            path,
            "schema.enum_value",
            ReasonCode.UNKNOWN_ENUM_VALUE,
            "Expected one of: " + ", ".join(allowed) + ".",
        )
    raise AssertionError("unreachable")  # pragma: no cover


def _sha256(value: object, path: DiagnosticPath) -> str:
    text = _string(value, path, limit=71)
    if not HASH_RE.fullmatch(text):
        _fail(
            path,
            "value.sha256_reference",
            ReasonCode.INVALID_HASH,
            "Expected sha256 followed by 64 lowercase hexadecimal characters.",
        )
    return text


def _sha1(value: object, path: DiagnosticPath) -> str:
    text = _string(value, path, limit=40)
    if len(text) != 40 or any(
        character not in "0123456789abcdef" for character in text
    ):
        _fail(
            path,
            "value.sha1_digest",
            ReasonCode.INVALID_HASH,
            "Expected a 40-character lowercase SHA-1 digest.",
        )
    return text


def _string_array(
    value: object,
    path: DiagnosticPath,
    *,
    maximum: int,
) -> tuple[str, ...]:
    items = _array(value, path, nonempty=True, maximum=maximum)
    result = tuple(_string(item, path + (index,)) for index, item in enumerate(items))
    if len(set(result)) != len(result):
        _fail(
            path,
            "value.duplicate_array_item",
            ReasonCode.DUPLICATE_ITEM,
            "The array must not contain duplicate values.",
        )
    return result


T = TypeVar("T")


def _construct(model_type: type[T], path: DiagnosticPath, **values: object) -> T:
    try:
        return model_type(**values)
    except (TypeError, ValueError) as exc:
        message = str(exc) or "Compatibility fields are not jointly valid."
        if (
            "digest" in message
            or "sha256" in message
            or "shasum" in message
            or "integrity" in message
        ):
            rule_id = "value.digest_match"
            reason_code = ReasonCode.INVALID_HASH
        elif "schema_version" in message:
            rule_id = "schema.version"
            reason_code = ReasonCode.UNSUPPORTED_SCHEMA_VERSION
        else:
            rule_id = "value.compatibility_contract"
            reason_code = ReasonCode.INVALID_MAPPING
        _fail(path, rule_id, reason_code, message)
    raise AssertionError("unreachable")  # pragma: no cover


def _provenance(value: object, path: DiagnosticPath) -> NpmProvenance:
    item = _object(
        value,
        path,
        frozenset({"attestationUrl", "predicateType"}),
    )
    return _construct(
        NpmProvenance,
        path,
        attestation_url=_string(
            item["attestationUrl"], path + ("attestationUrl",), limit=MAX_PATH_LENGTH
        ),
        predicate_type=_string(
            item["predicateType"], path + ("predicateType",), limit=128
        ),
    )


def _package(value: object, path: DiagnosticPath) -> TrellisPackage:
    item = _object(
        value,
        path,
        frozenset(
            {
                "filename",
                "name",
                "npmIntegrity",
                "npmShasum",
                "provenance",
                "role",
                "sha256",
                "size",
                "tarballUrl",
                "version",
            }
        ),
    )
    return _construct(
        TrellisPackage,
        path,
        filename=_string(item["filename"], path + ("filename",), limit=MAX_PATH_LENGTH),
        integrity=_string(item["npmIntegrity"], path + ("npmIntegrity",), limit=95),
        name=_string(item["name"], path + ("name",), limit=128),
        provenance=_provenance(item["provenance"], path + ("provenance",)),
        role=_enum(item["role"], TrellisPackageRole, path + ("role",)),
        sha256=_sha256(item["sha256"], path + ("sha256",)),
        shasum=_sha1(item["npmShasum"], path + ("npmShasum",)),
        size=_integer(item["size"], path + ("size",)),
        tarball_url=_string(
            item["tarballUrl"], path + ("tarballUrl",), limit=MAX_PATH_LENGTH
        ),
        version=_string(item["version"], path + ("version",), limit=128),
    )


def _graph_adapter_qualification(
    value: object, path: DiagnosticPath
) -> TrellisGraphAdapterQualification:
    item = _object(
        value,
        path,
        frozenset(
            {
                "derivedFormat",
                "deterministicSnapshot",
                "evidence",
                "readOnlySnapshot",
                "result",
                "strictImport",
            }
        ),
    )
    return _construct(
        TrellisGraphAdapterQualification,
        path,
        derived_format=_string(
            item["derivedFormat"], path + ("derivedFormat",), limit=MAX_PATH_LENGTH
        ),
        deterministic_snapshot=_boolean(
            item["deterministicSnapshot"], path + ("deterministicSnapshot",)
        ),
        evidence=_string_array(
            item["evidence"],
            path + ("evidence",),
            maximum=MAX_COMPATIBILITY_EVIDENCE,
        ),
        read_only_snapshot=_boolean(
            item["readOnlySnapshot"], path + ("readOnlySnapshot",)
        ),
        result=_enum(item["result"], AdapterQualificationStatus, path + ("result",)),
        strict_import=_boolean(item["strictImport"], path + ("strictImport",)),
    )


def _projection_adapter_qualification(
    value: object, path: DiagnosticPath
) -> TrellisProjectionAdapterQualification:
    item = _object(
        value,
        path,
        frozenset(
            {
                "concurrentProjectionWritersSafe",
                "crossProcessCas",
                "digestGuarded",
                "evidence",
                "expectedRevisionGuarded",
                "isolatedCheckout",
                "postWriteVerified",
                "result",
                "singleWriter",
            }
        ),
    )
    return _construct(
        TrellisProjectionAdapterQualification,
        path,
        concurrent_projection_writers_safe=_boolean(
            item["concurrentProjectionWritersSafe"],
            path + ("concurrentProjectionWritersSafe",),
        ),
        cross_process_cas=_boolean(
            item["crossProcessCas"], path + ("crossProcessCas",)
        ),
        digest_guarded=_boolean(item["digestGuarded"], path + ("digestGuarded",)),
        evidence=_string_array(
            item["evidence"],
            path + ("evidence",),
            maximum=MAX_COMPATIBILITY_EVIDENCE,
        ),
        expected_revision_guarded=_boolean(
            item["expectedRevisionGuarded"], path + ("expectedRevisionGuarded",)
        ),
        isolated_checkout=_boolean(
            item["isolatedCheckout"], path + ("isolatedCheckout",)
        ),
        post_write_verified=_boolean(
            item["postWriteVerified"], path + ("postWriteVerified",)
        ),
        result=_enum(item["result"], AdapterQualificationStatus, path + ("result",)),
        single_writer=_boolean(item["singleWriter"], path + ("singleWriter",)),
    )


def _adapter_qualification(
    value: object, path: DiagnosticPath
) -> TrellisAdapterQualification:
    item = _object(
        value,
        path,
        frozenset(
            {
                "graph",
                "projection",
                "qualificationDigest",
                "schemaVersion",
                "trellisVersion",
            }
        ),
    )
    return _construct(
        TrellisAdapterQualification,
        path,
        graph=_graph_adapter_qualification(item["graph"], path + ("graph",)),
        projection=_projection_adapter_qualification(
            item["projection"], path + ("projection",)
        ),
        qualification_digest=_sha256(
            item["qualificationDigest"], path + ("qualificationDigest",)
        ),
        schema_version=_adapter_qualification_schema_version(
            item["schemaVersion"], path + ("schemaVersion",)
        ),
        trellis_version=_string(
            item["trellisVersion"], path + ("trellisVersion",), limit=128
        ),
    )


def _policy(value: object, path: DiagnosticPath) -> CompatibilityPolicy:
    item = _object(
        value,
        path,
        frozenset(
            {
                "credentialsManagedBy",
                "freshAttemptWorktree",
                "freshProviderSession",
                "maxTaskPacketBytes",
                "oneProviderPerRun",
                "providerFallback",
                "providerNativeSiblingScheduling",
                "schedulerMode",
                "schemaVersion",
            }
        ),
    )
    return _construct(
        CompatibilityPolicy,
        path,
        credentials_managed_by=_string(
            item["credentialsManagedBy"], path + ("credentialsManagedBy",), limit=64
        ),
        fresh_attempt_worktree=_boolean(
            item["freshAttemptWorktree"], path + ("freshAttemptWorktree",)
        ),
        fresh_provider_session=_boolean(
            item["freshProviderSession"], path + ("freshProviderSession",)
        ),
        max_task_packet_bytes=_integer(
            item["maxTaskPacketBytes"], path + ("maxTaskPacketBytes",)
        ),
        one_provider_per_run=_boolean(
            item["oneProviderPerRun"], path + ("oneProviderPerRun",)
        ),
        provider_fallback=_boolean(
            item["providerFallback"], path + ("providerFallback",)
        ),
        provider_native_sibling_scheduling=_boolean(
            item["providerNativeSiblingScheduling"],
            path + ("providerNativeSiblingScheduling",),
        ),
        scheduler_mode=_string(
            item["schedulerMode"], path + ("schedulerMode",), limit=64
        ),
        schema_version=_v1_schema_version(
            item["schemaVersion"], path + ("schemaVersion",)
        ),
    )


def _sdk(value: object, path: DiagnosticPath) -> SdkPin:
    item = _object(value, path, frozenset({"name", "shasum", "version"}))
    return _construct(
        SdkPin,
        path,
        name=_string(item["name"], path + ("name",), limit=128),
        shasum=_sha1(item["shasum"], path + ("shasum",)),
        version=_string(item["version"], path + ("version",), limit=128),
    )


def _launch_profile(
    value: object,
    path: DiagnosticPath,
    provider: Provider,
) -> LaunchProfile:
    fields = {
        "args",
        "command",
        "freshSession",
        "platform",
        "protocol",
        "resume",
    }
    if provider is not Provider.CODEX:
        fields.update({"package", "packageShasum"})
    item = _object(value, path, frozenset(fields))
    args = _string_array(item["args"], path + ("args",), maximum=16)
    package = None
    package_shasum = None
    if provider is not Provider.CODEX:
        package = _string(item["package"], path + ("package",), limit=MAX_PATH_LENGTH)
        package_shasum = _sha1(item["packageShasum"], path + ("packageShasum",))
    return _construct(
        LaunchProfile,
        path,
        args=args,
        command=_string(item["command"], path + ("command",), limit=MAX_PATH_LENGTH),
        fresh_session=_boolean(item["freshSession"], path + ("freshSession",)),
        package=package,
        package_shasum=package_shasum,
        platform=_enum(item["platform"], Platform, path + ("platform",)),
        protocol=_string(item["protocol"], path + ("protocol",), limit=128),
        resume=_boolean(item["resume"], path + ("resume",)),
    )


def _features(value: object, path: DiagnosticPath) -> CapabilityFeatures:
    item = _object(
        value,
        path,
        frozenset(
            {
                "atomicChannelReservation",
                "callerControlledOperationIds",
                "freshProviderSessions",
            }
        ),
    )
    return _construct(
        CapabilityFeatures,
        path,
        atomic_channel_reservation=_boolean(
            item["atomicChannelReservation"], path + ("atomicChannelReservation",)
        ),
        caller_controlled_operation_ids=_boolean(
            item["callerControlledOperationIds"],
            path + ("callerControlledOperationIds",),
        ),
        fresh_provider_sessions=_boolean(
            item["freshProviderSessions"], path + ("freshProviderSessions",)
        ),
    )


def _operation_guarantee(value: object, path: DiagnosticPath) -> OperationGuarantee:
    item = _object(value, path, frozenset({"idempotent", "inspectable"}))
    return _construct(
        OperationGuarantee,
        path,
        idempotent=_boolean(item["idempotent"], path + ("idempotent",)),
        inspectable=_boolean(item["inspectable"], path + ("inspectable",)),
    )


def _capabilities(value: object, path: DiagnosticPath) -> IntegrationCapabilities:
    item = _object(
        value,
        path,
        frozenset(
            {
                "capabilityDigest",
                "features",
                "launchProfileDigest",
                "maxTaskPacketBytes",
                "operations",
                "platform",
                "policyDigest",
                "provider",
                "schemaVersion",
            }
        ),
    )
    operations_value = _object(
        item["operations"],
        path + ("operations",),
        frozenset(operation.value for operation in OPERATION_ORDER),
    )
    operations = tuple(
        (
            operation,
            _operation_guarantee(
                operations_value[operation.value],
                path + ("operations", operation.value),
            ),
        )
        for operation in OPERATION_ORDER
    )
    return _construct(
        IntegrationCapabilities,
        path,
        capability_digest=_sha256(
            item["capabilityDigest"], path + ("capabilityDigest",)
        ),
        features=_features(item["features"], path + ("features",)),
        launch_profile_digest=_sha256(
            item["launchProfileDigest"], path + ("launchProfileDigest",)
        ),
        max_task_packet_bytes=_integer(
            item["maxTaskPacketBytes"], path + ("maxTaskPacketBytes",)
        ),
        operations=operations,
        platform=_enum(item["platform"], Platform, path + ("platform",)),
        policy_digest=_sha256(item["policyDigest"], path + ("policyDigest",)),
        provider=_enum(item["provider"], Provider, path + ("provider",)),
        schema_version=_backend_capability_schema_version(
            item["schemaVersion"], path + ("schemaVersion",)
        ),
    )


def _qualification_scenario(
    value: object,
    path: DiagnosticPath,
) -> QualificationScenarioEvidence:
    item = _object(
        value,
        path,
        frozenset({"evidenceDigest", "live", "name", "status"}),
    )
    return _construct(
        QualificationScenarioEvidence,
        path,
        evidence_digest=_sha256(item["evidenceDigest"], path + ("evidenceDigest",)),
        live=_boolean(item["live"], path + ("live",)),
        name=_enum(item["name"], QualificationScenario, path + ("name",)),
        status=_enum(item["status"], QualificationStatus, path + ("status",)),
    )


def _disjoint_sibling_overlap(
    value: object,
    path: DiagnosticPath,
) -> DisjointSiblingOverlapEvidence:
    item = _object(
        value,
        path,
        frozenset(
            {
                "evidenceDigest",
                "observedConcurrentTurns",
                "siblingTaskIds",
                "ownedPathsDisjoint",
                "overlapObserved",
            }
        ),
    )
    return _construct(
        DisjointSiblingOverlapEvidence,
        path,
        evidence_digest=_sha256(item["evidenceDigest"], path + ("evidenceDigest",)),
        observed_concurrent_turns=_integer(
            item["observedConcurrentTurns"], path + ("observedConcurrentTurns",)
        ),
        sibling_task_ids=_string_array(
            item["siblingTaskIds"],
            path + ("siblingTaskIds",),
            maximum=MAX_QUALIFICATION_SIBLINGS,
        ),
        owned_paths_disjoint=_boolean(
            item["ownedPathsDisjoint"], path + ("ownedPathsDisjoint",)
        ),
        overlap_observed=_boolean(item["overlapObserved"], path + ("overlapObserved",)),
    )


def _qualification_artifact(
    value: object,
    path: DiagnosticPath,
) -> QualificationArtifact:
    item = _object(
        value,
        path,
        frozenset(
            {
                "artifactDigest",
                "capabilityDigest",
                "disjointSiblingOverlap",
                "harnessDigest",
                "harnessVersion",
                "launchProfileDigest",
                "maxConcurrentTurns",
                "observedMaxConcurrentTurns",
                "platform",
                "policyDigest",
                "provider",
                "scenarios",
                "schemaVersion",
                "sdk",
                "trellisCompatibilityDigest",
            }
        ),
    )
    overlap_value = item["disjointSiblingOverlap"]
    overlap = None
    if overlap_value is not None:
        overlap = _disjoint_sibling_overlap(
            overlap_value,
            path + ("disjointSiblingOverlap",),
        )
    scenarios_value = _object(
        item["scenarios"],
        path + ("scenarios",),
        frozenset(scenario.value for scenario in QualificationScenario),
    )
    return _construct(
        QualificationArtifact,
        path,
        artifact_digest=_sha256(item["artifactDigest"], path + ("artifactDigest",)),
        capability_digest=_sha256(
            item["capabilityDigest"], path + ("capabilityDigest",)
        ),
        disjoint_sibling_overlap=overlap,
        harness_digest=_sha256(item["harnessDigest"], path + ("harnessDigest",)),
        harness_version=_string(
            item["harnessVersion"], path + ("harnessVersion",), limit=128
        ),
        launch_profile_digest=_sha256(
            item["launchProfileDigest"], path + ("launchProfileDigest",)
        ),
        max_concurrent_turns=_integer(
            item["maxConcurrentTurns"], path + ("maxConcurrentTurns",)
        ),
        observed_max_concurrent_turns=_integer(
            item["observedMaxConcurrentTurns"],
            path + ("observedMaxConcurrentTurns",),
        ),
        platform=_enum(item["platform"], Platform, path + ("platform",)),
        policy_digest=_sha256(item["policyDigest"], path + ("policyDigest",)),
        provider=_enum(item["provider"], Provider, path + ("provider",)),
        scenarios=tuple(
            _qualification_scenario(
                scenarios_value[scenario.value],
                path + ("scenarios", scenario.value),
            )
            for scenario in QualificationScenario
        ),
        schema_version=_qualification_artifact_schema_version(
            item["schemaVersion"], path + ("schemaVersion",)
        ),
        sdk=_sdk(item["sdk"], path + ("sdk",)),
        trellis_compatibility_digest=_sha256(
            item["trellisCompatibilityDigest"],
            path + ("trellisCompatibilityDigest",),
        ),
    )


def _qualification(value: object, path: DiagnosticPath) -> Qualification:
    item = _object(
        value,
        path,
        frozenset(
            {
                "artifact",
                "enabledForDispatch",
                "evidence",
                "evidenceScope",
                "live",
                "note",
                "status",
            }
        ),
    )
    artifact_value = item["artifact"]
    artifact = (
        None
        if artifact_value is None
        else _qualification_artifact(artifact_value, path + ("artifact",))
    )
    return _construct(
        Qualification,
        path,
        artifact=artifact,
        enabled_for_dispatch=_boolean(
            item["enabledForDispatch"], path + ("enabledForDispatch",)
        ),
        evidence=_string_array(
            item["evidence"],
            path + ("evidence",),
            maximum=MAX_COMPATIBILITY_EVIDENCE,
        ),
        evidence_scope=_enum(
            item["evidenceScope"], EvidenceScope, path + ("evidenceScope",)
        ),
        live=_boolean(item["live"], path + ("live",)),
        note=_string(item["note"], path + ("note",)),
        status=_enum(item["status"], QualificationStatus, path + ("status",)),
    )


def _platform_cell(
    value: object,
    path: DiagnosticPath,
    provider: Provider,
) -> PlatformCompatibility:
    item = _object(
        value,
        path,
        frozenset(
            {
                "capabilities",
                "launchProfile",
                "launchProfileDigest",
                "platform",
                "qualification",
            }
        ),
    )
    return _construct(
        PlatformCompatibility,
        path,
        capabilities=_capabilities(item["capabilities"], path + ("capabilities",)),
        launch_profile=_launch_profile(
            item["launchProfile"], path + ("launchProfile",), provider
        ),
        launch_profile_digest=_sha256(
            item["launchProfileDigest"], path + ("launchProfileDigest",)
        ),
        platform=_enum(item["platform"], Platform, path + ("platform",)),
        qualification=_qualification(item["qualification"], path + ("qualification",)),
    )


def _provider(value: object, path: DiagnosticPath) -> ProviderCompatibility:
    item = _object(value, path, frozenset({"platforms", "provider", "sdk"}))
    provider = _enum(item["provider"], Provider, path + ("provider",))
    platforms_value = _array(
        item["platforms"],
        path + ("platforms",),
        nonempty=True,
        maximum=MAX_COMPATIBILITY_PLATFORMS,
    )
    platforms = tuple(
        _platform_cell(cell, path + ("platforms", index), provider)
        for index, cell in enumerate(platforms_value)
    )
    return _construct(
        ProviderCompatibility,
        path,
        platforms=platforms,
        provider=provider,
        sdk=_sdk(item["sdk"], path + ("sdk",)),
    )


def _decode_trellis_compatibility_shape(value: object) -> TrellisCompatibility:
    root = _object(
        value,
        (),
        frozenset(
            {
                "adapterQualification",
                "compatibilityDigest",
                "packages",
                "published",
                "schemaVersion",
                "trellisVersion",
            }
        ),
    )
    packages_value = _array(
        root["packages"],
        ("packages",),
        nonempty=True,
        maximum=MAX_COMPATIBILITY_PACKAGES,
    )
    return _construct(
        TrellisCompatibility,
        (),
        adapter_qualification=_adapter_qualification(
            root["adapterQualification"], ("adapterQualification",)
        ),
        compatibility_digest=_sha256(
            root["compatibilityDigest"], ("compatibilityDigest",)
        ),
        packages=tuple(
            _package(item, ("packages", index))
            for index, item in enumerate(packages_value)
        ),
        published=_boolean(root["published"], ("published",)),
        schema_version=_trellis_compatibility_schema_version(
            root["schemaVersion"], ("schemaVersion",)
        ),
        trellis_version=_string(root["trellisVersion"], ("trellisVersion",), limit=128),
    )


def _decode_bundle_shape(value: object) -> BackendQualificationBundle:
    root = _object(
        value,
        (),
        frozenset(
            {
                "bundleDigest",
                "policy",
                "policyDigest",
                "providers",
                "published",
                "schemaVersion",
                "trellisCompatibilityDigest",
            }
        ),
    )
    schema_version = _compatibility_schema_version(
        root["schemaVersion"], ("schemaVersion",)
    )
    providers_value = _array(
        root["providers"],
        ("providers",),
        nonempty=True,
        maximum=MAX_COMPATIBILITY_PROVIDERS,
    )
    return _construct(
        BackendQualificationBundle,
        (),
        bundle_digest=_sha256(root["bundleDigest"], ("bundleDigest",)),
        policy=_policy(root["policy"], ("policy",)),
        policy_digest=_sha256(root["policyDigest"], ("policyDigest",)),
        providers=tuple(
            _provider(item, ("providers", index))
            for index, item in enumerate(providers_value)
        ),
        published=_boolean(root["published"], ("published",)),
        schema_version=schema_version,
        trellis_compatibility_digest=_sha256(
            root["trellisCompatibilityDigest"],
            ("trellisCompatibilityDigest",),
        ),
    )


def _decode_error_issue(error: _CompatibilityDecodeError) -> ValidationIssue:
    return _issue(
        error.rule_id,
        error.path,
        error.reason_code.value,
        error.message,
        stage=(
            ValidationStage.BOUNDARY
            if error.rule_id.startswith("schema.")
            else ValidationStage.CAPABILITY
        ),
    )


def decode_trellis_compatibility_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[TrellisCompatibility]:
    """Decode official Trellis package compatibility through its closed schema."""

    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    shape_issues = _audit_shape(value, limits)
    if shape_issues:
        return DecodeResult(None, ValidationReport(shape_issues))
    try:
        compatibility = _decode_trellis_compatibility_shape(value)
    except _CompatibilityDecodeError as error:
        return DecodeResult(None, ValidationReport((_decode_error_issue(error),)))
    return DecodeResult(compatibility, ValidationReport(()))


def decode_trellis_compatibility_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[TrellisCompatibility]:
    """Decode strict UTF-8 JSON into immutable Trellis compatibility."""

    decoded_json = _decode_json_bytes(raw, limits=limits)
    if not decoded_json.ok:
        return DecodeResult(None, decoded_json.report, decoded_json.source_sha256)
    assert decoded_json.value is not None
    decoded = decode_trellis_compatibility_primitive(
        decoded_json.value.value, limits=limits
    )
    return DecodeResult(decoded.value, decoded.report, decoded_json.source_sha256)


def decode_backend_qualification_bundle_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[BackendQualificationBundle]:
    """Decode backend qualification independently from package compatibility."""

    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    shape_issues = _audit_shape(value, limits)
    if shape_issues:
        return DecodeResult(None, ValidationReport(shape_issues))
    try:
        bundle = _decode_bundle_shape(value)
    except _CompatibilityDecodeError as error:
        return DecodeResult(None, ValidationReport((_decode_error_issue(error),)))
    return DecodeResult(bundle, ValidationReport(()))


def decode_backend_qualification_bundle_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[BackendQualificationBundle]:
    """Decode strict UTF-8 JSON into an immutable backend qualification bundle."""

    decoded_json = _decode_json_bytes(raw, limits=limits)
    if not decoded_json.ok:
        return DecodeResult(None, decoded_json.report, decoded_json.source_sha256)
    assert decoded_json.value is not None
    decoded = decode_backend_qualification_bundle_primitive(
        decoded_json.value.value, limits=limits
    )
    return DecodeResult(decoded.value, decoded.report, decoded_json.source_sha256)


decode_compatibility_bundle_primitive = decode_backend_qualification_bundle_primitive
decode_compatibility_bundle_bytes = decode_backend_qualification_bundle_bytes
strict_decode_trellis_compatibility = decode_trellis_compatibility_bytes
strict_decode_backend_qualification_bundle = decode_backend_qualification_bundle_bytes
strict_decode_compatibility_bundle = decode_backend_qualification_bundle_bytes


__all__ = [
    "decode_backend_qualification_bundle_bytes",
    "decode_backend_qualification_bundle_primitive",
    "decode_compatibility_bundle_bytes",
    "decode_compatibility_bundle_primitive",
    "decode_trellis_compatibility_bytes",
    "decode_trellis_compatibility_primitive",
    "strict_decode_backend_qualification_bundle",
    "strict_decode_compatibility_bundle",
    "strict_decode_trellis_compatibility",
]
