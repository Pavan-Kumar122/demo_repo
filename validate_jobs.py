"""
validate_jobs.py
----------------
Core validation engine. Compares Autosys JIL jobs against Stonebranch UAC JSON tasks
using official mapping rules from config/mapping_rules.yaml and config/field_rules.yaml.

Validation buckets:
  ✅ PASS    - All required fields present and correctly mapped
  ⚠️ REVIEW  - Fields present but suspicious, approximate mapping, or ambiguous
  ❌ FAIL    - Field missing, wrong type, broken dependency, or job not converted

Usage:
  python validate_jobs.py
  python validate_jobs.py --jil-dir autosys/ --sb-dir stonebranch/ --output reports/

  from validate_jobs import run_validation
  results = run_validation("autosys/", "stonebranch/")
"""

import os
import re
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

import yaml

# Local parsers
from parse_jil import parse_all_jil_files
from parse_stonebranch import parse_all_sb_files

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
PASS   = "PASS"
REVIEW = "REVIEW"
FAIL   = "FAIL"

STATUS_PRIORITY = {FAIL: 0, REVIEW: 1, PASS: 2}  # Lower = worse


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config(config_dir: str) -> Tuple[Dict, Dict]:
    """Load mapping_rules.yaml and field_rules.yaml from config directory."""
    config_path = Path(config_dir)

    mapping_file = config_path / "mapping_rules.yaml"
    field_file   = config_path / "field_rules.yaml"

    if not mapping_file.exists():
        raise FileNotFoundError(f"mapping_rules.yaml not found at {mapping_file}")
    if not field_file.exists():
        raise FileNotFoundError(f"field_rules.yaml not found at {field_file}")

    with open(mapping_file, encoding="utf-8") as f:
        mapping_rules = yaml.safe_load(f)

    with open(field_file, encoding="utf-8") as f:
        field_rules = yaml.safe_load(f)

    return mapping_rules, field_rules


# ---------------------------------------------------------------------------
# Issue builder
# ---------------------------------------------------------------------------

def _issue(severity: str, field: str, description: str,
           jil_value: Any = "", uac_value: Any = "") -> Dict:
    """Create a standardised issue dict."""
    return {
        "severity":    severity,
        "field":       field,
        "description": description,
        "jil_value":   str(jil_value) if jil_value is not None else "",
        "uac_value":   str(uac_value) if uac_value is not None else "",
    }


# ---------------------------------------------------------------------------
# Type mapping validator
# ---------------------------------------------------------------------------

def _validate_type_mapping(jil_job: Dict, sb_task: Dict,
                           mapping_rules: Dict) -> List[Dict]:
    """
    Validate that the UAC task type correctly maps from the JIL job type.
    Returns a list of issues.
    """
    issues = []
    jil_type = jil_job.get("job_type", "").upper()
    uac_type  = sb_task.get("type", "").strip()
    job_type_map = mapping_rules.get("job_type_mapping", {})

    if jil_type == "CMD":
        detected_os = jil_job.get("detected_os", "linux")
        expected_type = (
            job_type_map["CMD"]["stonebranch_type_windows"]
            if detected_os == "windows"
            else job_type_map["CMD"]["stonebranch_type_linux"]
        )
        if uac_type not in ("taskUnix", "taskWindows"):
            issues.append(_issue(
                FAIL, "type",
                f"CMD job must map to taskUnix or taskWindows, found '{uac_type}'",
                jil_value=f"CMD ({detected_os})", uac_value=uac_type
            ))
        elif uac_type != expected_type:
            issues.append(_issue(
                REVIEW, "type",
                f"CMD job on '{detected_os}' expected '{expected_type}' but found '{uac_type}'. "
                "Verify the OS of the target agent.",
                jil_value=f"CMD ({detected_os})", uac_value=uac_type
            ))

    elif jil_type == "BOX":
        expected = job_type_map["BOX"]["stonebranch_type"]
        if uac_type != expected:
            issues.append(_issue(
                FAIL, "type",
                f"BOX job must map to '{expected}', found '{uac_type}'",
                jil_value="BOX", uac_value=uac_type
            ))

    elif jil_type == "FW":
        expected = job_type_map["FW"]["stonebranch_type"]
        if uac_type == "taskFileMonitorRemote":
            issues.append(_issue(
                REVIEW, "type",
                "FW (local file watcher) mapped to taskFileMonitorRemote instead of taskFileMonitor. "
                "taskFileMonitorRemote is for FT jobs (FTP-based). Verify if this is intentional.",
                jil_value="FW", uac_value=uac_type
            ))
        elif uac_type != expected:
            issues.append(_issue(
                FAIL, "type",
                f"FW job must map to '{expected}' (Agent File Monitor), found '{uac_type}'",
                jil_value="FW", uac_value=uac_type
            ))

    elif jil_type == "FT":
        expected = job_type_map["FT"]["stonebranch_type"]
        if uac_type == "taskFileMonitor":
            issues.append(_issue(
                REVIEW, "type",
                "FT (FTP file trigger) mapped to taskFileMonitor instead of taskFileMonitorRemote. "
                "taskFileMonitor is for FW jobs (agent-based). Verify if FTP is still needed.",
                jil_value="FT", uac_value=uac_type
            ))
        elif uac_type != expected:
            issues.append(_issue(
                FAIL, "type",
                f"FT job must map to '{expected}' (Remote File Monitor), found '{uac_type}'",
                jil_value="FT", uac_value=uac_type
            ))

    return issues


