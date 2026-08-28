#!/usr/bin/env python3
"""Join coverage and mutation reports into direct safety-invariant evidence."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_mutation_gate import DEFAULT_MUTATIONS, MutationSpec
from scripts.ci_safety_registry import ACTIVE_M1_SAFETY_PATHS, SafetyPathRegistry

SCHEMA_VERSION = 2
CHANGED_LINES_SCHEMA_VERSION = 4
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_SOURCE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HUNK_RE = re.compile(
    r"^@@ -(?P<old_first>[0-9]+)(?:,(?P<old_count>[0-9]+))? "
    r"\+(?P<new_first>[0-9]+)(?:,(?P<new_count>[0-9]+))? @@"
)


class ChangedLinesInputError(RuntimeError):
    """A changed-source provenance failure that must close the gate."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BranchProjection:
    branch_id: str
    kind: str
    first_line: int
    last_line: int
    ast_path: str
    body_digest: str
    decision_digest: str
    owner_key: str

    @property
    def correspondence_key(self) -> tuple[str, str, str, str]:
        """Match only an unchanged branch moved within one lexical owner."""
        return (
            self.kind,
            self.owner_key,
            self.decision_digest,
            self.body_digest,
        )

    @property
    def structural_key(self) -> tuple[str, str, str]:
        """Identify the same control-flow slot across ordinary source edits."""
        return (self.kind, self.owner_key, self.ast_path)

    @property
    def allows_move_correspondence(self) -> bool:
        """Only branches with a meaningful body can be proven after a move."""
        return self.kind not in {
            "BoolOpSlot",
            "ComprehensionFilter",
            "MatchGuard",
        }

    def to_primitive(self) -> dict[str, object]:
        return {
            "ast_path": self.ast_path,
            "body_digest": self.body_digest,
            "branch_id": self.branch_id,
            "decision_digest": self.decision_digest,
            "first_line": self.first_line,
            "kind": self.kind,
            "last_line": self.last_line,
            "owner_key": self.owner_key,
        }


@dataclass(frozen=True, slots=True)
class ChangedHunk:
    old_first: int
    old_count: int
    new_first: int
    new_count: int
    old_branch_refs: tuple[str, ...] = ()
    new_branch_refs: tuple[str, ...] = ()

    @property
    def new_range(self) -> tuple[int, int] | None:
        if self.new_count == 0:
            return None
        return (self.new_first, self.new_first + self.new_count - 1)

    def to_primitive(self) -> dict[str, object]:
        return {
            "new_count": self.new_count,
            "new_first": self.new_first,
            "new_branch_refs": list(self.new_branch_refs),
            "old_count": self.old_count,
            "old_first": self.old_first,
            "old_branch_refs": list(self.old_branch_refs),
        }


@dataclass(frozen=True, slots=True)
class ChangedFile:
    status: str
    old_path: str | None
    new_path: str | None
    hunks: tuple[ChangedHunk, ...]
    old_blob_oid: str | None
    new_blob_oid: str | None
    old_source_sha256: str | None
    new_source_sha256: str | None
    old_source: str | None
    new_source: str | None
    old_control_flow: tuple[BranchProjection, ...]
    new_control_flow: tuple[BranchProjection, ...]

    @property
    def governed_path(self) -> str | None:
        return self.new_path if self.new_path is not None else self.old_path

    @property
    def added_line_ranges(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            line_range
            for hunk in self.hunks
            if (line_range := hunk.new_range) is not None
        )


def _canonical_path(value: object) -> str | None:
    if type(value) is not str or not value or "\x00" in value:
        return None
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    positions = [index for index, part in enumerate(parts) if part == "wish_builder"]
    if len(positions) != 1 or ".." in parts:
        return None
    return "/".join(parts[positions[0] :])


def _repository_path(value: object) -> str | None:
    if type(value) is not str or not value or "\x00" in value:
        return None
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _source_bytes(source: str) -> bytes:
    return source.encode("utf-8", errors="strict")


def _source_sha256(source: str) -> str:
    return "sha256:" + hashlib.sha256(_source_bytes(source)).hexdigest()


def _git_blob_oid(source: str, hexadecimal_length: int = 40) -> str:
    payload = _source_bytes(source)
    framed = f"blob {len(payload)}\0".encode("ascii") + payload
    if hexadecimal_length == 40:
        return hashlib.sha1(framed).hexdigest()
    if hexadecimal_length == 64:
        return hashlib.sha256(framed).hexdigest()
    raise ValueError("unsupported Git object id length")


def _projection_refs(
    projections: Sequence[BranchProjection],
    first: int,
    count: int,
) -> tuple[str, ...]:
    if count == 0:
        return ()
    last = first + count - 1
    return tuple(
        projection.branch_id
        for projection in projections
        if not (projection.last_line < first or projection.first_line > last)
    )


def _hunk_side_start(first: int, count: int) -> int:
    """Convert a unified-diff coordinate to a zero-based source index."""
    return first if count == 0 else first - 1


def _hunk_side_is_bounded(first: int, count: int, line_count: int) -> bool:
    if count == 0:
        return 0 <= first <= line_count
    return first >= 1 and first + count - 1 <= line_count


def _hunks_describe_snapshot_diff(
    old_source: str | None,
    new_source: str | None,
    hunks: Sequence[ChangedHunk],
) -> bool:
    """Prove that hunks cover every and only changed snapshot line region."""
    old_lines = [] if old_source is None else old_source.splitlines(keepends=True)
    new_lines = [] if new_source is None else new_source.splitlines(keepends=True)
    old_cursor = 0
    new_cursor = 0
    for hunk in hunks:
        old_start = _hunk_side_start(hunk.old_first, hunk.old_count)
        new_start = _hunk_side_start(hunk.new_first, hunk.new_count)
        if old_lines[old_cursor:old_start] != new_lines[new_cursor:new_start]:
            return False
        old_end = old_start + hunk.old_count
        new_end = new_start + hunk.new_count
        if old_lines[old_start:old_end] == new_lines[new_start:new_end]:
            return False
        old_cursor = old_end
        new_cursor = new_end
    return old_lines[old_cursor:] == new_lines[new_cursor:]


def _changed_error(
    errors: list[dict[str, object]],
    code: str,
    message: str,
    **details: object,
) -> None:
    errors.append({"code": code, "message": message, **details})


