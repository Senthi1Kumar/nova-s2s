#!/usr/bin/env python3
"""Fail-closed inventory for the Nova-Hailo model matrix manifest."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from nova_hailo.bench.contracts import (
    SUPPORTED,
    UNAVAILABLE,
    CandidateStatus,
    file_size,
    sha256_file,
)
from nova_hailo.config import ROOT

DEFAULT_MANIFEST = ROOT / "bench" / "model_matrix.v0.0.1.yaml"


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    with open(Path(path).expanduser()) as f:
        return yaml.safe_load(f) or {}


def resolve_manifest_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


def declared_artifact_candidates(candidate: dict[str, Any]) -> list[Path]:
    raw_candidates = candidate.get("artifact_candidates")
    if raw_candidates:
        return [resolve_manifest_path(p) for p in raw_candidates]
    artifact = candidate.get("artifact")
    if artifact:
        return [resolve_manifest_path(artifact)]
    return []


def find_declared_artifact(candidate: dict[str, Any]) -> Path | None:
    for path in declared_artifact_candidates(candidate):
        if path.is_file():
            return path
    return None


def detect_hailort_version() -> str | None:
    versions: list[str] = []
    try:
        hailo_platform = importlib.import_module("hailo_platform")
        version = getattr(hailo_platform, "__version__", None)
        if version:
            versions.append(f"hailo_platform {version}")
    except Exception:
        pass
    try:
        version = importlib.metadata.version("hailo_platform")
        versions.append(f"hailo_platform {version}")
    except Exception:
        pass
    hailortcli = shutil.which("hailortcli")
    if hailortcli:
        try:
            proc = subprocess.run(
                [hailortcli, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = " ".join((proc.stdout, proc.stderr)).strip()
            if output:
                versions.append(output.splitlines()[0])
        except Exception:
            pass
    return "; ".join(dict.fromkeys(versions)) or None


def candidate_status(candidate: dict[str, Any], hailort_version: str | None) -> CandidateStatus:
    support = str(candidate.get("support") or UNAVAILABLE)
    path = find_declared_artifact(candidate)
    declared = declared_artifact_candidates(candidate)
    status_support = support
    notes: list[str] = []
    if support == SUPPORTED and declared and path is None:
        status_support = UNAVAILABLE
        notes.append("supported candidate has no declared artifact on this host")
    if declared:
        notes.append("declared_candidates=" + ",".join(str(p) for p in declared))
    size = file_size(path) if path else None
    # Release rule: always hash artifacts (provenance is the point of this
    # inventory — a wrong Whisper blob already burned us once). Opt out only
    # via NOVA_MATRIX_SKIP_HASH=1 for quick desk dry-runs.
    digest = None
    if path is not None and size is not None:
        if os.environ.get("NOVA_MATRIX_SKIP_HASH", "") == "1" and size > 200_000_000:
            notes.append(f"sha256_skipped_large_file_bytes={size}")
        else:
            digest = sha256_file(path)
    return CandidateStatus(
        candidate_id=str(candidate["id"]),
        component=str(candidate.get("component") or "unknown"),
        support=status_support,
        reason=str(candidate.get("reason") or ""),
        artifact_path=str(path) if path else None,
        sha256=digest,
        size_bytes=size,
        runtime=str(candidate.get("runtime")) if candidate.get("runtime") is not None else None,
        backend=str(candidate.get("backend")) if candidate.get("backend") is not None else None,
        license=str(candidate.get("license")) if candidate.get("license") is not None else None,
        expected_memory_mb=candidate.get("expected_memory_mb"),
        notes=notes,
    )


def inventory(manifest_path: str | Path = DEFAULT_MANIFEST) -> list[CandidateStatus]:
    manifest = load_manifest(manifest_path)
    hailort_version = detect_hailort_version()
    return [
        candidate_status(candidate, hailort_version)
        for candidate in manifest.get("candidates", [])
    ]


def has_fail_closed_missing(statuses: list[CandidateStatus]) -> bool:
    return any(
        s.support == UNAVAILABLE
        and any(note.startswith("supported candidate") for note in s.notes)
        for s in statuses
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--fail-closed", action="store_true")
    args = parser.parse_args()

    statuses = inventory(args.manifest)
    rows = [s.to_dict() for s in statuses]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(row)
    if args.fail_closed and has_fail_closed_missing(statuses):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