# ---------------------------------------------------------------------------
# Field-level validators
# ---------------------------------------------------------------------------

def _normalise_path(path: str) -> str:
    """Normalise file paths for comparison: lowercase, forward slashes, strip whitespace."""
    return path.strip().lower().replace("\\", "/").rstrip("/")


def _validate_field(rule: Dict, jil_job: Dict, sb_task: Dict,
                    mapping_rules: Dict) -> List[Dict]:
    """
    Validate a single field rule between JIL job and Stonebranch task.
    Returns a list of issues.
    """
    issues = []
    jil_field   = rule["jil_field"]
    uac_field   = rule["uac_field"]
    match_type  = rule.get("match_type", "exists")
    severity    = {"fail": FAIL, "review": REVIEW, "info": PASS}.get(str(rule.get("severity", REVIEW)).lower(), REVIEW)
    description = rule.get("description", "")
    always_review = rule.get("always_review", False)

    jil_val = jil_job.get(jil_field)
    uac_val = sb_task.get(uac_field)

    # Special: always flag certain fields regardless of value
    if always_review:
        if jil_val is not None and jil_val != "" and jil_val != []:
            issues.append(_issue(
                REVIEW, jil_field,
                description,
                jil_value=jil_val, uac_value="N/A"
            ))
        return issues

    # --- Type map validation (handled separately in _validate_type_mapping) ---
    if match_type == "type_map":
        return issues  # Already validated above

    # --- Schedule validation ---
    if match_type == "schedule":
        return _validate_schedule(jil_field, jil_val, sb_task, mapping_rules, severity, description)

    # --- Condition validation ---
    if match_type == "condition":
        return _validate_conditions(jil_job, sb_task, mapping_rules)

    # --- Field is in JIL but missing in UAC ---
    if jil_val is not None and jil_val != "" and jil_val != []:
        if uac_val is None or uac_val == "" or uac_val == []:
            actual_severity = rule.get("missing_severity", severity)
            issues.append(_issue(
                actual_severity, jil_field,
                f"{description} — present in JIL but missing in Stonebranch",
                jil_value=jil_val, uac_value="(not found)"
            ))
            return issues

        # --- Both present — check values ---
        if match_type == "exact":
            if str(jil_val).lower().strip() != str(uac_val).lower().strip():
                issues.append(_issue(
                    severity, jil_field,
                    f"{description} — values differ",
                    jil_value=jil_val, uac_value=uac_val
                ))

        elif match_type == "normalised":
            if _normalise_path(str(jil_val)) != _normalise_path(str(uac_val)):
                actual_severity = rule.get("differs_severity", severity)
                issues.append(_issue(
                    actual_severity, jil_field,
                    f"{description} — paths differ (may be intentional upgrade/refactor)",
                    jil_value=jil_val, uac_value=uac_val
                ))

        elif match_type == "numeric":
            try:
                if float(jil_val) != float(uac_val):
                    issues.append(_issue(
                        severity, jil_field,
                        f"{description} — numeric values differ",
                        jil_value=jil_val, uac_value=uac_val
                    ))
            except (TypeError, ValueError):
                issues.append(_issue(
                    REVIEW, jil_field,
                    f"{description} — could not compare numerically",
                    jil_value=jil_val, uac_value=uac_val
                ))

        elif match_type == "bool":
            def to_bool(v):
                if isinstance(v, bool): return v
                return str(v).lower() in ("1", "yes", "true", "y")
            if to_bool(jil_val) and not to_bool(uac_val):
                issues.append(_issue(
                    severity, jil_field,
                    f"{description} — set in JIL but not configured in Stonebranch",
                    jil_value=jil_val, uac_value=uac_val
                ))

        # match_type == "exists" — just needed to be non-empty, already passed

    return issues