def _decode_changed_lines(
    value: object,
    path_registry: SafetyPathRegistry,
) -> tuple[dict[str, str | None], tuple[ChangedFile, ...], list[dict[str, object]]]:
    errors: list[dict[str, object]] = []
    provenance: dict[str, str | None] = {
        "base_ref": None,
        "head": None,
        "merge_base": None,
    }
    if type(value) is not dict:
        _changed_error(
            errors,
            "changed_lines_artifact_invalid",
            "changed-lines artifact must be an object",
        )
        return provenance, (), errors

    expected_keys = {"base_ref", "files", "head", "merge_base", "schema_version"}
    actual_keys = set(value)
    if actual_keys != expected_keys:
        _changed_error(
            errors,
            "changed_lines_artifact_fields_invalid",
            "changed-lines artifact fields must match the schema exactly",
        )

    base_ref = value.get("base_ref")
    if type(base_ref) is not str or not base_ref.strip() or "\x00" in base_ref:
        _changed_error(
            errors,
            "changed_base_ref_missing",
            "changed-lines artifact must identify the comparison base ref",
        )
    else:
        provenance["base_ref"] = base_ref

    for field in ("merge_base", "head"):
        commit = value.get(field)
        if type(commit) is not str or _COMMIT_RE.fullmatch(commit) is None:
            _changed_error(
                errors,
                f"changed_{field}_invalid",
                f"changed-lines artifact {field} must be a lowercase commit id",
            )
        else:
            provenance[field] = commit

    if value.get("schema_version") != CHANGED_LINES_SCHEMA_VERSION:
        _changed_error(
            errors,
            "changed_lines_schema_unsupported",
            "changed-lines artifact schema is unsupported",
        )

    raw_files = value.get("files")
    if type(raw_files) is not list:
        _changed_error(
            errors,
            "changed_files_invalid",
            "changed-lines files must be a list",
        )
        return provenance, (), errors

    changed_files: list[ChangedFile] = []
    touched_paths: set[str] = set()
    file_keys = {
        "hunks",
        "new_blob_oid",
        "new_control_flow",
        "new_path",
        "new_source",
        "new_source_sha256",
        "old_blob_oid",
        "old_control_flow",
        "old_path",
        "old_source",
        "old_source_sha256",
        "status",
    }
    for index, raw_file in enumerate(raw_files):
        if type(raw_file) is not dict or set(raw_file) != file_keys:
            _changed_error(
                errors,
                "changed_file_invalid",
                "changed file fields must match the schema exactly",
                file_index=index,
            )
            continue
        status = raw_file.get("status")
        if type(status) is not str or status not in {"A", "D", "M", "R"}:
            _changed_error(
                errors,
                "changed_file_status_invalid",
                "changed file status must be A, D, M, or R",
                file_index=index,
            )
            continue
        old_raw = raw_file.get("old_path")
        new_raw = raw_file.get("new_path")
        old_path = None if old_raw is None else _repository_path(old_raw)
        new_path = None if new_raw is None else _repository_path(new_raw)
        if (old_raw is not None and old_path is None) or (
            new_raw is not None and new_path is None
        ):
            _changed_error(
                errors,
                "changed_file_path_invalid",
                "changed file paths must be normalized repository-relative paths",
                file_index=index,
            )
            continue
        shape_valid = (
            (status == "A" and old_path is None and new_path is not None)
            or (
                status == "M"
                and old_path is not None
                and old_path == new_path
            )
            or (status == "D" and old_path is not None and new_path is None)
            or (
                status == "R"
                and old_path is not None
                and new_path is not None
                and old_path != new_path
            )
        )
        if not shape_valid:
            _changed_error(
                errors,
                "changed_file_shape_invalid",
                "changed file paths do not match its status",
                file_index=index,
            )
            continue
        candidate_paths = {old_path, new_path} - {None}
        if not any(path_registry.governs(path) for path in candidate_paths):
            _changed_error(
                errors,
                "changed_file_outside_governed_surface",
                "changed-lines artifact may contain only governed safety sources",
                file_index=index,
            )
            continue

        side_values: dict[
            str, tuple[str | None, str | None, str | None, tuple[BranchProjection, ...]]
        ] = {}
        snapshots_valid = True
        for side, required in (
            ("old", old_path is not None),
            ("new", new_path is not None),
        ):
            source = raw_file.get(f"{side}_source")
            source_sha256 = raw_file.get(f"{side}_source_sha256")
            blob_oid = raw_file.get(f"{side}_blob_oid")
            raw_control_flow = raw_file.get(f"{side}_control_flow")
            if not required:
                if (
                    source is not None
                    or source_sha256 is not None
                    or blob_oid is not None
                    or raw_control_flow != []
                ):
                    snapshots_valid = False
                    _changed_error(
                        errors,
                        "changed_file_snapshot_invalid",
                        "an absent file side must not contain a source snapshot",
                        file_index=index,
                        side=side,
                    )
                side_values[side] = (None, None, None, ())
                continue
            if (
                type(source) is not str
                or type(source_sha256) is not str
                or _SOURCE_DIGEST_RE.fullmatch(source_sha256) is None
                or type(blob_oid) is not str
                or _COMMIT_RE.fullmatch(blob_oid) is None
                or type(raw_control_flow) is not list
            ):
                snapshots_valid = False
                _changed_error(
                    errors,
                    "changed_file_snapshot_invalid",
                    "a present file side requires an exact blob and UTF-8 source snapshot",
                    file_index=index,
                    side=side,
                )
                side_values[side] = (None, None, None, ())
                continue
            try:
                expected_sha256 = _source_sha256(source)
                expected_blob_oid = _git_blob_oid(source, len(blob_oid))
                projections = _branch_projections(source)
            except (SyntaxError, UnicodeError, ValueError):
                snapshots_valid = False
                _changed_error(
                    errors,
                    "changed_file_snapshot_invalid",
                    "a source snapshot cannot be decoded into a control-flow inventory",
                    file_index=index,
                    side=side,
                )
                side_values[side] = (None, None, None, ())
                continue
            if source_sha256 != expected_sha256 or blob_oid != expected_blob_oid:
                snapshots_valid = False
                _changed_error(
                    errors,
                    "changed_file_snapshot_digest_mismatch",
                    "a source snapshot does not match its blob or SHA-256 identity",
                    file_index=index,
                    side=side,
                )
            expected_control_flow = [
                projection.to_primitive() for projection in projections
            ]
            if raw_control_flow != expected_control_flow:
                snapshots_valid = False
                _changed_error(
                    errors,
                    "changed_control_flow_invalid",
                    "a control-flow inventory is not the deterministic source projection",
                    file_index=index,
                    side=side,
                )
            side_values[side] = (
                source,
                source_sha256,
                blob_oid,
                projections,
            )
        if not snapshots_valid:
            continue

        old_source, old_source_sha256, old_blob_oid, old_control_flow = side_values[
            "old"
        ]
        new_source, new_source_sha256, new_blob_oid, new_control_flow = side_values[
            "new"
        ]

        raw_hunks = raw_file.get("hunks")
        hunks: list[ChangedHunk] = []
        if type(raw_hunks) is not list:
            _changed_error(
                errors,
                "changed_hunks_invalid",
                "changed hunks must be a list",
                file_index=index,
            )
            continue
        valid_hunks = True
        previous_old_last = 0
        previous_new_last = 0
        old_line_count = (
            0 if old_source is None else len(old_source.splitlines(keepends=True))
        )
        new_line_count = (
            0 if new_source is None else len(new_source.splitlines(keepends=True))
        )
        hunk_keys = {
            "new_branch_refs",
            "new_count",
            "new_first",
            "old_branch_refs",
            "old_count",
            "old_first",
        }
        for raw_hunk in raw_hunks:
            if (
                type(raw_hunk) is not dict
                or set(raw_hunk) != hunk_keys
                or any(
                    type(raw_hunk[key]) is not int
                    for key in ("new_count", "new_first", "old_count", "old_first")
                )
                or type(raw_hunk["old_branch_refs"]) is not list
                or type(raw_hunk["new_branch_refs"]) is not list
            ):
                valid_hunks = False
                break
            old_first = raw_hunk["old_first"]
            old_count = raw_hunk["old_count"]
            new_first = raw_hunk["new_first"]
            new_count = raw_hunk["new_count"]
            old_last = old_first + max(old_count, 1) - 1
            new_last = new_first + max(new_count, 1) - 1
            if (
                old_count < 0
                or new_count < 0
                or not _hunk_side_is_bounded(
                    old_first, old_count, old_line_count
                )
                or not _hunk_side_is_bounded(
                    new_first, new_count, new_line_count
                )
                or (old_count == 0 and new_count == 0)
                or (hunks and old_first <= previous_old_last)
                or (hunks and new_first <= previous_new_last)
            ):
                valid_hunks = False
                break
            old_branch_refs = _projection_refs(
                old_control_flow, old_first, old_count
            )
            new_branch_refs = _projection_refs(
                new_control_flow, new_first, new_count
            )
            if (
                raw_hunk["old_branch_refs"] != list(old_branch_refs)
                or raw_hunk["new_branch_refs"] != list(new_branch_refs)
            ):
                valid_hunks = False
                break
            hunks.append(
                ChangedHunk(
                    old_first,
                    old_count,
                    new_first,
                    new_count,
                    old_branch_refs,
                    new_branch_refs,
                )
            )
            previous_old_last = old_last
            previous_new_last = new_last
        if not valid_hunks:
            _changed_error(
                errors,
                "changed_hunks_invalid",
                "changed hunks must be ordered, disjoint, and structurally valid",
                file_index=index,
            )
            continue
        if (
            (status == "A" and any(hunk.old_count for hunk in hunks))
            or (status == "D" and any(hunk.new_count for hunk in hunks))
        ):
            _changed_error(
                errors,
                "changed_hunks_invalid",
                "changed hunk sides do not match the file status",
                file_index=index,
            )
            continue
        if status != "R" and (
            old_source == new_source
            or not _hunks_describe_snapshot_diff(old_source, new_source, hunks)
        ):
            _changed_error(
                errors,
                "changed_hunks_snapshot_mismatch",
                "changed hunks do not completely and exactly describe the source snapshots",
                file_index=index,
            )

        governed_touches = {
            path for path in candidate_paths if path_registry.governs(path)
        }
        if touched_paths & governed_touches:
            _changed_error(
                errors,
                "changed_file_duplicate",
                "a governed safety source appears in more than one changed file",
                file_index=index,
            )
            continue
        touched_paths.update(governed_touches)
        changed_files.append(
            ChangedFile(
                status,
                old_path,
                new_path,
                tuple(hunks),
                old_blob_oid,
                new_blob_oid,
                old_source_sha256,
                new_source_sha256,
                old_source,
                new_source,
                old_control_flow,
                new_control_flow,
            )
        )

    changed_files.sort(
        key=lambda item: (
            item.governed_path or "",
            item.status,
            item.old_path or "",
            item.new_path or "",
        )
    )
    return provenance, tuple(changed_files), errors


