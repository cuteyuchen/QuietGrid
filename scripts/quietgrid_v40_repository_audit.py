from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_SHA = "50d681485503415ff339a8c24a3b90fda6049bb7"
FREEZE_EVIDENCE_SHA = "f5c2a6a45b28b348dcc50c3cbbda6d206b11e102"
FREEZE_TAG = "semiconductor-grid-forward-oos-v2.9-freeze"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8", errors="replace", stderr=subprocess.STDOUT).strip()


def object_exists(root: Path, sha: str) -> bool:
    try:
        git(root, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def commit_summary(root: Path, ref: str) -> dict[str, Any]:
    sha, date, subject = git(root, "show", "-s", "--format=%H%x09%cI%x09%s", ref).split("\t", 2)
    return {"sha": sha, "date": date, "subject": subject}


def branch_row(root: Path, name: str, scope: str, base: str) -> dict[str, Any]:
    head = commit_summary(root, name)

    def count(*args: str) -> int:
        try:
            return int(git(root, "rev-list", "--count", *args))
        except subprocess.CalledProcessError:
            return -1

    unique = git(root, "rev-list", "--format=%H", name, "--not", base).splitlines()
    unique = [line for line in unique if len(line) == 40]
    report_files = git(root, "diff", "--name-only", f"{base}...{name}", "--", "reports", "docs").splitlines()
    report_dirs = sorted({str(Path(path).parent) for path in report_files if path})
    evidence_names = {"final-report.md", "candidate-selection.json", "acceptance-gates.json", "forward_oos_status.md", "freeze-report.md"}
    evidence_files = [path for path in git(root, "ls-tree", "-r", "--name-only", name, "--", "reports", "docs").splitlines() if Path(path).name in evidence_names]
    conclusions: set[str] = set()
    for path in evidence_files:
        try:
            content = git(root, "show", f"{name}:{path}")
        except subprocess.CalledProcessError:
            continue
        conclusions.update(re.findall(r"\b(?:PASS|FAIL|BLOCKED|INSUFFICIENT|NOT_EVALUATED|INCOMPLETE|UNKNOWN)[A-Z0-9_\-]*\b", content))
    ahead = count(f"{base}..{name}")
    behind = count(f"{name}..{base}")
    short_name = name.removeprefix("origin/")
    if short_name in {"master", "codex/semiconductor-grid-forward-oos-monitor-v2.9.1", "codex/semiconductor-grid-testnet-shadow-v4.0"}:
        classification = "KEEP_ACTIVE"
    elif head["sha"] == BASE_SHA:
        classification = "KEEP_ACTIVE"
    elif not unique:
        classification = "MERGED_SAFE_TO_DELETE"
    elif report_dirs:
        classification = "KEEP_RESEARCH_REFERENCE"
    else:
        classification = "UNKNOWN_DO_NOT_DELETE"
    try:
        is_name_ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", name, base]).returncode == 0
        is_base_ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", base, name]).returncode == 0
    except OSError:
        is_name_ancestor = False
        is_base_ancestor = False
    return {
        "branch_name": name.removeprefix("refs/remotes/"),
        "scope": scope,
        "head_sha": head["sha"],
        "head_commit_date": head["date"],
        "head_subject": head["subject"],
        "merge_base_vs_v4_base": git(root, "merge-base", name, base),
        "is_ancestor_of_base": is_name_ancestor,
        "is_base_ancestor_of_branch": is_base_ancestor,
        "ahead_count_vs_base": ahead,
        "behind_count_vs_base": behind,
        "unique_commit_count": len(unique),
        "unique_commit_shas": unique,
        "report_directories": report_dirs,
        "known_evidence_files": evidence_files,
        "conclusion_codes": sorted(conclusions),
        "classification": classification,
        "tags_containing_head": [tag for tag in git(root, "tag", "--contains", head["sha"]).splitlines() if tag],
    }


def freeze_integrity(root: Path) -> dict[str, Any]:
    report = root / "reports" / "semiconductor-grid-forward-oos-v2.9"
    candidate = report / "candidate-freeze.json"
    ledger = report / "forward-oos-ledger.csv"
    ledger_bytes = ledger.read_bytes()
    candidate_data = json.loads(candidate.read_text(encoding="utf-8"))
    candidate_blob_base = git(root, "rev-parse", f"{BASE_SHA}:reports/semiconductor-grid-forward-oos-v2.9/candidate-freeze.json")
    candidate_blob_evidence = git(root, "rev-parse", f"{FREEZE_EVIDENCE_SHA}:reports/semiconductor-grid-forward-oos-v2.9/candidate-freeze.json")
    with ledger.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    candidate_sha = next((row.get("candidate_sha") for row in rows if row.get("candidate_sha")), None)
    ok = candidate_blob_base == candidate_blob_evidence == "8d78533ef4601439cb9d14bb0b4628b596cf4fd0"
    return {
        "result": "PASS_FREEZE_INTEGRITY" if ok else "FAIL_FREEZE_INTEGRITY",
        "base_candidate_freeze_blob_sha": candidate_blob_base,
        "evidence_candidate_freeze_blob_sha": candidate_blob_evidence,
        "working_candidate_freeze_blob_sha": git(root, "hash-object", str(candidate)),
        "working_candidate_freeze_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "candidate_id": candidate_data.get("candidate_id"),
        "candidate_sha": candidate_sha,
        "freeze_commit_sha": candidate_data.get("freeze_commit_sha"),
        "freeze_time_utc": candidate_data.get("freeze_time_utc"),
        "exposure_cutoff": candidate_data.get("exposure_cutoff"),
        "freeze_tag": FREEZE_TAG,
        "freeze_tag_target": git(root, "rev-list", "-n", "1", FREEZE_TAG),
        "ledger_before_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "ledger_before_row_count": len(rows),
        "ledger_prefix_sha256": hashlib.sha256(ledger_bytes[:4096]).hexdigest(),
        "ledger_prefix_bytes": min(4096, len(ledger_bytes)),
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "reports" / "repository-consolidation-v4.0"
    out.mkdir(parents=True, exist_ok=True)
    local = [name for name in git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines() if name]
    remote = [name for name in git(root, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").splitlines() if name and name != "origin/HEAD"]
    rows = [branch_row(root, name, "both" if f"origin/{name}" in remote else "local", BASE_SHA) for name in local]
    rows += [branch_row(root, name, "remote", BASE_SHA) for name in remote if name.removeprefix("origin/") not in local]
    recovery: dict[str, Any] = {}
    for version, sha in (("v3.4", "32df63f7a843bdf0982d580f427026f416d70118"), ("v3.5", "996151e9f910786494c03e7d5e5bfef5a525e964")):
        found = object_exists(root, sha)
        recovery[version] = {"sha": sha, "status": "RECOVERED_LOCAL_OBJECT" if found else "MISSING_REMOTE_EVIDENCE", "summary": commit_summary(root, sha) if found else None}
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "base_sha": BASE_SHA, "local_branches": len(local), "remote_branches": len(remote), "tag_count": len(git(root, "tag", "--list").splitlines()), "branches": rows, "recovery": recovery, "no_branches_deleted": True, "no_existing_tags_moved": True}
    (out / "branch-audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    freeze = freeze_integrity(root)
    (out / "freeze-integrity.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# QuietGrid v4.0 Branch Audit", "", f"Base: {BASE_SHA}", f"Local branches: {len(local)}", f"Remote branches: {len(remote)}", f"Tags: {payload['tag_count']}", "", "## Recovery", ""]
    lines.extend(f"- {version}: {item['status']} ({item['sha']})" for version, item in recovery.items())
    lines += ["", "## Classification", "", "| Branch | Scope | Head | Classification | Unique commits |", "|---|---|---|---|---:|"]
    lines.extend(f"| {item['branch_name']} | {item['scope']} | {item['head_sha'][:12]} | {item['classification']} | {item['unique_commit_count']} |" for item in rows)
    lines += ["", "NO BRANCHES DELETED", "NO EXISTING TAGS MOVED", ""]
    (out / "BRANCH_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")
    plan = ["# QuietGrid v4.0 Branch Cleanup Plan", "", "PROPOSED ONLY — NOT EXECUTED", "", "本轮只生成建议，不创建 archive tag、不删除 branch、不移动 tag。", "", "## Proposed annotated tags", "", "git tag -a research/v2.8-final 7a24b18c0f027853fcfd939e8d2cb375f2e99c59 -m 'archive v2.8 research'", "git tag -a research/v2.9-freeze f5c2a6a45b28b348dcc50c3cbbda6d206b11e102 -m 'archive v2.9 freeze'", "git tag -a research/v2.9.2-diagnostics abe6ebf4474ac0362707fa631d29e35a81cc81b4 -m 'archive v2.9.2 diagnostics'", "git tag -a research/v3.2-final eb64ed821b09f285041639a1502456d24bb40e2f -m 'archive v3.2 research'", "git tag -a research/v3.3-final 3c15782da79dab306154d02653ca59201370b76f -m 'archive v3.3 research'", "git tag -a research/v3.4-final 32df63f7a843bdf0982d580f427026f416d70118 -m 'archive v3.4 research'", "git tag -a research/v3.5-final 996151e9f910786494c03e7d5e5bfef5a525e964 -m 'archive v3.5 research'", "", "## Proposed branch deletes", ""]
    plan.extend(f"git branch -d {item['branch_name']}" for item in rows if item["classification"] in {"ARCHIVE_TAG_THEN_DELETE_BRANCH", "MERGED_SAFE_TO_DELETE"} and not item["branch_name"].startswith("origin/"))
    plan += ["", "All commands above are proposed only. Existing freeze tag is not moved or replaced.", ""]
    (out / "BRANCH_CLEANUP_PLAN.md").write_text("\n".join(plan), encoding="utf-8")
    print(json.dumps({"audit": str(out), "freeze": freeze, "recovery": recovery}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