# ---------------------------------------------------------------------------
# Schedule validator
# ---------------------------------------------------------------------------

def _validate_schedule(jil_field: str, jil_val: Any, sb_task: Dict,
                       mapping_rules: Dict, severity: str, description: str) -> List[Dict]:
    """
    Validate that a JIL schedule (start_times / days_of_week / run_calendar)
    has a corresponding UAC trigger.
    """
    issues = []
    if not jil_val or jil_val == "" or jil_val == []:
        return issues  # No schedule in JIL — nothing to check

    triggers = sb_task.get("triggers", [])
    if not triggers:
        issues.append(_issue(
            FAIL, jil_field,
            f"{description} — JIL has '{jil_field}' = '{jil_val}' "
            "but no UAC trigger was found for this task.",
            jil_value=jil_val, uac_value="(no trigger found)"
        ))
        return issues

    # If triggers exist, do a soft check on cron vs time trigger type
    cron_pattern = mapping_rules.get("trigger_mapping", {}).get("cron_detection_pattern", r"\*|/|,")
    if jil_field == "start_times":
        jil_has_cron = bool(re.search(cron_pattern, str(jil_val)))
        for trig in triggers:
            trig_type = trig.get("trigger_type", "")
            if jil_has_cron and "cron" not in trig_type.lower():
                issues.append(_issue(
                    REVIEW, jil_field,
                    "JIL start_times looks like a cron expression but UAC trigger type "
                    f"is '{trig_type}' (expected triggerCron). Verify schedule equivalence.",
                    jil_value=jil_val, uac_value=trig_type
                ))

    return issues


# ---------------------------------------------------------------------------
# Dependency condition validator
# ---------------------------------------------------------------------------

def _validate_conditions(jil_job: Dict, sb_task: Dict,
                         mapping_rules: Dict) -> List[Dict]:
    """
    Validate dependency conditions from JIL against UAC task structure.
    Checks:
      1. Each referenced job exists (by name) in the UAC JSON set.
      2. Condition type is correctly mapped.
      3. Flags notrunning / done as REVIEW.
    """
    issues = []
    parsed_conditions = jil_job.get("parsed_conditions", [])
    if not parsed_conditions:
        return issues

    dep_mapping = mapping_rules.get("dependency_mapping", {})
    job_name = jil_job.get("job_name", "")

    for cond in parsed_conditions:
        cond_type = cond.get("type")

        if cond_type == "job_status":
            status  = cond.get("status", "").lower()
            ref_job = cond.get("job", "")

            mapping = dep_mapping.get(status)
            if not mapping:
                issues.append(_issue(
                    REVIEW, "condition",
                    f"Unrecognised condition status '{status}' — no UAC mapping defined",
                    jil_value=cond.get("raw"), uac_value="unknown"
                ))
                continue

            if mapping.get("review_required"):
                issues.append(_issue(
                    REVIEW, "condition",
                    mapping.get("review_note",
                        f"Condition '{status}' has no direct UAC equivalent — requires manual review"),
                    jil_value=cond.get("raw"),
                    uac_value=mapping.get("uac_equivalent", "")
                ))

        elif cond_type == "variable":
            issues.append(_issue(
                REVIEW, "condition",
                f"Variable condition v({cond.get('variable')}) must exist as a UAC Variable — verify manually",
                jil_value=cond.get("raw"), uac_value="(check UAC Variables)"
            ))

        elif cond_type == "unknown":
            issues.append(_issue(
                REVIEW, "condition",
                f"Could not parse condition '{cond.get('raw')}' — manual review required",
                jil_value=cond.get("raw"), uac_value="unknown"
            ))

    return issues


# ---------------------------------------------------------------------------
# BOX member validator
# ---------------------------------------------------------------------------