def _git_output(repository_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise ChangedLinesInputError(
            "git_unavailable", "git could not be started"
        ) from exc
    if completed.returncode != 0:
        raise ChangedLinesInputError(
            "git_command_failed", "git could not produce changed-source evidence"
        )
    return completed.stdout


def _decode_git_text(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ChangedLinesInputError(
            "git_output_invalid", "git output is not valid UTF-8"
        ) from exc


def _resolve_commit(repository_root: Path, ref: str, *, code: str) -> str:
    try:
        raw_output = _git_output(
            repository_root,
            ("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"),
        )
    except ChangedLinesInputError as exc:
        raise ChangedLinesInputError(code, "git ref is unavailable") from exc
    output = _decode_git_text(raw_output).strip()
    if _COMMIT_RE.fullmatch(output) is None:
        raise ChangedLinesInputError(code, "git ref did not resolve to one commit")
    return output


def _parse_name_status(value: bytes) -> tuple[tuple[str, str | None, str | None], ...]:
    fields = value.split(b"\0")
    if fields[-1:] == [b""]:
        fields.pop()
    result: list[tuple[str, str | None, str | None]] = []
    index = 0
    while index < len(fields):
        status_text = _decode_git_text(fields[index])
        index += 1
        status = status_text[:1]
        path_count = 2 if status in {"C", "R"} else 1
        if status not in {"A", "D", "M", "R"} or index + path_count > len(fields):
            raise ChangedLinesInputError(
                "git_name_status_invalid",
                "git reported an unsupported or malformed safety-source change",
            )
        paths = [_decode_git_text(item) for item in fields[index : index + path_count]]
        index += path_count
        if status == "A":
            result.append((status, None, paths[0]))
        elif status == "D":
            result.append((status, paths[0], None))
        elif status == "M":
            result.append((status, paths[0], paths[0]))
        else:
            result.append((status, paths[0], paths[1]))
    return tuple(result)


def _parse_unified_hunks(value: bytes) -> tuple[ChangedHunk, ...]:
    text = _decode_git_text(value)
    if text and not text.startswith("diff --git "):
        raise ChangedLinesInputError(
            "git_diff_invalid", "git produced malformed unified diff output"
        )
    if "GIT binary patch" in text or "Binary files " in text:
        raise ChangedLinesInputError(
            "git_diff_invalid", "governed safety source produced a binary diff"
        )
    hunks: list[ChangedHunk] = []
    for line in text.splitlines():
        if not line.startswith("@@"):
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            raise ChangedLinesInputError(
                "git_diff_invalid", "git produced a malformed unified diff hunk"
            )
        hunks.append(
            ChangedHunk(
                int(match.group("old_first")),
                int(match.group("old_count") or "1"),
                int(match.group("new_first")),
                int(match.group("new_count") or "1"),
            )
        )
    for previous, current in zip(hunks, hunks[1:]):
        previous_old_last = previous.old_first + max(previous.old_count, 1) - 1
        previous_new_last = previous.new_first + max(previous.new_count, 1) - 1
        if (
            current.old_first <= previous_old_last
            or current.new_first <= previous_new_last
        ):
            raise ChangedLinesInputError(
                "git_diff_invalid", "git produced overlapping or unordered diff hunks"
            )
    return tuple(hunks)


def _parse_unified_added_ranges(value: bytes) -> tuple[tuple[int, int], ...]:
    """Compatibility helper returning the new-side ranges from strict hunks."""
    return tuple(
        line_range
        for hunk in _parse_unified_hunks(value)
        if (line_range := hunk.new_range) is not None
    )


def _git_blob_source(
    repository_root: Path,
    revision: str,
    path: str,
) -> tuple[str, str]:
    tree_output = _git_output(
        repository_root,
        (
            "ls-tree",
            "-z",
            "--full-tree",
            revision,
            "--",
            f":(literal){path}",
        ),
    )
    records = [record for record in tree_output.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise ChangedLinesInputError(
            "git_blob_unavailable", "Git could not identify one exact safety-source blob"
        )
    metadata, raw_path = records[0].split(b"\t", 1)
    parts = metadata.split(b" ")
    if len(parts) != 3 or parts[0] not in {b"100644", b"100755"} or parts[1] != b"blob":
        raise ChangedLinesInputError(
            "git_blob_invalid", "a safety source is not a regular Git blob"
        )
    decoded_path = _decode_git_text(raw_path)
    blob_oid = _decode_git_text(parts[2])
    if decoded_path != path or _COMMIT_RE.fullmatch(blob_oid) is None:
        raise ChangedLinesInputError(
            "git_blob_invalid", "Git returned a mismatched safety-source blob"
        )
    raw_source = _git_output(repository_root, ("cat-file", "blob", blob_oid))
    source = _decode_git_text(raw_source)
    if _git_blob_oid(source, len(blob_oid)) != blob_oid:
        raise ChangedLinesInputError(
            "git_blob_invalid", "Git blob content does not match its object id"
        )
    try:
        _branch_projections(source)
    except (SyntaxError, ValueError) as exc:
        raise ChangedLinesInputError(
            "git_source_invalid", "a safety-source Git blob is not valid Python"
        ) from exc
    return blob_oid, source


def collect_changed_lines(
    repository_root: Path,
    base_ref: str,
    *,
    governed_paths: Sequence[str],
) -> dict[str, object]:
    """Create canonical changed-line evidence from merge-base...HEAD."""
    if type(base_ref) is not str or not base_ref.strip() or "\x00" in base_ref:
        raise ChangedLinesInputError(
            "changed_base_ref_missing", "comparison base ref is required"
        )
    root = repository_root.resolve(strict=True)
    base_commit = _resolve_commit(root, base_ref, code="base_ref_unavailable")
    head = _resolve_commit(root, "HEAD", code="head_ref_unavailable")
    merge_bases = tuple(
        _decode_git_text(
            _git_output(root, ("merge-base", "--all", base_commit, head))
        ).splitlines()
    )
    if len(merge_bases) != 1 or _COMMIT_RE.fullmatch(merge_bases[0]) is None:
        raise ChangedLinesInputError(
            "merge_base_unavailable",
            "comparison ref and HEAD must have exactly one merge base",
        )
    merge_base = merge_bases[0]
    paths = tuple(sorted(dict.fromkeys(governed_paths)))
    if not paths:
        raise ChangedLinesInputError(
            "governed_paths_missing", "no governed safety sources are registered"
        )
    comparison = f"{merge_base}...{head}"
    status_output = _git_output(
        root,
        ("diff", "--name-status", "-z", "--find-renames", comparison, "--", *paths),
    )
    files: list[dict[str, object]] = []
    for status, old_raw, new_raw in _parse_name_status(status_output):
        old_path = None if old_raw is None else _repository_path(old_raw)
        new_path = None if new_raw is None else _repository_path(new_raw)
        if (old_raw is not None and old_path is None) or (
            new_raw is not None and new_path is None
        ):
            raise ChangedLinesInputError(
                "git_path_invalid", "git reported an invalid repository path"
            )
        old_blob_oid: str | None = None
        old_source: str | None = None
        old_control_flow: tuple[BranchProjection, ...] = ()
        if old_path is not None:
            old_blob_oid, old_source = _git_blob_source(root, merge_base, old_path)
            old_control_flow = _branch_projections(old_source)
        new_blob_oid: str | None = None
        new_source: str | None = None
        new_control_flow: tuple[BranchProjection, ...] = ()
        if new_path is not None:
            new_blob_oid, new_source = _git_blob_source(root, head, new_path)
            new_control_flow = _branch_projections(new_source)
        hunks: tuple[ChangedHunk, ...] = ()
        if status in {"A", "D", "M"}:
            diff_path = new_path if new_path is not None else old_path
            assert diff_path is not None
            diff_output = _git_output(
                root,
                (
                    "diff",
                    "--unified=0",
                    "--no-color",
                    "--no-ext-diff",
                    "--no-renames",
                    comparison,
                    "--",
                    f":(literal){diff_path}",
                ),
            )
            parsed_hunks = _parse_unified_hunks(diff_output)
            hunks = tuple(
                ChangedHunk(
                    hunk.old_first,
                    hunk.old_count,
                    hunk.new_first,
                    hunk.new_count,
                    _projection_refs(
                        old_control_flow, hunk.old_first, hunk.old_count
                    ),
                    _projection_refs(
                        new_control_flow, hunk.new_first, hunk.new_count
                    ),
                )
                for hunk in parsed_hunks
            )
        files.append(
            {
                "hunks": [hunk.to_primitive() for hunk in hunks],
                "new_blob_oid": new_blob_oid,
                "new_control_flow": [
                    projection.to_primitive() for projection in new_control_flow
                ],
                "new_path": new_path,
                "new_source": new_source,
                "new_source_sha256": (
                    None if new_source is None else _source_sha256(new_source)
                ),
                "old_blob_oid": old_blob_oid,
                "old_control_flow": [
                    projection.to_primitive() for projection in old_control_flow
                ],
                "old_path": old_path,
                "old_source": old_source,
                "old_source_sha256": (
                    None if old_source is None else _source_sha256(old_source)
                ),
                "status": status,
            }
        )
    files.sort(
        key=lambda item: (
            str(item["new_path"] or item["old_path"]),
            str(item["status"]),
        )
    )
    return {
        "base_ref": base_ref,
        "files": files,
        "head": head,
        "merge_base": merge_base,
        "schema_version": CHANGED_LINES_SCHEMA_VERSION,
    }


def _integer_set(value: object) -> set[int] | None:
    if type(value) is not list or any(type(item) is not int or item < 1 for item in value):
        return None
    return set(value)


def _branches(value: object) -> tuple[tuple[int, int], ...] | None:
    if type(value) is not list:
        return None
    result: list[tuple[int, int]] = []
    for item in value:
        if (
            type(item) is not list
            or len(item) != 2
            or any(type(part) is not int for part in item)
            or item[0] < 1
        ):
            return None
        result.append((item[0], item[1]))
    return tuple(result)


_COVERAGE_PRAGMA_RE = re.compile(
    r"#\s*pragma:\s*no\s+(?:branch|cover)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CoverageDetails:
    executed_lines: set[int]
    missing_lines: set[int]
    excluded_lines: set[int]
    executed_branches: tuple[tuple[int, int], ...]
    missing_branches: tuple[tuple[int, int], ...]


def _coverage_details(value: object) -> CoverageDetails | None:
    if not isinstance(value, Mapping):
        return None
    executed_lines = _integer_set(value.get("executed_lines"))
    missing_lines = _integer_set(value.get("missing_lines"))
    excluded_lines = _integer_set(value.get("excluded_lines"))
    executed_branches = _branches(value.get("executed_branches"))
    missing_branches = _branches(value.get("missing_branches"))
    summary = value.get("summary")
    if (
        None
        in (
            executed_lines,
            missing_lines,
            excluded_lines,
            executed_branches,
            missing_branches,
        )
        or not isinstance(summary, Mapping)
    ):
        return None
    assert executed_lines is not None and missing_lines is not None
    assert excluded_lines is not None
    assert executed_branches is not None and missing_branches is not None
    executed_set = set(executed_branches)
    missing_set = set(missing_branches)
    if (
        len(executed_set) != len(executed_branches)
        or len(missing_set) != len(missing_branches)
        or executed_set & missing_set
        or type(summary.get("num_branches")) is not int
        or type(summary.get("covered_branches")) is not int
        or type(summary.get("missing_branches")) is not int
        or summary["num_branches"] != len(executed_set | missing_set)
        or summary["covered_branches"] != len(executed_set)
        or summary["missing_branches"] != len(missing_set)
    ):
        return None
    return CoverageDetails(
        executed_lines,
        missing_lines,
        excluded_lines,
        executed_branches,
        missing_branches,
    )


def _source_span(value: ast.AST | None) -> tuple[int, int] | None:
    if value is None:
        return None
    first = getattr(value, "lineno", None)
    last = getattr(value, "end_lineno", first)
    if type(first) is not int or type(last) is not int:
        return None
    return (first, last)


class _DecisionNormalizer(ast.NodeTransformer):
    """Remove decision expressions while retaining control-flow and body structure."""

    @staticmethod
    def _sentinel() -> ast.Constant:
        return ast.Constant(value="<decision>")

    def visit_If(self, node: ast.If) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.If)
        node.test = self._sentinel()
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.While)
        node.test = self._sentinel()
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.For)
        node.iter = self._sentinel()
        return node

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.AsyncFor)
        node.iter = self._sentinel()
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.IfExp)
        node.test = self._sentinel()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.BoolOp)
        node.op = ast.And()
        node.values = [self._sentinel()]
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.ExceptHandler)
        node.type = None
        return node

    def visit_Match(self, node: ast.Match) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.Match)
        node.subject = self._sentinel()
        return node

    def visit_match_case(self, node: ast.match_case) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.match_case)
        node.pattern = ast.MatchAs()
        node.guard = None
        return node

    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        node = self.generic_visit(node)
        assert isinstance(node, ast.comprehension)
        node.iter = self._sentinel()
        node.ifs = [self._sentinel() for _ in node.ifs]
        return node


