"""Immutable Trellis adapter compatibility and backend qualification contracts."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum

from .models import HASH_RE, MAX_PATH_LENGTH, MAX_TEXT_LENGTH, _nonempty
from .serialization import canonical_json_bytes, canonical_sha256

TRELLIS_COMPATIBILITY_SCHEMA_VERSION = 1
ADAPTER_QUALIFICATION_SCHEMA_VERSION = 1
BACKEND_CAPABILITY_SCHEMA_VERSION = 2
BACKEND_QUALIFICATION_SCHEMA_VERSION = 5
COMPATIBILITY_SCHEMA_VERSION = BACKEND_QUALIFICATION_SCHEMA_VERSION
QUALIFICATION_ARTIFACT_SCHEMA_VERSION = 2
MAX_SAFE_JSON_INTEGER = 2**53 - 1
MAX_COMPATIBILITY_EVIDENCE = 64
MAX_COMPATIBILITY_PACKAGES = 2
MAX_COMPATIBILITY_PROVIDERS = 3
MAX_COMPATIBILITY_PLATFORMS = 2
MAX_QUALIFICATION_CONCURRENT_TURNS = 64
MAX_QUALIFICATION_SCENARIOS = 4
MAX_QUALIFICATION_SIBLINGS = 64
MAX_TASK_PACKET_BYTES = 1_048_576
SUPPORTED_TRELLIS_VERSION = "0.6.15"
SUPPORTED_TRELLIS_GRAPH_FORMAT = "wish-builder.trellis-graph.v1"

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
NPM_INTEGRITY_RE = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


class Provider(StrEnum):
    CODEX = "codex"
    OMP = "omp"
    PI = "pi"


class Platform(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"


class TrellisPackageRole(StrEnum):
    CLI = "cli"
    CORE = "core"


class AdapterQualificationStatus(StrEnum):
    PASSED = "passed"


class QualificationStatus(StrEnum):
    PASSED = "passed"
    BLOCKED_CREDENTIALS = "blocked_credentials"
    FIXTURE_CI_ONLY = "fixture_ci_only"


class EvidenceScope(StrEnum):
    DETERMINISTIC_FIXTURE_AND_CI = "deterministic_fixture_and_ci"
    DETERMINISTIC_FIXTURE_ONLY = "deterministic_fixture_only"
    STARTUP_AND_HANDSHAKE = "startup_and_handshake"
    FULL_TURN_AND_CANCELLATION = "full_turn_and_cancellation"


class QualificationScenario(StrEnum):
    FULL_TURN = "full_turn"
    ACTIVE_TURN_CANCELLATION = "active_turn_cancellation"
    CRASH_RECONCILE = "crash_reconcile"
    CLEANUP = "cleanup"


class CompatibilityOperation(StrEnum):
    CANCEL_TURN = "cancel_turn"
    RESERVE_CHANNEL = "reserve_channel"
    SEND_TASK_PACKET = "send_task_packet"


PROVIDER_ORDER = (Provider.CODEX, Provider.OMP, Provider.PI)
PLATFORM_ORDER = (Platform.LINUX, Platform.WINDOWS)
OPERATION_ORDER = (
    CompatibilityOperation.CANCEL_TURN,
    CompatibilityOperation.RESERVE_CHANNEL,
    CompatibilityOperation.SEND_TASK_PACKET,
)
PACKAGE_ORDER = ("@mindfoldhq/trellis", "@mindfoldhq/trellis-core")
PACKAGE_ROLE_ORDER = (TrellisPackageRole.CLI, TrellisPackageRole.CORE)
GRAPH_ADAPTER_EVIDENCE = (
    "wish_builder/bridges/trellis_core/graph-snapshot.mjs",
    "wish_builder/adapters/trellis/graph_snapshot.py",
    "wish_builder/adapters/trellis/graph.py",
    "wish_builder/services/trellis_graph_admission.py",
    "tests/node/trellis-graph-snapshot.test.mjs",
    "tests/adapters/test_trellis_graph_snapshot.py",
    "tests/adapters/test_trellis_graph_import.py",
)
PROJECTION_ADAPTER_EVIDENCE = (
    "wish_builder/bridges/trellis_core/projection.mjs",
    "wish_builder/adapters/trellis/projection.py",
    "tests/node/trellis-projection-bridge.test.mjs",
    "tests/adapters/test_trellis_projection.py",
)
QUALIFICATION_SCENARIO_ORDER = (
    QualificationScenario.FULL_TURN,
    QualificationScenario.ACTIVE_TURN_CANCELLATION,
    QualificationScenario.CRASH_RECONCILE,
    QualificationScenario.CLEANUP,
)
SDK_NAMES = {
    Provider.CODEX: "@openai/codex",
    Provider.OMP: "@oh-my-pi/pi-coding-agent",
    Provider.PI: "@earendil-works/pi-coding-agent",
}
PROVIDER_LAUNCH = {
    Provider.CODEX: (("app-server", "--stdio"), "codex-app-server-jsonl-stdio"),
    Provider.OMP: (("--mode", "rpc"), "omp-rpc-v2-jsonl-stdio"),
    Provider.PI: (("--mode", "rpc"), "pi-rpc-jsonl-stdio"),
}


def _enum(value: object, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a bool")
    return value


def _positive_safe_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if not 1 <= value <= MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"{field_name} must be a positive safe JSON integer")
    return value


def _version(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, 128)
    if not VERSION_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact semantic version")
    return normalized


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _sha1(value: object, field_name: str) -> str:
    if type(value) is not str or not SHA1_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-1 digest")
    return value


def _npm_integrity(value: object, field_name: str) -> str:
    if type(value) is not str or not NPM_INTEGRITY_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an npm sha512 integrity string")
    try:
        digest = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be an npm sha512 integrity string"
        ) from exc
    if len(digest) != 64:
        raise ValueError(f"{field_name} must contain a full SHA-512 digest")
    return value


def _string_tuple(
    value: object,
    field_name: str,
    *,
    nonempty: bool,
    max_items: int,
    unique: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if nonempty and not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds the item limit")
    normalized = tuple(
        _nonempty(item, f"{field_name}[{index}]", MAX_TEXT_LENGTH)
        for index, item in enumerate(value)
    )
    if unique and len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class NpmProvenance:
    attestation_url: str
    predicate_type: str

    def __post_init__(self) -> None:
        attestation_url = _nonempty(
            self.attestation_url, "attestation_url", MAX_PATH_LENGTH
        )
        if not attestation_url.startswith(
            "https://registry.npmjs.org/-/npm/v1/attestations/"
        ):
            raise ValueError("attestation_url must identify the npm registry")
        if self.predicate_type != "https://slsa.dev/provenance/v1":
            raise ValueError("predicate_type must identify SLSA provenance v1")
        object.__setattr__(self, "attestation_url", attestation_url)

    def to_primitive(self) -> dict[str, object]:
        return {
            "attestationUrl": self.attestation_url,
            "predicateType": self.predicate_type,
        }


@dataclass(frozen=True, slots=True)
class TrellisPackage:
    filename: str
    integrity: str
    name: str
    provenance: NpmProvenance
    role: TrellisPackageRole
    sha256: str
    shasum: str
    size: int
    tarball_url: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "filename", _nonempty(self.filename, "filename", MAX_PATH_LENGTH)
        )
        object.__setattr__(
            self, "integrity", _npm_integrity(self.integrity, "integrity")
        )
        object.__setattr__(self, "name", _nonempty(self.name, "name", 128))
        if type(self.provenance) is not NpmProvenance:
            raise TypeError("provenance must be NpmProvenance")
        object.__setattr__(self, "role", _enum(self.role, TrellisPackageRole, "role"))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "sha256"))
        object.__setattr__(self, "shasum", _sha1(self.shasum, "shasum"))
        object.__setattr__(self, "size", _positive_safe_integer(self.size, "size"))
        tarball_url = _nonempty(self.tarball_url, "tarball_url", MAX_PATH_LENGTH)
        if not tarball_url.startswith("https://registry.npmjs.org/"):
            raise ValueError("tarball_url must identify the npm registry")
        object.__setattr__(self, "tarball_url", tarball_url)
        object.__setattr__(self, "version", _version(self.version, "version"))

    def to_primitive(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "name": self.name,
            "npmIntegrity": self.integrity,
            "npmShasum": self.shasum,
            "provenance": self.provenance.to_primitive(),
            "role": self.role.value,
            "sha256": self.sha256,
            "size": self.size,
            "tarballUrl": self.tarball_url,
            "version": self.version,
        }


CompatibilityPackage = TrellisPackage


@dataclass(frozen=True, slots=True)
class TrellisGraphAdapterQualification:
    derived_format: str
    deterministic_snapshot: bool
    evidence: tuple[str, ...]
    read_only_snapshot: bool
    result: AdapterQualificationStatus
    strict_import: bool

    def __post_init__(self) -> None:
        derived_format = _nonempty(
            self.derived_format, "derived_format", MAX_PATH_LENGTH
        )
        if derived_format != SUPPORTED_TRELLIS_GRAPH_FORMAT:
            raise ValueError("graph derived_format is unsupported")
        expected_booleans = {
            "deterministic_snapshot": True,
            "read_only_snapshot": True,
            "strict_import": True,
        }
        for field_name, expected in expected_booleans.items():
            actual = _boolean(getattr(self, field_name), field_name)
            if actual is not expected:
                raise ValueError(f"graph {field_name} must be {expected}")
        evidence = _string_tuple(
            self.evidence,
            "graph evidence",
            nonempty=True,
            max_items=len(GRAPH_ADAPTER_EVIDENCE),
            unique=True,
        )
        if evidence != GRAPH_ADAPTER_EVIDENCE:
            raise ValueError("graph evidence must match the canonical evidence set")
        result = _enum(self.result, AdapterQualificationStatus, "graph result")
        if result is not AdapterQualificationStatus.PASSED:
            raise ValueError("graph qualification must have passed")
        object.__setattr__(self, "derived_format", derived_format)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "result", result)

    def to_primitive(self) -> dict[str, object]:
        return {
            "derivedFormat": self.derived_format,
            "deterministicSnapshot": self.deterministic_snapshot,
            "evidence": list(self.evidence),
            "readOnlySnapshot": self.read_only_snapshot,
            "result": self.result.value,
            "strictImport": self.strict_import,
        }


@dataclass(frozen=True, slots=True)
class TrellisProjectionAdapterQualification:
    concurrent_projection_writers_safe: bool
    cross_process_cas: bool
    digest_guarded: bool
    evidence: tuple[str, ...]
    expected_revision_guarded: bool
    isolated_checkout: bool
    post_write_verified: bool
    result: AdapterQualificationStatus
    single_writer: bool

    def __post_init__(self) -> None:
        required_true = {
            "digest_guarded": True,
            "expected_revision_guarded": True,
            "post_write_verified": True,
            "single_writer": True,
        }
        for field_name, expected in required_true.items():
            actual = _boolean(getattr(self, field_name), field_name)
            if actual is not expected:
                raise ValueError(f"projection {field_name} must be {expected}")
        required_false = {
            "concurrent_projection_writers_safe": False,
            "cross_process_cas": False,
            "isolated_checkout": False,
        }
        for field_name, expected in required_false.items():
            actual = _boolean(getattr(self, field_name), field_name)
            if actual is not expected:
                raise ValueError(f"projection {field_name} must be {expected}")
        evidence = _string_tuple(
            self.evidence,
            "projection evidence",
            nonempty=True,
            max_items=len(PROJECTION_ADAPTER_EVIDENCE),
            unique=True,
        )
        if evidence != PROJECTION_ADAPTER_EVIDENCE:
            raise ValueError(
                "projection evidence must match the canonical evidence set"
            )
        result = _enum(self.result, AdapterQualificationStatus, "projection result")
        if result is not AdapterQualificationStatus.PASSED:
            raise ValueError("projection qualification must have passed")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "result", result)

    def to_primitive(self) -> dict[str, object]:
        return {
            "concurrentProjectionWritersSafe": self.concurrent_projection_writers_safe,
            "crossProcessCas": self.cross_process_cas,
            "digestGuarded": self.digest_guarded,
            "evidence": list(self.evidence),
            "expectedRevisionGuarded": self.expected_revision_guarded,
            "isolatedCheckout": self.isolated_checkout,
            "postWriteVerified": self.post_write_verified,
            "result": self.result.value,
            "singleWriter": self.single_writer,
        }


@dataclass(frozen=True, slots=True)
class TrellisAdapterQualification:
    graph: TrellisGraphAdapterQualification
    projection: TrellisProjectionAdapterQualification
    qualification_digest: str
    schema_version: int
    trellis_version: str

    def __post_init__(self) -> None:
        if type(self.graph) is not TrellisGraphAdapterQualification:
            raise TypeError("graph must be TrellisGraphAdapterQualification")
        if type(self.projection) is not TrellisProjectionAdapterQualification:
            raise TypeError("projection must be TrellisProjectionAdapterQualification")
        qualification_digest = _sha256(
            self.qualification_digest, "qualification_digest"
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version != ADAPTER_QUALIFICATION_SCHEMA_VERSION
        ):
            raise ValueError("adapter qualification schema_version must be 1")
        trellis_version = _version(self.trellis_version, "trellis_version")
        if trellis_version != SUPPORTED_TRELLIS_VERSION:
            raise ValueError("adapter qualification targets unsupported Trellis")
        object.__setattr__(self, "qualification_digest", qualification_digest)
        object.__setattr__(self, "trellis_version", trellis_version)
        expected = "sha256:" + canonical_sha256(self.body_primitive())
        if qualification_digest != expected:
            raise ValueError(
                "qualification_digest does not match the adapter qualification body"
            )

    def body_primitive(self) -> dict[str, object]:
        return {
            "graph": self.graph.to_primitive(),
            "projection": self.projection.to_primitive(),
            "schemaVersion": self.schema_version,
            "trellisVersion": self.trellis_version,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.body_primitive()
        result["qualificationDigest"] = self.qualification_digest
        return result


@dataclass(frozen=True, slots=True)
class TrellisCompatibility:
    adapter_qualification: TrellisAdapterQualification
    compatibility_digest: str
    packages: tuple[TrellisPackage, ...]
    published: bool
    schema_version: int
    trellis_version: str

    def __post_init__(self) -> None:
        if type(self.adapter_qualification) is not TrellisAdapterQualification:
            raise TypeError("adapter_qualification must be TrellisAdapterQualification")
        compatibility_digest = _sha256(
            self.compatibility_digest, "compatibility_digest"
        )
        if type(self.packages) is not tuple or not all(
            type(item) is TrellisPackage for item in self.packages
        ):
            raise TypeError("packages must contain TrellisPackage values")
        if tuple(item.name for item in self.packages) != PACKAGE_ORDER:
            raise ValueError("packages must contain the exact Trellis package set")
        if tuple(item.role for item in self.packages) != PACKAGE_ROLE_ORDER:
            raise ValueError("packages must contain CLI and Core in canonical order")
        if _boolean(self.published, "published") is not True:
            raise ValueError(
                "Wish Builder Trellis compatibility record must be published"
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRELLIS_COMPATIBILITY_SCHEMA_VERSION
        ):
            raise ValueError("Trellis compatibility schema_version must be 1")
        trellis_version = _version(self.trellis_version, "trellis_version")
        if trellis_version != SUPPORTED_TRELLIS_VERSION:
            raise ValueError("Trellis compatibility targets an unsupported version")
        if self.adapter_qualification.trellis_version != trellis_version:
            raise ValueError(
                "adapter qualification Trellis version does not match compatibility"
            )
        for package in self.packages:
            if package.version != trellis_version:
                raise ValueError("package version does not match Trellis version")
            suffix = (
                "trellis" if package.role is TrellisPackageRole.CLI else "trellis-core"
            )
            if package.filename != f"mindfoldhq-{suffix}-{trellis_version}.tgz":
                raise ValueError("package filename does not match its identity")
            expected_tarball = (
                f"https://registry.npmjs.org/{package.name}/-/"
                f"{suffix}-{trellis_version}.tgz"
            )
            if package.tarball_url != expected_tarball:
                raise ValueError("package tarball URL does not match its identity")
            expected_attestation = (
                "https://registry.npmjs.org/-/npm/v1/attestations/"
                f"{package.name.replace('/', '%2f')}@{trellis_version}"
            )
            if package.provenance.attestation_url != expected_attestation:
                raise ValueError("package provenance does not match its identity")
        object.__setattr__(self, "compatibility_digest", compatibility_digest)
        object.__setattr__(self, "trellis_version", trellis_version)
        expected = "sha256:" + canonical_sha256(self.body_primitive())
        if compatibility_digest != expected:
            raise ValueError(
                "compatibility_digest does not match the Trellis compatibility body"
            )

    def body_primitive(self) -> dict[str, object]:
        return {
            "adapterQualification": self.adapter_qualification.to_primitive(),
            "packages": [item.to_primitive() for item in self.packages],
            "published": self.published,
            "schemaVersion": self.schema_version,
            "trellisVersion": self.trellis_version,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.body_primitive()
        result["compatibilityDigest"] = self.compatibility_digest
        return result

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())


@dataclass(frozen=True, slots=True)
class CompatibilityPolicy:
    credentials_managed_by: str
    fresh_attempt_worktree: bool
    fresh_provider_session: bool
    max_task_packet_bytes: int
    one_provider_per_run: bool
    provider_fallback: bool
    provider_native_sibling_scheduling: bool
    scheduler_mode: str
    schema_version: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("policy schema_version must be 1")
        if self.scheduler_mode != "wish_builder":
            raise ValueError("policy scheduler_mode must be wish_builder")
        if self.credentials_managed_by != "provider":
            raise ValueError("policy credentials_managed_by must be provider")
        if self.max_task_packet_bytes != MAX_TASK_PACKET_BYTES:
            raise ValueError("policy max_task_packet_bytes is unsupported")
        expected_booleans = {
            "fresh_attempt_worktree": True,
            "fresh_provider_session": True,
            "one_provider_per_run": True,
            "provider_fallback": False,
            "provider_native_sibling_scheduling": False,
        }
        for field_name, expected in expected_booleans.items():
            actual = _boolean(getattr(self, field_name), field_name)
            if actual is not expected:
                raise ValueError(f"policy {field_name} must be {expected}")

    def to_primitive(self) -> dict[str, object]:
        return {
            "credentialsManagedBy": self.credentials_managed_by,
            "freshAttemptWorktree": self.fresh_attempt_worktree,
            "freshProviderSession": self.fresh_provider_session,
            "maxTaskPacketBytes": self.max_task_packet_bytes,
            "oneProviderPerRun": self.one_provider_per_run,
            "providerFallback": self.provider_fallback,
            "providerNativeSiblingScheduling": (
                self.provider_native_sibling_scheduling
            ),
            "schedulerMode": self.scheduler_mode,
            "schemaVersion": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class SdkPin:
    name: str
    shasum: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "name", 128))
        object.__setattr__(self, "shasum", _sha1(self.shasum, "shasum"))
        object.__setattr__(self, "version", _version(self.version, "version"))

    def to_primitive(self) -> dict[str, object]:
        return {"name": self.name, "shasum": self.shasum, "version": self.version}


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    args: tuple[str, ...]
    command: str
    fresh_session: bool
    platform: Platform
    protocol: str
    resume: bool
    package: str | None = None
    package_shasum: str | None = None

    def __post_init__(self) -> None:
        args = _string_tuple(self.args, "args", nonempty=True, max_items=16)
        command = _nonempty(self.command, "command", MAX_PATH_LENGTH)
        platform = _enum(self.platform, Platform, "platform")
        protocol = _nonempty(self.protocol, "protocol", 128)
        fresh_session = _boolean(self.fresh_session, "fresh_session")
        resume = _boolean(self.resume, "resume")
        package = (
            None
            if self.package is None
            else _nonempty(self.package, "package", MAX_PATH_LENGTH)
        )
        package_shasum = (
            None
            if self.package_shasum is None
            else _sha1(self.package_shasum, "package_shasum")
        )
        if fresh_session is not True or resume is not False:
            raise ValueError("launch profiles must start a fresh non-resumed session")
        if (package is None) is not (package_shasum is None):
            raise ValueError("launch package and shasum must be present together")
        object.__setattr__(self, "args", args)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "package", package)
        object.__setattr__(self, "package_shasum", package_shasum)

    def to_primitive(self) -> dict[str, object]:
        result: dict[str, object] = {
            "args": list(self.args),
            "command": self.command,
            "freshSession": self.fresh_session,
        }
        if self.package is not None:
            result["package"] = self.package
            result["packageShasum"] = self.package_shasum
        result.update(
            {
                "platform": self.platform.value,
                "protocol": self.protocol,
                "resume": self.resume,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class CapabilityFeatures:
    atomic_channel_reservation: bool
    caller_controlled_operation_ids: bool
    fresh_provider_sessions: bool

    def __post_init__(self) -> None:
        for field_name in (
            "atomic_channel_reservation",
            "caller_controlled_operation_ids",
            "fresh_provider_sessions",
        ):
            if _boolean(getattr(self, field_name), field_name) is not True:
                raise ValueError(f"capability feature {field_name} must be true")

    def to_primitive(self) -> dict[str, object]:
        return {
            "atomicChannelReservation": self.atomic_channel_reservation,
            "callerControlledOperationIds": self.caller_controlled_operation_ids,
            "freshProviderSessions": self.fresh_provider_sessions,
        }


@dataclass(frozen=True, slots=True)
class OperationGuarantee:
    idempotent: bool
    inspectable: bool

    def __post_init__(self) -> None:
        if _boolean(self.idempotent, "idempotent") is not True:
            raise ValueError("compatibility operations must be idempotent")
        if _boolean(self.inspectable, "inspectable") is not True:
            raise ValueError("compatibility operations must be inspectable")

    def to_primitive(self) -> dict[str, object]:
        return {"idempotent": self.idempotent, "inspectable": self.inspectable}


@dataclass(frozen=True, slots=True)
class IntegrationCapabilities:
    capability_digest: str
    features: CapabilityFeatures
    launch_profile_digest: str
    max_task_packet_bytes: int
    operations: tuple[tuple[CompatibilityOperation, OperationGuarantee], ...]
    platform: Platform
    policy_digest: str
    provider: Provider
    schema_version: int

    def __post_init__(self) -> None:
        capability_digest = _sha256(self.capability_digest, "capability_digest")
        if type(self.features) is not CapabilityFeatures:
            raise TypeError("features must be CapabilityFeatures")
        launch_profile_digest = _sha256(
            self.launch_profile_digest, "launch_profile_digest"
        )
        if self.max_task_packet_bytes != MAX_TASK_PACKET_BYTES:
            raise ValueError("capability max_task_packet_bytes is unsupported")
        if type(self.operations) is not tuple:
            raise TypeError("operations must be a tuple")
        if not all(
            type(item) is tuple
            and len(item) == 2
            and isinstance(item[0], CompatibilityOperation)
            and type(item[1]) is OperationGuarantee
            for item in self.operations
        ):
            raise TypeError("operations contains an invalid entry")
        if tuple(item[0] for item in self.operations) != OPERATION_ORDER:
            raise ValueError("operations must contain the exact backend operation set")
        platform = _enum(self.platform, Platform, "platform")
        policy_digest = _sha256(self.policy_digest, "policy_digest")
        provider = _enum(self.provider, Provider, "provider")
        if (
            type(self.schema_version) is not int
            or self.schema_version != BACKEND_CAPABILITY_SCHEMA_VERSION
        ):
            raise ValueError("backend capability schema_version must be 2")
        object.__setattr__(self, "capability_digest", capability_digest)
        object.__setattr__(self, "launch_profile_digest", launch_profile_digest)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "provider", provider)
        expected = "sha256:" + canonical_sha256(self.body_primitive())
        if self.capability_digest != expected:
            raise ValueError("capability_digest does not match the capability body")

    def body_primitive(self) -> dict[str, object]:
        return {
            "features": self.features.to_primitive(),
            "launchProfileDigest": self.launch_profile_digest,
            "maxTaskPacketBytes": self.max_task_packet_bytes,
            "operations": {
                operation.value: guarantee.to_primitive()
                for operation, guarantee in self.operations
            },
            "platform": self.platform.value,
            "policyDigest": self.policy_digest,
            "provider": self.provider.value,
            "schemaVersion": self.schema_version,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.body_primitive()
        result["capabilityDigest"] = self.capability_digest
        return result


@dataclass(frozen=True, slots=True)
class QualificationScenarioEvidence:
    evidence_digest: str
    live: bool
    name: QualificationScenario
    status: QualificationStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_digest", _sha256(self.evidence_digest, "evidence_digest")
        )
        if _boolean(self.live, "live") is not True:
            raise ValueError("qualification scenario evidence must be live")
        object.__setattr__(
            self,
            "name",
            _enum(self.name, QualificationScenario, "name"),
        )
        status = _enum(self.status, QualificationStatus, "status")
        if status is not QualificationStatus.PASSED:
            raise ValueError("qualification scenario evidence must have passed")
        object.__setattr__(self, "status", status)

    def to_primitive(self) -> dict[str, object]:
        return {
            "evidenceDigest": self.evidence_digest,
            "live": self.live,
            "name": self.name.value,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class DisjointSiblingOverlapEvidence:
    evidence_digest: str
    observed_concurrent_turns: int
    sibling_task_ids: tuple[str, ...]
    owned_paths_disjoint: bool
    overlap_observed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_digest", _sha256(self.evidence_digest, "evidence_digest")
        )
        observed = _positive_safe_integer(
            self.observed_concurrent_turns, "observed_concurrent_turns"
        )
        if not 2 <= observed <= MAX_QUALIFICATION_CONCURRENT_TURNS:
            raise ValueError("observed_concurrent_turns must be between 2 and 64")
        sibling_task_ids = _string_tuple(
            self.sibling_task_ids,
            "sibling_task_ids",
            nonempty=True,
            max_items=MAX_QUALIFICATION_SIBLINGS,
            unique=True,
        )
        if len(sibling_task_ids) < 2:
            raise ValueError("sibling_task_ids must contain at least two tasks")
        if len(sibling_task_ids) < observed:
            raise ValueError(
                "sibling_task_ids must cover every observed concurrent turn"
            )
        if _boolean(self.owned_paths_disjoint, "owned_paths_disjoint") is not True:
            raise ValueError("sibling overlap must prove disjoint owned paths")
        if _boolean(self.overlap_observed, "overlap_observed") is not True:
            raise ValueError("sibling overlap must have been observed")
        object.__setattr__(self, "sibling_task_ids", sibling_task_ids)

    def to_primitive(self) -> dict[str, object]:
        return {
            "evidenceDigest": self.evidence_digest,
            "observedConcurrentTurns": self.observed_concurrent_turns,
            "siblingTaskIds": list(self.sibling_task_ids),
            "ownedPathsDisjoint": self.owned_paths_disjoint,
            "overlapObserved": self.overlap_observed,
        }


@dataclass(frozen=True, slots=True)
class QualificationArtifact:
    artifact_digest: str
    capability_digest: str
    disjoint_sibling_overlap: DisjointSiblingOverlapEvidence | None
    harness_digest: str
    harness_version: str
    launch_profile_digest: str
    max_concurrent_turns: int
    observed_max_concurrent_turns: int
    platform: Platform
    policy_digest: str
    provider: Provider
    scenarios: tuple[QualificationScenarioEvidence, ...]
    schema_version: int
    sdk: SdkPin
    trellis_compatibility_digest: str

    def __post_init__(self) -> None:
        artifact_digest = _sha256(self.artifact_digest, "artifact_digest")
        capability_digest = _sha256(self.capability_digest, "capability_digest")
        overlap = self.disjoint_sibling_overlap
        if overlap is not None and type(overlap) is not DisjointSiblingOverlapEvidence:
            raise TypeError(
                "disjoint_sibling_overlap must be "
                "DisjointSiblingOverlapEvidence or None"
            )
        harness_digest = _sha256(self.harness_digest, "harness_digest")
        harness_version = _nonempty(self.harness_version, "harness_version", 128)
        launch_profile_digest = _sha256(
            self.launch_profile_digest, "launch_profile_digest"
        )
        max_concurrent_turns = _positive_safe_integer(
            self.max_concurrent_turns, "max_concurrent_turns"
        )
        observed_max_concurrent_turns = _positive_safe_integer(
            self.observed_max_concurrent_turns, "observed_max_concurrent_turns"
        )
        if (
            max_concurrent_turns > MAX_QUALIFICATION_CONCURRENT_TURNS
            or observed_max_concurrent_turns > MAX_QUALIFICATION_CONCURRENT_TURNS
        ):
            raise ValueError("qualification concurrency cannot exceed 64 turns")
        platform = _enum(self.platform, Platform, "platform")
        policy_digest = _sha256(self.policy_digest, "policy_digest")
        provider = _enum(self.provider, Provider, "provider")
        if type(self.scenarios) is not tuple or not all(
            type(item) is QualificationScenarioEvidence for item in self.scenarios
        ):
            raise TypeError(
                "scenarios must contain QualificationScenarioEvidence values"
            )
        if tuple(item.name for item in self.scenarios) != (
            QUALIFICATION_SCENARIO_ORDER
        ):
            raise ValueError(
                "scenarios must contain the exact qualification scenario set "
                "in canonical order"
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version != QUALIFICATION_ARTIFACT_SCHEMA_VERSION
        ):
            raise ValueError("qualification artifact schema_version must be 2")
        if type(self.sdk) is not SdkPin:
            raise TypeError("sdk must be SdkPin")
        trellis_compatibility_digest = _sha256(
            self.trellis_compatibility_digest, "trellis_compatibility_digest"
        )
        if max_concurrent_turns > observed_max_concurrent_turns:
            raise ValueError(
                "max_concurrent_turns cannot exceed observed_max_concurrent_turns"
            )
        concurrent = max_concurrent_turns > 1 or observed_max_concurrent_turns > 1
        if concurrent and (
            overlap is None
            or overlap.observed_concurrent_turns < max_concurrent_turns
            or overlap.observed_concurrent_turns > observed_max_concurrent_turns
        ):
            raise ValueError(
                "concurrent qualification requires sufficient disjoint sibling "
                "overlap evidence"
            )
        if not concurrent and overlap is not None:
            raise ValueError("serial qualification must not claim sibling overlap")
        object.__setattr__(self, "artifact_digest", artifact_digest)
        object.__setattr__(self, "capability_digest", capability_digest)
        object.__setattr__(self, "harness_digest", harness_digest)
        object.__setattr__(self, "harness_version", harness_version)
        object.__setattr__(self, "launch_profile_digest", launch_profile_digest)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self,
            "trellis_compatibility_digest",
            trellis_compatibility_digest,
        )
        expected_artifact_digest = "sha256:" + canonical_sha256(self.body_primitive())
        if artifact_digest != expected_artifact_digest:
            raise ValueError(
                "artifact_digest does not match the qualification artifact body"
            )

    def body_primitive(self) -> dict[str, object]:
        return {
            "capabilityDigest": self.capability_digest,
            "disjointSiblingOverlap": (
                None
                if self.disjoint_sibling_overlap is None
                else self.disjoint_sibling_overlap.to_primitive()
            ),
            "harnessDigest": self.harness_digest,
            "harnessVersion": self.harness_version,
            "launchProfileDigest": self.launch_profile_digest,
            "maxConcurrentTurns": self.max_concurrent_turns,
            "observedMaxConcurrentTurns": self.observed_max_concurrent_turns,
            "platform": self.platform.value,
            "policyDigest": self.policy_digest,
            "provider": self.provider.value,
            "scenarios": {
                item.name.value: item.to_primitive() for item in self.scenarios
            },
            "schemaVersion": self.schema_version,
            "sdk": self.sdk.to_primitive(),
            "trellisCompatibilityDigest": self.trellis_compatibility_digest,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.body_primitive()
        result["artifactDigest"] = self.artifact_digest
        return result

@dataclass(frozen=True, slots=True)
class Qualification:
    artifact: QualificationArtifact | None
    enabled_for_dispatch: bool
    evidence: tuple[str, ...]
    evidence_scope: EvidenceScope
    live: bool
    note: str
    status: QualificationStatus

    def __post_init__(self) -> None:
        artifact = self.artifact
        if artifact is not None and type(artifact) is not QualificationArtifact:
            raise TypeError("artifact must be a QualificationArtifact or None")
        enabled = _boolean(self.enabled_for_dispatch, "enabled_for_dispatch")
        evidence = _string_tuple(
            self.evidence,
            "evidence",
            nonempty=True,
            max_items=MAX_COMPATIBILITY_EVIDENCE,
            unique=True,
        )
        scope = _enum(self.evidence_scope, EvidenceScope, "evidence_scope")
        live = _boolean(self.live, "live")
        note = _nonempty(self.note, "note", MAX_TEXT_LENGTH)
        status = _enum(self.status, QualificationStatus, "status")
        if status is QualificationStatus.PASSED:
            if not live:
                raise ValueError("passed qualification must contain live evidence")
            if scope not in {
                EvidenceScope.STARTUP_AND_HANDSHAKE,
                EvidenceScope.FULL_TURN_AND_CANCELLATION,
            }:
                raise ValueError("passed qualification has an invalid evidence scope")
        elif live:
            raise ValueError("only passed qualification can be live")
        if status is QualificationStatus.BLOCKED_CREDENTIALS and (
            scope is not EvidenceScope.DETERMINISTIC_FIXTURE_ONLY
        ):
            raise ValueError("blocked credentials must use fixture-only evidence")
        if status is QualificationStatus.FIXTURE_CI_ONLY and (
            scope is not EvidenceScope.DETERMINISTIC_FIXTURE_AND_CI
        ):
            raise ValueError("fixture_ci_only must use fixture-and-CI evidence")
        if enabled and (
            status is not QualificationStatus.PASSED
            or not live
            or scope is not EvidenceScope.FULL_TURN_AND_CANCELLATION
        ):
            raise ValueError(
                "dispatch requires passed full-turn-and-cancellation live conformance"
            )
        if enabled and artifact is None:
            raise ValueError("dispatch requires a qualification artifact")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "evidence_scope", scope)
        object.__setattr__(self, "note", note)
        object.__setattr__(self, "status", status)

    def to_primitive(self) -> dict[str, object]:
        return {
            "artifact": None if self.artifact is None else self.artifact.to_primitive(),
            "enabledForDispatch": self.enabled_for_dispatch,
            "evidence": list(self.evidence),
            "evidenceScope": self.evidence_scope.value,
            "live": self.live,
            "note": self.note,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class PlatformCompatibility:
    capabilities: IntegrationCapabilities
    launch_profile: LaunchProfile
    launch_profile_digest: str
    platform: Platform
    qualification: Qualification

    def __post_init__(self) -> None:
        if type(self.capabilities) is not IntegrationCapabilities:
            raise TypeError("capabilities must be IntegrationCapabilities")
        if type(self.launch_profile) is not LaunchProfile:
            raise TypeError("launch_profile must be LaunchProfile")
        launch_profile_digest = _sha256(
            self.launch_profile_digest, "launch_profile_digest"
        )
        platform = _enum(self.platform, Platform, "platform")
        if type(self.qualification) is not Qualification:
            raise TypeError("qualification must be Qualification")
        if self.launch_profile.platform is not platform:
            raise ValueError("launch profile platform does not match its cell")
        if self.capabilities.platform is not platform:
            raise ValueError("capability platform does not match its cell")
        expected = "sha256:" + canonical_sha256(self.launch_profile.to_primitive())
        if launch_profile_digest != expected:
            raise ValueError("launch_profile_digest does not match the launch profile")
        if self.capabilities.launch_profile_digest != launch_profile_digest:
            raise ValueError("capability launch profile digest does not match its cell")
        artifact = self.qualification.artifact
        if artifact is not None:
            if artifact.platform is not platform:
                raise ValueError(
                    "qualification artifact platform does not match its cell"
                )
            if artifact.launch_profile_digest != launch_profile_digest:
                raise ValueError(
                    "qualification artifact launch profile digest does not match its cell"
                )
            if artifact.capability_digest != self.capabilities.capability_digest:
                raise ValueError(
                    "qualification artifact capability digest does not match its cell"
                )
        object.__setattr__(self, "launch_profile_digest", launch_profile_digest)
        object.__setattr__(self, "platform", platform)

    def to_primitive(self) -> dict[str, object]:
        return {
            "capabilities": self.capabilities.to_primitive(),
            "launchProfile": self.launch_profile.to_primitive(),
            "launchProfileDigest": self.launch_profile_digest,
            "platform": self.platform.value,
            "qualification": self.qualification.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class ProviderCompatibility:
    platforms: tuple[PlatformCompatibility, ...]
    provider: Provider
    sdk: SdkPin

    def __post_init__(self) -> None:
        if type(self.platforms) is not tuple or not all(
            type(item) is PlatformCompatibility for item in self.platforms
        ):
            raise TypeError("platforms must contain PlatformCompatibility values")
        provider = _enum(self.provider, Provider, "provider")
        if type(self.sdk) is not SdkPin:
            raise TypeError("sdk must be SdkPin")
        if tuple(item.platform for item in self.platforms) != PLATFORM_ORDER:
            raise ValueError(
                "provider must contain Linux and Windows in canonical order"
            )
        if self.sdk.name != SDK_NAMES[provider]:
            raise ValueError("provider SDK package name does not match")
        expected_args, expected_protocol = PROVIDER_LAUNCH[provider]
        for cell in self.platforms:
            if cell.capabilities.provider is not provider:
                raise ValueError("capability provider does not match its parent")
            profile = cell.launch_profile
            expected_command = provider.value + (
                ".cmd" if cell.platform is Platform.WINDOWS else ""
            )
            if profile.command != expected_command:
                raise ValueError("launch command does not match provider and platform")
            if profile.args != expected_args or profile.protocol != expected_protocol:
                raise ValueError("launch protocol does not match provider")
            if provider is Provider.CODEX:
                if profile.package is not None or profile.package_shasum is not None:
                    raise ValueError(
                        "Codex launch profile must not duplicate SDK metadata"
                    )
            else:
                expected_package = f"{self.sdk.name}@{self.sdk.version}"
                if (
                    profile.package != expected_package
                    or profile.package_shasum != self.sdk.shasum
                ):
                    raise ValueError(
                        "launch package does not match the provider SDK pin"
                    )
            artifact = cell.qualification.artifact
            if artifact is not None:
                if artifact.provider is not provider:
                    raise ValueError(
                        "qualification artifact provider does not match its parent"
                    )
                if artifact.sdk != self.sdk:
                    raise ValueError(
                        "qualification artifact SDK pin does not match its parent"
                    )
        object.__setattr__(self, "provider", provider)

    def to_primitive(self) -> dict[str, object]:
        return {
            "platforms": [item.to_primitive() for item in self.platforms],
            "provider": self.provider.value,
            "sdk": self.sdk.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class BackendQualificationBundle:
    bundle_digest: str
    policy: CompatibilityPolicy
    policy_digest: str
    providers: tuple[ProviderCompatibility, ...]
    published: bool
    schema_version: int
    trellis_compatibility_digest: str

    def __post_init__(self) -> None:
        bundle_digest = _sha256(self.bundle_digest, "bundle_digest")
        if type(self.policy) is not CompatibilityPolicy:
            raise TypeError("policy must be CompatibilityPolicy")
        policy_digest = _sha256(self.policy_digest, "policy_digest")
        if type(self.providers) is not tuple or not all(
            type(item) is ProviderCompatibility for item in self.providers
        ):
            raise TypeError("providers must contain ProviderCompatibility values")
        if tuple(item.provider for item in self.providers) != PROVIDER_ORDER:
            raise ValueError(
                "providers must contain Codex, OMP, and Pi in canonical order"
            )
        published = _boolean(self.published, "published")
        if (
            type(self.schema_version) is not int
            or self.schema_version != BACKEND_QUALIFICATION_SCHEMA_VERSION
        ):
            raise ValueError("backend qualification schema_version must be 5")
        trellis_compatibility_digest = _sha256(
            self.trellis_compatibility_digest, "trellis_compatibility_digest"
        )
        expected_policy_digest = "sha256:" + canonical_sha256(
            self.policy.to_primitive()
        )
        if policy_digest != expected_policy_digest:
            raise ValueError("policy_digest does not match the policy body")
        for provider in self.providers:
            for cell in provider.platforms:
                capabilities = cell.capabilities
                if capabilities.policy_digest != policy_digest:
                    raise ValueError("capability policy digest does not match bundle")
                if (
                    capabilities.max_task_packet_bytes
                    != self.policy.max_task_packet_bytes
                ):
                    raise ValueError(
                        "capability task packet limit does not match policy"
                    )
                artifact = cell.qualification.artifact
                if artifact is not None:
                    if (
                        artifact.trellis_compatibility_digest
                        != trellis_compatibility_digest
                    ):
                        raise ValueError(
                            "qualification artifact Trellis compatibility digest "
                            "does not match bundle"
                        )
                    if artifact.policy_digest != policy_digest:
                        raise ValueError(
                            "qualification artifact policy digest does not match bundle"
                        )
        object.__setattr__(self, "bundle_digest", bundle_digest)
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "published", published)
        object.__setattr__(
            self,
            "trellis_compatibility_digest",
            trellis_compatibility_digest,
        )
        expected_bundle_digest = "sha256:" + canonical_sha256(self.body_primitive())
        if bundle_digest != expected_bundle_digest:
            raise ValueError(
                "bundle_digest does not match the backend qualification body"
            )

    def body_primitive(self) -> dict[str, object]:
        return {
            "policy": self.policy.to_primitive(),
            "policyDigest": self.policy_digest,
            "providers": [item.to_primitive() for item in self.providers],
            "published": self.published,
            "schemaVersion": self.schema_version,
            "trellisCompatibilityDigest": self.trellis_compatibility_digest,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.body_primitive()
        result["bundleDigest"] = self.bundle_digest
        return result

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def platform(self, provider: Provider, platform: Platform) -> PlatformCompatibility:
        if not isinstance(provider, Provider) or not isinstance(platform, Platform):
            raise TypeError("provider and platform must use compatibility enums")
        for provider_entry in self.providers:
            if provider_entry.provider is provider:
                for platform_entry in provider_entry.platforms:
                    if platform_entry.platform is platform:
                        return platform_entry
        raise LookupError("compatibility cell is missing")  # pragma: no cover


CompatibilityBundle = BackendQualificationBundle


__all__ = [
    "ADAPTER_QUALIFICATION_SCHEMA_VERSION",
    "BACKEND_CAPABILITY_SCHEMA_VERSION",
    "BACKEND_QUALIFICATION_SCHEMA_VERSION",
    "COMPATIBILITY_SCHEMA_VERSION",
    "QUALIFICATION_ARTIFACT_SCHEMA_VERSION",
    "SUPPORTED_TRELLIS_GRAPH_FORMAT",
    "SUPPORTED_TRELLIS_VERSION",
    "TRELLIS_COMPATIBILITY_SCHEMA_VERSION",
    "AdapterQualificationStatus",
    "BackendQualificationBundle",
    "CompatibilityBundle",
    "CompatibilityOperation",
    "CompatibilityPackage",
    "CompatibilityPolicy",
    "DisjointSiblingOverlapEvidence",
    "EvidenceScope",
    "IntegrationCapabilities",
    "LaunchProfile",
    "NpmProvenance",
    "OperationGuarantee",
    "Platform",
    "PlatformCompatibility",
    "Provider",
    "ProviderCompatibility",
    "Qualification",
    "QualificationArtifact",
    "QualificationScenario",
    "QualificationScenarioEvidence",
    "QualificationStatus",
    "SdkPin",
    "TrellisAdapterQualification",
    "TrellisCompatibility",
    "TrellisGraphAdapterQualification",
    "TrellisPackage",
    "TrellisPackageRole",
    "TrellisProjectionAdapterQualification",
]