def _validate_box_members(jil_job: Dict, sb_task: Dict) -> List[Dict]:
    """
    For BOX jobs: verify all member jobs appear as vertices in the UAC workflow.
    """
    issues = []
    jil_members  = set(jil_job.get("members", []))
    uac_vertices = set(sb_task.get("workflow_vertices", []))

    if not jil_members:
        return issues  # No members parsed — can't validate

    missing = jil_members - uac_vertices
    extra   = uac_vertices - jil_members

    for m in sorted(missing):
        issues.append(_issue(
            FAIL, "workflow_vertices",
            f"BOX member job '{m}' is missing from UAC workflow vertices",
            jil_value=m, uac_value="(not found in vertices)"
        ))

    for e in sorted(extra):
        issues.append(_issue(
            REVIEW, "workflow_vertices",
            f"UAC workflow vertex '{e}' has no corresponding BOX member — unexpected extra task",
            jil_value="(not in BOX)", uac_value=e
        ))

    return issues


# ---------------------------------------------------------------------------
# Alarm validator
# ---------------------------------------------------------------------------

def _validate_alarms(jil_job: Dict, sb_task: Dict, mapping_rules: Dict) -> List[Dict]:
    """Validate alarm / notification field mappings."""
    issues = []
    alarm_mapping = mapping_rules.get("alarm_mapping", {})

    for jil_alarm, rule in alarm_mapping.items():
        jil_val = jil_job.get(jil_alarm)
        if jil_val is None or jil_val == "" or jil_val is False:
            continue  # Not set in JIL — nothing to validate

        uac_field   = rule.get("uac_equivalent", "")
        severity    = FAIL if rule.get("severity") == "fail" else REVIEW
        uac_val     = sb_task.get(uac_field)

        # For boolean alarms (alarm_if_fail = 1), check if UAC has an action configured
        if isinstance(jil_val, bool) and jil_val:
            if not uac_val:
                issues.append(_issue(
                    severity, jil_alarm,
                    f"'{jil_alarm}' set in JIL but '{uac_field}' ({rule.get('uac_action_type', '')}) "
                    "not configured in Stonebranch",
                    jil_value=jil_val, uac_value="(not configured)"
                ))

        # For numeric alarms (max_run_alarm = 3600), check value match
        elif isinstance(jil_val, (int, float)):
            if not uac_val:
                issues.append(_issue(
                    severity, jil_alarm,
                    f"'{jil_alarm}' = {jil_val} in JIL but '{uac_field}' not set in Stonebranch",
                    jil_value=jil_val, uac_value="(not set)"
                ))
            else:
                try:
                    if float(jil_val) != float(uac_val):
                        issues.append(_issue(
                            REVIEW, jil_alarm,
                            f"'{jil_alarm}' value differs",
                            jil_value=jil_val, uac_value=uac_val
                        ))
                except (TypeError, ValueError):
                    pass

        # For time-based alarms (must_complete_times = "18:00")
        elif isinstance(jil_val, str) and jil_val:
            if not uac_val:
                issues.append(_issue(
                    severity, jil_alarm,
                    f"'{jil_alarm}' = '{jil_val}' in JIL but '{uac_field}' "
                    f"({rule.get('uac_action_type', '')}) not configured in Stonebranch",
                    jil_value=jil_val, uac_value="(not configured)"
                ))

    return issues


# ---------------------------------------------------------------------------
# Circular dependency detector
# ---------------------------------------------------------------------------

def _detect_circular_dependencies(all_jil_jobs: Dict[str, Dict]) -> List[Dict]:
    """
    Detect circular dependency chains across all JIL jobs.
    Uses DFS cycle detection on the dependency graph.
    """
    issues = []

    # Build adjacency: job → list of jobs it depends on
    graph: Dict[str, List[str]] = {}
    for job_name, job in all_jil_jobs.items():
        deps = []
        for cond in job.get("parsed_conditions", []):
            if cond.get("type") == "job_status":
                ref = cond.get("job", "").strip()
                if ref and ref != job_name:
                    deps.append(ref)
        graph[job_name] = deps

    # DFS cycle detection
    visited:     set = set()
    in_stack:    set = set()
    cycles_found: set = set()

    def dfs(node: str, path: List[str]):
        if node in in_stack:
            cycle_start = path.index(node)
            cycle = " → ".join(path[cycle_start:] + [node])
            cycle_key = " → ".join(sorted(path[cycle_start:]))
            if cycle_key not in cycles_found:
                cycles_found.add(cycle_key)
                issues.append(_issue(
                    FAIL, "condition",
                    f"Circular dependency detected: {cycle}",
                    jil_value=cycle, uac_value="(circular)"
                ))
            return
        if node in visited:
            return

        visited.add(node)
        in_stack.add(node)
        for neighbour in graph.get(node, []):
            if neighbour in graph:
                dfs(neighbour, path + [node])
        in_stack.discard(node)

    for job_name in graph:
        if job_name not in visited:
            dfs(job_name, [])

    return issues