def _normalized_ast_dump(value: ast.AST) -> str:
    normalized = _DecisionNormalizer().visit(copy.deepcopy(value))
    assert isinstance(normalized, ast.AST)
    return ast.dump(normalized, include_attributes=False)


def _ast_parts_digest(*groups: tuple[str, Sequence[ast.AST]]) -> str:
    payload = [
        [label, [_normalized_ast_dump(item) for item in items]]
        for label, items in groups
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _exact_ast_digest(node: ast.AST) -> str:
    encoded = ast.dump(node, include_attributes=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _branch_body_digest(node: ast.AST) -> str:
    if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor)):
        return _ast_parts_digest(
            ("body", node.body),
            ("orelse", node.orelse),
        )
    if isinstance(node, ast.IfExp):
        return _ast_parts_digest(("body", (node.body,)), ("orelse", (node.orelse,)))
    if isinstance(node, ast.ExceptHandler):
        return _ast_parts_digest(("body", node.body))
    if isinstance(node, (ast.Try, ast.TryStar)):
        return _ast_parts_digest(
            ("body", node.body),
            ("handlers", node.handlers),
            ("orelse", node.orelse),
            ("finalbody", node.finalbody),
        )
    if isinstance(node, ast.Match):
        return _ast_parts_digest(
            (
                "case_bodies",
                tuple(statement for case in node.cases for statement in case.body),
            )
        )
    if isinstance(node, ast.match_case):
        return _ast_parts_digest(("body", node.body))
    if isinstance(node, ast.comprehension):
        return _ast_parts_digest(("target", (node.target,)))
    return _ast_parts_digest(("decision", ()))


