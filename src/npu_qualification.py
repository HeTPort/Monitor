"""NPU known-good evidence gates, deliberately separate from run consistency."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .artifact_store import atomic_write_json, sha256_file
from .evidence_collection import validated_hash_entries
from .qualification import QualificationError, correctness_fingerprint, _now
from .qualification_artifacts import resolve_qualification_run


def validate_source_run(run_dir, profile, *, golden=False):
    artifacts = resolve_qualification_run(run_dir)
    if artifacts.pc_run_dir is None or artifacts.spool_dir is None or artifacts.result_path is None:
        raise QualificationError("NPU qualification requires paired PC run and collected device spool")
    pc = artifacts.pc_run_dir
    result = json.loads(artifacts.result_path.read_text())
    manifest = json.loads((pc / "run-manifest.json").read_text())
    spool = artifacts.spool_dir
    hashes = validated_hash_entries(json.loads((spool / "artifact-hashes.json").read_text()))
    required = {"events.jsonl", "workload-summary.json", "final.json"}
    if not required.issubset(hashes):
        raise QualificationError("NPU qualification device evidence is incomplete")
    for name in required:
        if sha256_file(spool / name) != hashes[name]:
            raise QualificationError(f"NPU qualification evidence hash mismatch: {name}")
    summary = artifacts.summary
    config_value = profile.workload.get("config")
    if not isinstance(config_value, str):
        raise QualificationError("NPU qualification requires a model configuration")
    config_path = (profile.source_path.parent / config_value).resolve(strict=True)
    native_config = json.loads(config_path.read_text())
    if summary.get("profile") != native_config.get("profile"):
        raise QualificationError("NPU native model identity mismatch")
    final = json.loads((spool / "final.json").read_text())
    final = final.get("payload", final)
    if (result.get("verdict") != "PASS" or result.get("exit_code") != 0
            or manifest.get("target") != "npu" or manifest.get("profile", {}).get("sha256") != profile.fingerprint
            or manifest.get("run_id") != result.get("run_id")
            or summary.get("backend") != "tflite_delegate" or summary.get("fallback_used") is not False
            or summary.get("result") != "PASS" or summary.get("exit_code") != 0
            or summary.get("contract_version") != 2 or final.get("workload_exit_code") != 0
            or final.get("summary_seen") is not True):
        raise QualificationError("NPU qualification identity, FINAL, or real backend evidence is invalid")
    if final.get("test_id") != manifest.get("test_id") or final.get("attempt_id") != manifest.get("attempt_id") or final.get("timed_out") is not False:
        raise QualificationError("NPU FINAL identity/deadline mismatch")
    deployment = json.loads((pc / "deployment-manifest.json").read_text())
    placement = deployment.get("npu_placement", {})
    evidence_path = (profile.source_path.parent / profile.workload.get("placement_evidence", "")).resolve()
    if placement.get("status") != "full_npu" or not evidence_path.is_file() or placement.get("evidence_sha256") != sha256_file(evidence_path):
        raise QualificationError("NPU source run lacks bound vendor placement evidence")
    records = [json.loads(line) for line in (spool / "events.jsonl").read_text().splitlines() if line.strip()]
    if any(record.get("run_id") != manifest["run_id"] for record in records):
        raise QualificationError("NPU source event identity mismatch")
    if golden and (manifest.get("qualification", {}).get("mode") != "golden"
                   or summary.get("inference_count") != 0 or summary.get("verify_count") != 0):
        raise QualificationError("NPU golden sources must be explicit generation runs, excluded from measured counts")
    return summary, placement


def create_npu(service, *, qualification_id, profile, fingerprint_fields, golden_records, board_ids, approver, conditions):
    records = list(golden_records)
    if not isinstance(approver, str) or not approver.strip() or not isinstance(conditions, str) or not conditions.strip():
        raise QualificationError("NPU known-good golden requires approver and generation conditions")
    checksums = [record.get("checksum") for record in records]
    if not records or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}|[0-9a-fA-F]{16}", value) for value in checksums):
        raise QualificationError("NPU golden records need valid exact checksums")
    if len(set(value.lower() for value in checksums)) != 1:
        raise QualificationError("NPU golden checksums disagree")
    if fingerprint_fields.get("target") != "npu" or not fingerprint_fields.get("placement_evidence_sha256"):
        raise QualificationError("NPU golden must bind model, inputs, runtime and vendor evidence")
    manifest = {"schema_version": 1, "kind": "npu-checksum", "qualification_id": qualification_id,
                "profile": profile, "created_at": _now(), "correctness_fingerprint": correctness_fingerprint(fingerprint_fields),
                "fingerprint_fields": dict(fingerprint_fields), "checksum": checksums[0].lower(),
                "repeat_count": len(records), "board_ids": list(board_ids), "records": records,
                "approval": {"approver": approver, "conditions": conditions, "approved_at": _now(), "known_good": True}}
    path = service._golden_dir(qualification_id) / "npu-golden.json"
    if path.exists():
        raise QualificationError(f"NPU golden destination exists: {path}")
    atomic_write_json(path, manifest)
    return manifest | {"manifest_path": str(path), "manifest_sha256": sha256_file(path)}