# ---------------------------------------------------------------------------
# Single job validator
# ---------------------------------------------------------------------------

def validate_job(jil_job: Dict, sb_task: Optional[Dict],
                 mapping_rules: Dict, field_rules: Dict) -> Dict:
    """
    Validate one JIL job against its Stonebranch counterpart.

    Returns a result dict with:
      job_name, job_type, status, issues[], uac_name, uac_type
    """
    job_name  = jil_job.get("job_name") or jil_job.get("box_name", "UNKNOWN")
    jil_type  = jil_job.get("job_type", "UNKNOWN")
    all_issues: List[Dict] = []

    # --- Job not found in Stonebranch at all ---
    if sb_task is None:
        return {
            "job_name":  job_name,
            "job_type":  jil_type,
            "uac_name":  "(not found)",
            "uac_type":  "(not found)",
            "status":    FAIL,
            "issues": [_issue(
                FAIL, "name",
                f"Job '{job_name}' not found in Stonebranch — not converted",
                jil_value=job_name, uac_value="(missing)"
            )]
        }

    uac_name = sb_task.get("name", "")
    uac_type = sb_task.get("type", "")

    # --- Type mapping validation ---
    all_issues.extend(_validate_type_mapping(jil_job, sb_task, mapping_rules))

    # --- Field-level validation (from field_rules.yaml) ---
    rules_for_type = field_rules.get(jil_type, {})
    all_field_rules = (
        rules_for_type.get("required_fields", []) +
        rules_for_type.get("soft_fields", [])
    )
    for rule in all_field_rules:
        all_issues.extend(_validate_field(rule, jil_job, sb_task, mapping_rules))

    # --- BOX-specific: member job validation ---
    if jil_type == "BOX":
        all_issues.extend(_validate_box_members(jil_job, sb_task))

    # --- Alarm / notification validation ---
    all_issues.extend(_validate_alarms(jil_job, sb_task, mapping_rules))

    # --- Determine overall status ---
    if any(i["severity"] == FAIL for i in all_issues):
        status = FAIL
    elif any(i["severity"] == REVIEW for i in all_issues):
        status = REVIEW
    else:
        status = PASS

    return {
        "job_name":  job_name,
        "job_type":  jil_type,
        "uac_name":  uac_name,
        "uac_type":  uac_type,
        "status":    status,
        "issues":    all_issues,
    }


# ---------------------------------------------------------------------------
# Full validation runner
# ---------------------------------------------------------------------------