def _branch_projections(source: str) -> tuple[BranchProjection, ...]:
    """Project every control-flow header while preserving duplicate spans."""
    tree = ast.parse(source)
    source_lines = source.splitlines()
    raw: list[tuple[str, int, int, str, str, str, str]] = []

    def add(
        node: ast.AST,
        ast_path: str,
        first: int | None,
        tail: ast.AST | None = None,
        *,
        last: int | None = None,
        kind: str | None = None,
        owner_key: str,
    ) -> None:
        if type(first) is not int:
            return
        tail_span = _source_span(tail)
        header_last = (
            last
            if type(last) is int
            else first if tail_span is None else max(first, tail_span[1])
        )
        raw.append(
            (
                kind or type(node).__name__,
                first,
                header_last,
                ast_path,
                _branch_body_digest(node),
                _exact_ast_digest(node),
                owner_key,
            )
        )

    def walk(node: ast.AST, ast_path: str, owner_key: str) -> None:
        current_owner = owner_key
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current_owner = (
                f"{owner_key}/function:{node.name}:"
                f"{_exact_ast_digest(node.args)}"
            )
        elif isinstance(node, ast.ClassDef):
            current_owner = (
                f"{owner_key}/class:{node.name}:"
                f"{_ast_parts_digest(('bases', node.bases))}"
            )
        elif isinstance(node, ast.Lambda):
            current_owner = f"{owner_key}/lambda:{ast_path}"
        if isinstance(node, (ast.If, ast.While)):
            add(node, ast_path, node.lineno, node.test, owner_key=current_owner)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add(node, ast_path, node.lineno, node.iter, owner_key=current_owner)
        elif isinstance(node, ast.BoolOp):
            span = _source_span(node)
            if span is not None:
                for index in range(max(0, len(node.values) - 1)):
                    add(
                        node,
                        f"{ast_path}.short_circuit[{index}]",
                        span[0],
                        last=span[1],
                        kind="BoolOpSlot",
                        owner_key=current_owner,
                    )
        elif isinstance(node, ast.IfExp):
            span = _source_span(node)
            if span is not None:
                add(node, ast_path, span[0], last=span[1], owner_key=current_owner)
        elif isinstance(node, ast.ExceptHandler):
            add(node, ast_path, node.lineno, node.type, owner_key=current_owner)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            add(node, ast_path, node.lineno, owner_key=current_owner)
        elif isinstance(node, ast.Match):
            add(node, ast_path, node.lineno, node.subject, owner_key=current_owner)
        elif isinstance(node, ast.match_case):
            pattern_span = _source_span(node.pattern)
            if pattern_span is not None:
                case_first = _match_case_first_line(node, source_lines)
                tail = node.guard if node.guard is not None else node.pattern
                add(
                    node,
                    ast_path,
                    case_first,
                    tail,
                    kind="MatchCase",
                    owner_key=current_owner,
                )
                if node.guard is not None:
                    guard_span = _source_span(node.guard)
                    if guard_span is not None:
                        add(
                            node,
                            f"{ast_path}.guard_decision",
                            guard_span[0],
                            last=guard_span[1],
                            kind="MatchGuard",
                            owner_key=current_owner,
                        )
        elif isinstance(node, ast.comprehension):
            parts = (node.target, node.iter)
            part_spans = [span for part in parts if (span := _source_span(part))]
            if part_spans:
                add(
                    node,
                    f"{ast_path}.generator_decision",
                    min(span[0] for span in part_spans),
                    last=max(span[1] for span in part_spans),
                    kind="ComprehensionGenerator",
                    owner_key=current_owner,
                )
            for index, condition in enumerate(node.ifs):
                condition_span = _source_span(condition)
                if condition_span is not None:
                    add(
                        node,
                        f"{ast_path}.filter_decision[{index}]",
                        condition_span[0],
                        last=condition_span[1],
                        kind="ComprehensionFilter",
                        owner_key=current_owner,
                    )

        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                walk(value, f"{ast_path}.{field}", current_owner)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        walk(item, f"{ast_path}.{field}[{index}]", current_owner)

    walk(tree, "module", "module")
    raw.sort(
        key=lambda item: (
            item[1],
            item[2],
            item[3],
            item[0],
            item[4],
            item[5],
            item[6],
        )
    )
    return tuple(
        BranchProjection(
            f"branch-{index:06d}",
            kind,
            first,
            last,
            ast_path,
            body_digest,
            decision_digest,
            owner_key,
        )
        for index, (
            kind,
            first,
            last,
            ast_path,
            body_digest,
            decision_digest,
            owner_key,
        ) in enumerate(raw)
    )


def _branch_source_spans(source: str) -> tuple[tuple[int, int], ...]:
    """Return unique header spans for coverage-origin mapping."""
    return tuple(
        sorted(
            {
                (projection.first_line, projection.last_line)
                for projection in _branch_projections(source)
            }
        )
    )


def _match_case_first_line(node: ast.match_case, source_lines: Sequence[str]) -> int:
    pattern_span = _source_span(node.pattern)
    if pattern_span is None:
        raise ValueError("match case has no source span")
    for number in range(pattern_span[0], 0, -1):
        if re.match(r"^\s*case(?:\s|\()", source_lines[number - 1]):
            return number
    return pattern_span[0]


def _source_branch_arc_universe(source: str) -> dict[int, frozenset[int]]:
    """Derive exact line exits for source-level statement branches."""
    tree = ast.parse(source)
    source_lines = source.splitlines()
    raw: dict[int, set[int]] = defaultdict(set)

    def first_line(statements: Sequence[ast.stmt], fallback: int) -> int:
        return statements[0].lineno if statements else fallback

    def walk_block(statements: Sequence[ast.stmt], continuation: int) -> None:
        for index, statement in enumerate(statements):
            next_line = (
                statements[index + 1].lineno
                if index + 1 < len(statements)
                else continuation
            )
            if isinstance(statement, ast.If):
                raw[statement.lineno].update(
                    {
                        first_line(statement.body, next_line),
                        first_line(statement.orelse, next_line),
                    }
                )
                walk_block(statement.body, next_line)
                walk_block(statement.orelse, next_line)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                raw[statement.lineno].update(
                    {
                        first_line(statement.body, statement.lineno),
                        first_line(statement.orelse, next_line),
                    }
                )
                walk_block(statement.body, statement.lineno)
                walk_block(statement.orelse, next_line)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_block(statement.body, -statement.lineno)
            elif isinstance(statement, ast.ClassDef):
                walk_block(statement.body, -statement.lineno)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                walk_block(statement.body, next_line)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                walk_block(statement.body, next_line)
                for handler in statement.handlers:
                    walk_block(handler.body, next_line)
                walk_block(statement.orelse, next_line)
                walk_block(statement.finalbody, next_line)
            elif isinstance(statement, ast.Match):
                case_lines = [
                    _match_case_first_line(case, source_lines)
                    for case in statement.cases
                ]
                for case_index, case in enumerate(statement.cases):
                    false_line = (
                        case_lines[case_index + 1]
                        if case_index + 1 < len(case_lines)
                        else next_line
                    )
                    is_irrefutable = (
                        isinstance(case.pattern, ast.MatchAs)
                        and case.pattern.pattern is None
                        and case.guard is None
                    )
                    if not is_irrefutable:
                        raw[case_lines[case_index]].update(
                            {first_line(case.body, next_line), false_line}
                        )
                    walk_block(case.body, next_line)

    walk_block(tree.body, -1)
    return {origin: frozenset(destinations) for origin, destinations in raw.items()}


def _branch_origin_spans(
    branch_spans: Sequence[tuple[int, int]],
    origins: set[int],
) -> dict[int, tuple[int, int]]:
    """Map coverage origins to source control-flow headers starting at that line."""
    result: dict[int, tuple[int, int]] = {}
    for origin in origins:
        candidates = [span for span in branch_spans if span[0] == origin]
        if not candidates:
            continue
        first = min(span[0] for span in candidates)
        last = max(span[1] for span in candidates)
        result[origin] = (first, last)
    return result


def _error(
    code: str,
    message: str,
    spec: MutationSpec | None = None,
    **details: object,
) -> dict[str, object]:
    result: dict[str, object] = {"code": code, "message": message, **details}
    if spec is not None:
        result["mutation_id"] = spec.mutation_id
        result["path"] = spec.source_path
    return result


