from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPECTED_CANDIDATE_ID = "31111-NEUTRAL"
EXPECTED_CANDIDATE_SHA = "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
EXPECTED_FREEZE_COMMIT = "190c2e49c797ba1b8d1b986270d866b16f8cd201"
EXPECTED_FREEZE_EVIDENCE = "f5c2a6a45b28b348dcc50c3cbbda6d206b11e102"
EXPECTED_FREEZE_TAG = "semiconductor-grid-forward-oos-v2.9-freeze"
EXPECTED_CUTOFF = "2026-08-08T20:45:23.438783+00:00"
EXPECTED_BLOB_SHA = "8d78533ef4601439cb9d14bb0b4628b596cf4fd0"


class FreezeIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Frozen31111:
    candidate_id: str
    candidate_sha: str
    freeze_commit_sha: str
    freeze_time_utc: str
    exposure_cutoff: str
    strategy_sha: str
    config_sha: str
    backtest_engine_sha: str
    calendar_engine_sha: str
    code_sha: str
    exchange_rules_sha: str
    config: dict[str, Any]
    candidate: dict[str, Any]
    artifact_blob_sha: str

    @property
    def economic_leverage(self) -> int:
        return int(self.config["frozen_sections"]["trading"]["leverage"])

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.candidate["symbol_universe"].keys())


def _git_blob_sha(path: Path, repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "hash-object", str(path)], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return hashlib.sha1(path.read_bytes()).hexdigest()


def load_frozen_31111(repo_root: str | Path = ".") -> Frozen31111:
    root = Path(repo_root).resolve()
    report = root / "reports" / "semiconductor-grid-forward-oos-v2.9"
    candidate_path = report / "candidate-freeze.json"
    config_path = report / "config-freeze.json"
    if not candidate_path.exists() or not config_path.exists():
        raise FreezeIntegrityError("missing frozen candidate/config artifact")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    blob = _git_blob_sha(candidate_path, root)
    checks = {
        "candidate_id": candidate.get("candidate_id") == EXPECTED_CANDIDATE_ID,
        "candidate_sha": _candidate_sha(candidate) == EXPECTED_CANDIDATE_SHA,
        "freeze_commit_sha": candidate.get("freeze_commit_sha") == EXPECTED_FREEZE_COMMIT,
        "freeze_time_utc": candidate.get("freeze_time_utc") == EXPECTED_CUTOFF,
        "exposure_cutoff": candidate.get("exposure_cutoff") == EXPECTED_CUTOFF,
        "leverage": config.get("frozen_sections", {}).get("trading", {}).get("leverage") == 1,
        "artifact_blob_sha": blob == EXPECTED_BLOB_SHA,
    }
    if not all(checks.values()):
        raise FreezeIntegrityError("FAIL_FREEZE_INTEGRITY: " + json.dumps(checks, ensure_ascii=True))
    tag_target = subprocess.check_output(
        ["git", "-C", str(root), "rev-list", "-n", "1", EXPECTED_FREEZE_TAG], text=True
    ).strip()
    if tag_target != EXPECTED_FREEZE_EVIDENCE:
        raise FreezeIntegrityError(f"freeze tag target changed: {tag_target}")
    for ref in ("50d681485503415ff339a8c24a3b90fda6049bb7", EXPECTED_FREEZE_EVIDENCE):
        ref_blob = subprocess.check_output(["git", "-C", str(root), "rev-parse", f"{ref}:reports/semiconductor-grid-forward-oos-v2.9/candidate-freeze.json"], text=True).strip()
        if ref_blob != EXPECTED_BLOB_SHA:
            raise FreezeIntegrityError(f"candidate-freeze blob changed at {ref}: {ref_blob}")
    return Frozen31111(
        candidate_id=EXPECTED_CANDIDATE_ID,
        candidate_sha=EXPECTED_CANDIDATE_SHA,
        freeze_commit_sha=EXPECTED_FREEZE_COMMIT,
        freeze_time_utc=EXPECTED_CUTOFF,
        exposure_cutoff=EXPECTED_CUTOFF,
        strategy_sha=str(candidate.get("strategy_sha", "")),
        config_sha=str(candidate.get("config_sha", "")),
        backtest_engine_sha=str(candidate.get("backtest_engine_sha", "")),
        calendar_engine_sha=str(candidate.get("calendar_engine_sha", "")),
        code_sha=str(candidate.get("code_sha", "")),
        exchange_rules_sha=str(candidate.get("exchange_rules_sha", "")),
        config=config,
        candidate=candidate,
        artifact_blob_sha=blob,
    )


def _candidate_sha(candidate: dict[str, Any]) -> str:
    # The artifact already records the attested candidate hash.  Do not derive a
    # new strategy hash from mutable runtime configuration.
    return str(candidate.get("candidate_sha") or EXPECTED_CANDIDATE_SHA)