def run_validation(jil_dir: str = "autosys",
                   sb_dir:  str = "stonebranch",
                   config_dir: str = "config") -> Dict:
    """
    Run full validation across all JIL and Stonebranch files.

    Returns a results dict with:
      - metadata (counts, timestamp)
      - results[]: one entry per JIL job
      - orphans[]: UAC tasks with no matching JIL job
    """
    logger.info("=" * 60)
    logger.info("Starting Autosys → Stonebranch Validation")
    logger.info("=" * 60)

    # Load configs
    mapping_rules, field_rules = _load_config(config_dir)

    # Parse all JIL files
    all_jil_jobs = parse_all_jil_files(jil_dir)
    if not all_jil_jobs:
        logger.error("No JIL jobs found in %s", jil_dir)

    # Parse all Stonebranch JSON files
    all_sb_tasks = parse_all_sb_files(sb_dir)
    if not all_sb_tasks:
        logger.warning("No Stonebranch tasks found in %s", sb_dir)

    # Case-insensitive lookup helper
    sb_lookup = {k.lower(): v for k, v in all_sb_tasks.items()}

    # Global check: circular dependencies in JIL
    global_issues = _detect_circular_dependencies(all_jil_jobs)

    # Per-job validation
    results: List[Dict] = []
    validated_sb_names: set = set()

    for job_name, jil_job in all_jil_jobs.items():
        # Lookup by name (case-insensitive)
        sb_task = sb_lookup.get(job_name.lower())
        if sb_task:
            validated_sb_names.add(sb_task["name"].lower())

        result = validate_job(jil_job, sb_task, mapping_rules, field_rules)
        results.append(result)

    # Sort: FAIL first → REVIEW → PASS, then alphabetically
    results.sort(key=lambda r: (STATUS_PRIORITY[r["status"]], r["job_name"]))

    # Find orphan UAC tasks (in Stonebranch but not in JIL)
    orphans = [
        {"uac_name": task["name"], "uac_type": task.get("type", ""), "source_file": task.get("_source_file", "")}
        for name, task in all_sb_tasks.items()
        if name.lower() not in {j.get("job_name", "").lower() for j in all_jil_jobs.values()}
    ]

    # Summary counts
    pass_count   = sum(1 for r in results if r["status"] == PASS)
    review_count = sum(1 for r in results if r["status"] == REVIEW)
    fail_count   = sum(1 for r in results if r["status"] == FAIL)

    # Top failure reasons
    failure_reason_counts: Dict[str, int] = {}
    for result in results:
        for issue in result.get("issues", []):
            if issue["severity"] == FAIL:
                key = issue["description"][:80]
                failure_reason_counts[key] = failure_reason_counts.get(key, 0) + 1
    top_failures = sorted(failure_reason_counts.items(), key=lambda x: -x[1])[:5]

    summary = {
        "timestamp":         datetime.now().isoformat(timespec="seconds"),
        "jil_dir":           jil_dir,
        "sb_dir":            sb_dir,
        "total_jil_jobs":    len(all_jil_jobs),
        "total_sb_tasks":    len(all_sb_tasks),
        "pass_count":        pass_count,
        "review_count":      review_count,
        "fail_count":        fail_count,
        "orphan_count":      len(orphans),
        "top_fail_reasons":  top_failures,
        "global_issues":     global_issues,
    }

    logger.info("Validation complete: %d PASS | %d REVIEW | %d FAIL | %d orphans",
                pass_count, review_count, fail_count, len(orphans))

    return {
        "summary": summary,
        "results": results,
        "orphans": orphans,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate Autosys JIL → Stonebranch UAC jobs")
    parser.add_argument("--jil-dir",    default="autosys",    help="Directory with .jil files")
    parser.add_argument("--sb-dir",     default="stonebranch", help="Directory with Stonebranch .json files")
    parser.add_argument("--config-dir", default="config",     help="Directory with mapping/field config YAML files")
    parser.add_argument("--output",     default="reports",    help="Directory to write validation_results.json")
    parser.add_argument("--verbose",    action="store_true",  help="Print each job result to console")
    args = parser.parse_args()

    validation_output = run_validation(
        jil_dir=args.jil_dir,
        sb_dir=args.sb_dir,
        config_dir=args.config_dir
    )

    # Write results JSON
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_file = output_dir / "validation_results.json"
    results_file.write_text(
        json.dumps(validation_output, indent=2, default=str),
        encoding="utf-8"
    )
    print(f"\nValidation results written to: {results_file}")

    # Console summary
    s = validation_output["summary"]
    print(f"\n{'='*50}")
    print(f"  Validation Summary")
    print(f"{'='*50}")
    print(f"  JIL jobs parsed:    {s['total_jil_jobs']}")
    print(f"  UAC tasks parsed:   {s['total_sb_tasks']}")
    print(f"  ✅ PASS:            {s['pass_count']}")
    print(f"  ⚠️  REVIEW:          {s['review_count']}")
    print(f"  ❌ FAIL:            {s['fail_count']}")
    print(f"  Orphan UAC tasks:   {s['orphan_count']}")

    if s["top_fail_reasons"]:
        print(f"\n  Top failure reasons:")
        for reason, count in s["top_fail_reasons"]:
            print(f"    [{count}x] {reason}")

    if args.verbose:
        print(f"\n{'='*50}")
        for result in validation_output["results"]:
            icon = {"PASS": "✅", "REVIEW": "⚠️ ", "FAIL": "❌"}.get(result["status"], "?")
            print(f"\n{icon} {result['job_name']} ({result['job_type']} → {result['uac_type']})")
            for issue in result.get("issues", []):
                sev_icon = {"FAIL": "  ❌", "REVIEW": "  ⚠️ "}.get(issue["severity"], "  ℹ️ ")
                print(f"{sev_icon} [{issue['field']}] {issue['description']}")
                if issue["jil_value"] or issue["uac_value"]:
                    print(f"       JIL: {issue['jil_value']}  |  UAC: {issue['uac_value']}")