def evaluate_safety_evidence(
    coverage_report: object,
    mutation_report: object,
    changed_lines_report: object = None,
    *,
    trusted_changed_lines_report: object = None,
    source_root: Path = REPOSITORY_ROOT,
    specs: Sequence[MutationSpec] = DEFAULT_MUTATIONS,
    path_registry: SafetyPathRegistry = ACTIVE_M1_SAFETY_PATHS,
) -> dict[str, Any]:
    """Require direct evidence for fixed invariants and every changed branch."""
    mutation_specs = tuple(specs)
    provenance, changed_files, changed_errors = _decode_changed_lines(
        changed_lines_report, path_registry
    )
    errors: list[dict[str, object]] = list(changed_errors)
    if trusted_changed_lines_report is None:
        errors.append(
            _error(
                "changed_lines_provenance_unverified",
                "changed-lines evidence was not matched to an independently trusted Git collection",
            )
        )
    if (
        trusted_changed_lines_report is not None
        and changed_lines_report != trusted_changed_lines_report
    ):
        errors.append(
            _error(
                "changed_lines_provenance_mismatch",
                "changed-lines evidence differs from the independently trusted Git collection",
            )
        )
    if not mutation_specs:
        errors.append(_error("mutation_registry_empty", "safety mutation registry is empty"))
    for spec in mutation_specs:
        if not path_registry.governs(spec.source_path):
            errors.append(
                _error(
                    "mutation_source_not_governed",
                    "safety mutation source is outside the safety-path registry",
                    spec,
                )
            )
    if not isinstance(coverage_report, Mapping):
        errors.append(_error("coverage_report_invalid", "coverage report must be an object"))
        raw_coverage_files: Mapping[object, object] = {}
    else:
        meta = coverage_report.get("meta")
        if not isinstance(meta, Mapping) or meta.get("branch_coverage") is not True:
            errors.append(
                _error(
                    "branch_coverage_required",
                    "coverage report must prove branch_coverage=true",
                )
            )
        candidate_files = coverage_report.get("files")
        if not isinstance(candidate_files, Mapping):
            errors.append(_error("coverage_files_invalid", "coverage files must be an object"))
            raw_coverage_files = {}
        else:
            raw_coverage_files = candidate_files

    coverage_files: dict[str, object] = {}
    for raw_path, payload in raw_coverage_files.items():
        path = _canonical_path(raw_path)
        if path is None:
            continue
        if path in coverage_files:
            errors.append(
                _error("coverage_path_duplicate", f"duplicate normalized coverage path: {path}")
            )
        coverage_files[path] = payload

    if not isinstance(mutation_report, Mapping):
        errors.append(_error("mutation_report_invalid", "mutation report must be an object"))
        raw_results: object = []
    else:
        baseline = mutation_report.get("baseline")
        policy = mutation_report.get("policy")
        if (
            mutation_report.get("status") != "passed"
            or not isinstance(baseline, Mapping)
            or baseline.get("successful") is not True
            or not isinstance(policy, Mapping)
            or policy.get("passed") is not True
        ):
            errors.append(
                _error(
                    "mutation_gate_not_passed",
                    "mutation baseline and policy must both pass",
                )
            )
        raw_results = mutation_report.get("results")

    mutation_results: dict[str, Mapping[str, object]] = {}
    if type(raw_results) is not list:
        errors.append(_error("mutation_results_invalid", "mutation results must be a list"))
    else:
        for result in raw_results:
            if not isinstance(result, Mapping) or type(result.get("mutation_id")) is not str:
                errors.append(_error("mutation_result_invalid", "mutation result is malformed"))
                continue
            mutation_id = result["mutation_id"]
            if mutation_id in mutation_results:
                errors.append(
                    _error(
                        "mutation_result_duplicate",
                        f"duplicate mutation result: {mutation_id}",
                    )
                )
            mutation_results[mutation_id] = result

    expected_ids = {spec.mutation_id for spec in mutation_specs}
    for mutation_id in sorted(set(mutation_results) - expected_ids):
        errors.append(
            _error("mutation_result_unexpected", f"unexpected mutation result: {mutation_id}")
        )

    invariants: list[dict[str, object]] = []
    eligible_anchors: dict[str, list[tuple[int, int, MutationSpec]]] = {}
    coverage_details: dict[str, CoverageDetails] = {}
    for spec in mutation_specs:
        source_path = source_root / Path(spec.source_path)
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError:
            errors.append(_error("source_read_failed", "cannot read safety source", spec))
            continue
        if source.count(spec.before) != 1:
            errors.append(
                _error(
                    "source_anchor_drift",
                    "registered safety source anchor must occur exactly once",
                    spec,
                )
            )
            continue
        offset = source.index(spec.before)
        anchor_end = offset + len(spec.before.rstrip("\r\n"))
        first_line = source.count("\n", 0, offset) + 1
        last_line = source.count("\n", 0, anchor_end) + 1

        payload = coverage_files.get(spec.source_path)
        if not isinstance(payload, Mapping):
            errors.append(
                _error(
                    "coverage_file_missing",
                    "safety source lacks coverage data",
                    spec,
                )
            )
            continue
        details = _coverage_details(payload)
        if details is None:
            errors.append(_error("coverage_detail_invalid", "coverage detail is malformed", spec))
            continue
        coverage_details[spec.source_path] = details
        anchor_range = set(range(first_line, last_line + 1))
        executable_anchor = anchor_range & (
            details.executed_lines | details.missing_lines
        )
        uncovered_anchor = executable_anchor & details.missing_lines
        uncovered_arcs = tuple(
            branch
            for branch in details.missing_branches
            if branch[0] in anchor_range
        )
        if uncovered_anchor:
            errors.append(
                _error(
                    "source_anchor_uncovered",
                    "safety anchor line was not executed",
                    spec,
                )
            )
        if uncovered_arcs:
            errors.append(
                _error(
                    "source_anchor_branch_uncovered",
                    "safety anchor branch was not fully executed",
                    spec,
                )
            )

        result = mutation_results.get(spec.mutation_id)
        if result is None:
            errors.append(_error("mutation_result_missing", "safety mutation has no result", spec))
            continue
        test_run = result.get("test_run")
        direct_failure = (
            isinstance(test_run, Mapping)
            and type(test_run.get("tests_run")) is int
            and test_run["tests_run"] > 0
            and test_run.get("successful") is False
            and type(test_run.get("failures")) is int
            and type(test_run.get("errors")) is int
            and test_run["failures"] + test_run["errors"] > 0
            and test_run.get("infrastructure_error") is None
        )
        direct_evidence_valid = not (
            result.get("status") != "killed"
            or result.get("safety_invariant") is not True
            or result.get("invariant") != spec.invariant
            or result.get("source_path") != spec.source_path
            or result.get("test_ids") != list(spec.test_ids)
            or not direct_failure
        )
        if not direct_evidence_valid:
            errors.append(
                _error(
                    "direct_mutation_evidence_invalid",
                    "registered direct tests did not kill the exact safety mutation",
                    spec,
                )
            )
        else:
            eligible_anchors.setdefault(spec.source_path, []).append(
                (first_line, last_line, spec)
            )

        invariants.append(
            {
                "anchor_first_line": first_line,
                "anchor_last_line": last_line,
                "coverage_evidence": (
                    "anchor_executed" if executable_anchor else "mutation_only"
                ),
                "covered_anchor_lines": sorted(executable_anchor - uncovered_anchor),
                "executed_anchor_branches": [
                    list(branch)
                    for branch in details.executed_branches
                    if branch[0] in anchor_range
                ],
                "mutation_id": spec.mutation_id,
                "path": spec.source_path,
                "test_ids": list(spec.test_ids),
            }
        )

    changed_files_output: list[dict[str, object]] = []
    for changed_file in changed_files:
        changed_path = changed_file.governed_path
        changed_files_output.append(
            {
                "added_line_ranges": [
                    list(item) for item in changed_file.added_line_ranges
                ],
                "hunks": [hunk.to_primitive() for hunk in changed_file.hunks],
                "new_path": changed_file.new_path,
                "old_path": changed_file.old_path,
                "status": changed_file.status,
            }
        )
        if changed_file.status == "D":
            errors.append(
                _error(
                    "governed_safety_source_deleted",
                    "a governed safety source was deleted",
                    path=changed_path,
                )
            )
        elif changed_file.status == "R":
            errors.append(
                _error(
                    "governed_safety_source_renamed",
                    "a governed safety source was renamed",
                    path=changed_path,
                )
            )
        elif changed_file.status in {"A", "M"} and not changed_file.hunks:
            errors.append(
                _error(
                    "changed_safety_change_unmapped",
                    "a changed safety source has no reviewable textual hunk",
                    path=changed_path,
                )
            )
        if changed_file.status == "M":
            available_new = {
                projection.branch_id: projection
                for projection in changed_file.new_control_flow
            }
            branch_mapping: dict[str, str] = {}
            unmatched_projections: list[BranchProjection] = []
            for projection in changed_file.old_control_flow:
                candidates = [
                    candidate
                    for candidate in available_new.values()
                    if candidate.structural_key == projection.structural_key
                ]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    branch_mapping[projection.branch_id] = candidate.branch_id
                    available_new.pop(candidate.branch_id)
                else:
                    unmatched_projections.append(projection)

            new_by_key: dict[
                tuple[str, str, str, str], list[BranchProjection]
            ] = defaultdict(list)
            for projection in available_new.values():
                if projection.allows_move_correspondence:
                    new_by_key[projection.correspondence_key].append(projection)

            unmatched_old: set[str] = set()
            for projection in unmatched_projections:
                candidates = (
                    new_by_key[projection.correspondence_key]
                    if projection.allows_move_correspondence
                    else []
                )
                if len(candidates) == 1:
                    candidate = candidates.pop()
                    branch_mapping[projection.branch_id] = candidate.branch_id
                    available_new.pop(candidate.branch_id)
                else:
                    unmatched_old.add(projection.branch_id)
                    errors.append(
                        _error(
                            "changed_safety_deletion_unproven",
                            "an old safety branch has no trustworthy current correspondence",
                            branch_id=projection.branch_id,
                            kind=projection.kind,
                            line=projection.first_line,
                            path=changed_path,
                        )
                    )
            changed_new_refs = {
                branch_ref
                for hunk in changed_file.hunks
                for branch_ref in hunk.new_branch_refs
            }
            for hunk in changed_file.hunks:
                moved_correspondences = {
                    branch_mapping[branch_ref]
                    for branch_ref in hunk.old_branch_refs
                    if branch_ref in branch_mapping
                }
                deletion_is_unproven = (
                    hunk.old_count > hunk.new_count
                    and (
                        not hunk.old_branch_refs
                        or any(
                            branch_ref in unmatched_old
                            for branch_ref in hunk.old_branch_refs
                        )
                        or not moved_correspondences <= changed_new_refs
                    )
                )
                if deletion_is_unproven and not any(
                    branch_ref in unmatched_old for branch_ref in hunk.old_branch_refs
                ):
                    errors.append(
                        _error(
                            "changed_safety_deletion_unproven",
                            "a safety-source replacement removed lines without deletion evidence",
                            line=hunk.old_first,
                            path=changed_path,
                        )
                    )

    changed_branches: list[dict[str, object]] = []
    changed_coverage_checked: set[str] = set()
    for changed_file in changed_files:
        if changed_file.status not in {"A", "M"}:
            continue
        path = changed_file.new_path
        assert path is not None
        if not path_registry.governs(path) or not changed_file.hunks:
            continue
        details = coverage_details.get(path)
        if details is None:
            payload = coverage_files.get(path)
            if not isinstance(payload, Mapping):
                if path not in changed_coverage_checked:
                    errors.append(
                        _error(
                            "changed_branch_coverage_missing",
                            "changed safety source lacks branch coverage details",
                            path=path,
                        )
                    )
                    changed_coverage_checked.add(path)
                continue
            details = _coverage_details(payload)
            if details is None:
                if path not in changed_coverage_checked:
                    errors.append(
                        _error(
                            "changed_branch_coverage_invalid",
                            "changed safety source branch coverage is malformed",
                            path=path,
                        )
                    )
                    changed_coverage_checked.add(path)
                continue
            coverage_details[path] = details

        executed_set = set(details.executed_branches)
        missing_set = set(details.missing_branches)
        origins = {branch[0] for branch in executed_set | missing_set}
        try:
            source = (source_root / Path(path)).read_text(encoding="utf-8")
            source_tree = ast.parse(source)
            current_control_flow = _branch_projections(source)
            branch_arc_universe = _source_branch_arc_universe(source)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            errors.append(
                _error(
                    "changed_safety_source_unreadable",
                    "the changed safety source cannot be mapped to branch evidence",
                    path=path,
                )
            )
            continue
        if (
            source != changed_file.new_source
            or _source_sha256(source) != changed_file.new_source_sha256
            or changed_file.new_blob_oid is None
            or _git_blob_oid(source, len(changed_file.new_blob_oid))
            != changed_file.new_blob_oid
            or current_control_flow != changed_file.new_control_flow
            or any(
                _projection_refs(
                    current_control_flow, hunk.new_first, hunk.new_count
                )
                != hunk.new_branch_refs
                for hunk in changed_file.hunks
            )
        ):
            errors.append(
                _error(
                    "changed_safety_source_mismatch",
                    "the current safety source differs from its exact Git snapshot",
                    path=path,
                )
            )
            continue
        branch_spans = tuple(
            (projection.first_line, projection.last_line)
            for projection in current_control_flow
        )
        origin_spans = _branch_origin_spans(branch_spans, origins)
        changed_ranges = changed_file.added_line_ranges
        changed_lines = {
            line
            for first, last in changed_ranges
            for line in range(first, last + 1)
        }
        suppressed_lines = {
            number
            for number, line in enumerate(source.splitlines(), start=1)
            if number in changed_lines and _COVERAGE_PRAGMA_RE.search(line)
        }
        excluded_changed_lines = changed_lines & details.excluded_lines
        if suppressed_lines or excluded_changed_lines:
            errors.append(
                _error(
                    "changed_branch_coverage_suppressed",
                    "changed safety code may not suppress coverage evidence",
                    lines=sorted(suppressed_lines | excluded_changed_lines),
                    path=path,
                )
            )
        changed_projections = tuple(
            projection
            for projection in current_control_flow
            if any(
                not (
                    projection.last_line < first
                    or projection.first_line > last
                )
                for first, last in changed_ranges
            )
        )
        mapped_origins = {
            origin
            for origin, (span_first, span_last) in origin_spans.items()
            if any(
                first <= origin <= last
                or not (span_last < first or span_first > last)
                for first, last in changed_ranges
            )
        }
        unknown_changed_origins = {
            origin
            for origin in origins
            if origin in changed_lines and origin not in origin_spans
        }
        if unknown_changed_origins:
            errors.append(
                _error(
                    "changed_branch_coverage_invalid",
                    "changed coverage contains an origin absent from the source control-flow inventory",
                    lines=sorted(unknown_changed_origins),
                    path=path,
                )
            )

        source_line_count = len(source.splitlines(keepends=True))
        valid_destination_lines = {
            node.lineno
            for node in ast.walk(source_tree)
            if isinstance(node, ast.stmt) and type(getattr(node, "lineno", None)) is int
        } | {projection.first_line for projection in current_control_flow}
        known_source_lines = (
            details.executed_lines
            | details.missing_lines
            | details.excluded_lines
        )
        relevant_origins = mapped_origins | unknown_changed_origins
        invalid_arcs = sorted(
            branch
            for branch in executed_set | missing_set
            if branch[0] in relevant_origins
            and (
                branch[1] == 0
                or abs(branch[1]) > source_line_count
                or (
                    branch[1] > 0
                    and branch[1] not in valid_destination_lines
                )
                or (branch[1] > 0 and branch[1] not in known_source_lines)
                or (
                    branch in executed_set
                    and branch[0] not in details.executed_lines
                )
            )
        )
        if invalid_arcs:
            errors.append(
                _error(
                    "changed_branch_coverage_invalid",
                    "changed coverage contains an arc outside the source line universe",
                    arcs=[list(branch) for branch in invalid_arcs],
                    path=path,
                )
            )
        denominator_mismatches: list[dict[str, object]] = []
        for origin in sorted(mapped_origins):
            actual_destinations = {
                branch[1]
                for branch in executed_set | missing_set
                if branch[0] == origin
            }
            expected_destinations = branch_arc_universe.get(origin)
            if (
                expected_destinations is None
                or actual_destinations != expected_destinations
            ):
                denominator_mismatches.append(
                    {
                        "actual": sorted(actual_destinations),
                        "expected": (
                            None
                            if expected_destinations is None
                            else sorted(expected_destinations)
                        ),
                        "origin": origin,
                    }
                )
        if denominator_mismatches:
            errors.append(
                _error(
                    "changed_branch_coverage_invalid",
                    "changed coverage arc denominator differs from source control flow",
                    mismatches=denominator_mismatches,
                    path=path,
                )
            )
        incomplete_origins = {
            origin
            for origin in mapped_origins
            if len(
                {
                    branch
                    for branch in executed_set | missing_set
                    if branch[0] == origin
                }
            )
            < 2
        }
        if incomplete_origins:
            errors.append(
                _error(
                    "changed_branch_coverage_invalid",
                    "a changed branch origin has an incomplete arc denominator",
                    lines=sorted(incomplete_origins),
                    path=path,
                )
            )
        for projection in changed_projections:
            if not any(
                projection.first_line <= origin <= projection.last_line
                for origin in mapped_origins
            ):
                errors.append(
                    _error(
                        "changed_branch_coverage_missing",
                        "a changed safety branch is absent from coverage branch evidence",
                        branch_id=projection.branch_id,
                        kind=projection.kind,
                        line=projection.first_line,
                        path=path,
                    )
                )

        projections_by_span: dict[
            tuple[int, int], list[BranchProjection]
        ] = defaultdict(list)
        for projection in changed_projections:
            projections_by_span[
                (projection.first_line, projection.last_line)
            ].append(projection)
        for span, projections in sorted(projections_by_span.items()):
            proof_origins = {
                origin
                for origin in mapped_origins
                if span[0] <= origin <= span[1]
            }
            if len(proof_origins) < len(projections):
                errors.append(
                    _error(
                        "changed_branch_coverage_ambiguous",
                        "multiple changed decisions share coverage evidence that cannot prove each decision independently",
                        branch_ids=sorted(
                            projection.branch_id for projection in projections
                        ),
                        line=span[0],
                        path=path,
                    )
                )
        for hunk in changed_file.hunks:
            line_range = hunk.new_range
            if hunk.old_count == 0 or line_range is None:
                continue
            first, last = line_range
            if not any(
                not (origin_spans[origin][1] < first or origin_spans[origin][0] > last)
                for origin in mapped_origins
            ):
                errors.append(
                    _error(
                        "changed_safety_change_unmapped",
                        "a replaced safety-source hunk has no current branch evidence",
                        line=hunk.new_first,
                        path=path,
                    )
                )
        for origin in sorted(mapped_origins):
            executed_at_origin = sorted(
                branch for branch in executed_set if branch[0] == origin
            )
            missing_at_origin = sorted(
                branch for branch in missing_set if branch[0] == origin
            )
            if set(executed_at_origin) & set(missing_at_origin):
                errors.append(
                    _error(
                        "changed_branch_coverage_conflict",
                        "a changed branch arc is both executed and missing",
                        line=origin,
                        path=path,
                    )
                )
            if missing_at_origin:
                errors.append(
                    _error(
                        "changed_branch_uncovered",
                        "every arc from a changed safety branch must be executed",
                        line=origin,
                        path=path,
                    )
                )
            matching_specs = sorted(
                (
                    spec
                    for first, last, spec in eligible_anchors.get(path, ())
                    if first <= origin <= last
                ),
                key=lambda spec: spec.mutation_id,
            )
            changed_branches.append(
                {
                    "executed_arcs": [list(branch) for branch in executed_at_origin],
                    "line": origin,
                    "missing_arcs": [list(branch) for branch in missing_at_origin],
                    "mutation_ids": [spec.mutation_id for spec in matching_specs],
                    "path": path,
                    "test_ids": sorted(
                        {
                            test_id
                            for spec in matching_specs
                            for test_id in spec.test_ids
                        }
                    ),
                }
            )

    errors.sort(
        key=lambda item: (
            str(item["code"]),
            str(item.get("path", "")),
            int(item.get("line", 0)) if type(item.get("line", 0)) is int else 0,
            str(item.get("mutation_id", "")),
            str(item["message"]),
        )
    )
    invariants.sort(key=lambda item: item["mutation_id"])
    changed_branches.sort(key=lambda item: (item["path"], item["line"]))
    changed_files_output.sort(
        key=lambda item: (
            str(item["new_path"] or item["old_path"]),
            str(item["status"]),
        )
    )
    digest_input = {
        "changed_branches": changed_branches,
        "changed_files": changed_files_output,
        "invariants": invariants,
        "provenance": provenance,
    }
    evidence_bytes = json.dumps(
        digest_input,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return {
        "changed_branch_count": len(changed_branches),
        "changed_branches": changed_branches,
        "changed_files": changed_files_output,
        "errors": errors,
        "evidence_digest": "sha256:" + hashlib.sha256(evidence_bytes).hexdigest(),
        "invariant_count": len(invariants),
        "invariants": invariants,
        "provenance": provenance,
        "schema_version": SCHEMA_VERSION,
        "status": (
            "pass"
            if not errors and len(invariants) == len(mutation_specs)
            else "fail"
        ),
    }


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def _write_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("mutation_json", type=Path)
    parser.add_argument(
        "--base-ref",
        required=True,
        help="Git ref whose merge base with HEAD defines changed added lines",
    )
    parser.add_argument(
        "--changed-lines",
        type=Path,
        help="strict changed-lines JSON artifact to verify against --base-ref",
    )
    parser.add_argument(
        "--changed-lines-output",
        type=Path,
        help="atomically write the exact changed-lines artifact used for evaluation",
    )
    parser.add_argument("--output", type=Path)
    return parser


def _input_error_result(code: str, message: str) -> dict[str, object]:
    return {
        "changed_branch_count": 0,
        "changed_branches": [],
        "changed_files": [],
        "errors": [_error(code, message)],
        "evidence_digest": None,
        "invariant_count": 0,
        "invariants": [],
        "provenance": {"base_ref": None, "head": None, "merge_base": None},
        "schema_version": SCHEMA_VERSION,
        "status": "error",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.changed_lines is not None:
            changed_lines = _read_json(arguments.changed_lines)
            trusted_changed_lines = collect_changed_lines(
                REPOSITORY_ROOT,
                arguments.base_ref,
                governed_paths=ACTIVE_M1_SAFETY_PATHS.git_pathspecs,
            )
            if changed_lines != trusted_changed_lines:
                raise ChangedLinesInputError(
                    "changed_lines_provenance_mismatch",
                    "replayed changed-lines evidence differs from the current Git comparison",
                )
        else:
            changed_lines = collect_changed_lines(
                REPOSITORY_ROOT,
                arguments.base_ref,
                governed_paths=ACTIVE_M1_SAFETY_PATHS.git_pathspecs,
            )
            trusted_changed_lines = changed_lines
        if arguments.changed_lines_output is not None:
            _write_atomic(arguments.changed_lines_output, _json_bytes(changed_lines))
        result = evaluate_safety_evidence(
            _read_json(arguments.coverage_json),
            _read_json(arguments.mutation_json),
            changed_lines,
            trusted_changed_lines_report=trusted_changed_lines,
        )
    except ChangedLinesInputError as exc:
        result = _input_error_result(exc.code, str(exc))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = _input_error_result("input_error", str(exc) or type(exc).__name__)
    output = _json_bytes(result)
    if arguments.output is not None:
        try:
            _write_atomic(arguments.output, output)
        except OSError as exc:
            print(f"cannot write safety evidence: {exc}", file=sys.stderr)
            return 2
    sys.stdout.buffer.write(output)
    return 0 if result["status"] == "pass" else 1 if result["status"] == "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
