"""
convert_jil.py
--------------
Converts Autosys JIL jobs into Stonebranch UAC-compatible JSON task definitions.

Two modes:
  FILE MODE  — convert a .jil file on disk
  PASTE MODE — convert JIL text piped from stdin or saved from chat

Handles all 4 Autosys job types:
  CMD  → taskUnix / taskWindows
  BOX  → workflow  (with vertices + edges from member jobs)
  FW   → taskFileMonitor   (Agent File Monitor)
  FT   → taskFileMonitorRemote  (Remote File Monitor)

Full field mapping:
  command         → command
  machine         → agentVar
  owner           → credentials
  start_times     → attached trigger (Time or Cron)
  days_of_week    → trigger day restriction
  run_calendar    → calendar name on trigger
  condition       → workflow edges / successCheckCondition
  alarm_if_fail   → failureAction
  max_run_alarm   → lateFinishDuration
  must_start_times→ lateStart
  must_complete_t → lateFinish
  watch_file      → filename
  watch_interval  → scanInterval
  exitcode        → exitCodes
  description     → description
  std_out_file    → stdoutLogFile
  std_err_file    → stderrLogFile

Output:
  - One JSON file per BOX (workflow) containing the BOX + all its member tasks
  - One JSON file per standalone CMD / FW / FT job
  - Or single combined file with --output-mode combined
  - Or stdout with --stdout (for chat paste mode)

Usage:
  # Convert a file
  python scripts/convert_jil.py --input autosys/my_jobs.jil --output stonebranch/

  # Convert all JIL files in a directory
  python scripts/convert_jil.py --input autosys/ --output stonebranch/

  # Paste mode — read from stdin, print to stdout
  python scripts/convert_jil.py --stdin

  # Paste mode from a temp file Copilot saved
  python scripts/convert_jil.py --input /tmp/pasted_jil.jil --stdout

  # Single combined output file
  python scripts/convert_jil.py --input autosys/ --output stonebranch/ --output-mode combined
"""

import sys
import os
import re
import json
import logging
import argparse
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

# Add scripts/ to path so we can import parse_jil
sys.path.insert(0, str(Path(__file__).parent))
from parse_jil import parse_jil_file, parse_all_jil_files, _resolve_box_members

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversion warnings — collected per job, shown in output
# ---------------------------------------------------------------------------

class ConversionWarnings:
    """Collects conversion notes and warnings per job for transparency."""

    def __init__(self):
        self._warnings: Dict[str, List[str]] = {}

    def add(self, job_name: str, message: str):
        self._warnings.setdefault(job_name, []).append(message)

    def get(self, job_name: str) -> List[str]:
        return self._warnings.get(job_name, [])

    def all(self) -> Dict[str, List[str]]:
        return self._warnings


WARNINGS = ConversionWarnings()


# ---------------------------------------------------------------------------
# Schedule / Trigger conversion helpers
# ---------------------------------------------------------------------------

def _clean_quoted(value: str) -> str:
    """Strip surrounding quotes from JIL values like '\"02:00\"'."""
    return str(value).strip().strip('"').strip("'")


def _is_cron_expression(value: str) -> bool:
    """Detect if a start_times value looks like a cron expression vs a simple time."""
    cleaned = _clean_quoted(value)
    # Cron has 5 space-separated fields and typically contains * / , -
    parts = cleaned.split()
    if len(parts) == 5:
        return True
    if re.search(r"[*/,]", cleaned):
        return True
    return False


def _convert_autosys_time_to_cron(start_times: str, days_of_week: str = "") -> Dict:
    """
    Convert AutoSys start_times + days_of_week into a UAC trigger definition.

    Returns a trigger dict with type (triggerCron or triggerTime) and schedule fields.
    """
    cleaned = _clean_quoted(start_times)

    if _is_cron_expression(cleaned):
        return {
            "triggerType": "triggerCron",
            "cronExpression": cleaned,
        }

    # Simple time like "02:30" or "0230"
    time_match = re.match(r"(\d{1,2}):?(\d{2})", cleaned)
    if time_match:
        hours   = time_match.group(1).zfill(2)
        minutes = time_match.group(2)

        # Convert days_of_week
        day_map = {
            "all": "*", "weekdays": "1-5", "weekends": "0,6",
            "su": "0", "mo": "1", "tu": "2", "we": "3",
            "th": "4", "fr": "5", "sa": "6",
            "sun": "0", "mon": "1", "tue": "2", "wed": "3",
            "thu": "4", "fri": "5", "sat": "6",
        }
        dow_value = "*"
        if days_of_week:
            cleaned_dow = _clean_quoted(days_of_week).lower()
            # Handle comma-separated day lists
            if "," in cleaned_dow:
                parts = [day_map.get(d.strip(), d.strip()) for d in cleaned_dow.split(",")]
                dow_value = ",".join(parts)
            else:
                dow_value = day_map.get(cleaned_dow, "*")

        cron_expr = f"{minutes} {hours} * * {dow_value}"
        return {
            "triggerType": "triggerCron",
            "cronExpression": cron_expr,
            "_originalStartTimes": cleaned,
            "_originalDaysOfWeek": days_of_week,
        }

    return {
        "triggerType": "triggerTime",
        "rawValue": cleaned,
    }


def _build_trigger(job_name: str, job: Dict) -> Optional[Dict]:
    """
    Build a UAC trigger definition from JIL schedule fields.
    Returns None if no schedule defined.
    """
    start_times  = job.get("start_times", "")
    days_of_week = job.get("days_of_week", "")
    run_calendar = job.get("run_calendar", "")

    if not start_times:
        return None

    trigger = _convert_autosys_time_to_cron(
        str(start_times), str(days_of_week) if days_of_week else ""
    )

    trigger_def: Dict[str, Any] = {
        "type":   trigger["triggerType"],
        "name":   f"{job_name}_trigger",
        "tasks":  [job_name],
        "active": True,
    }

    if trigger["triggerType"] == "triggerCron":
        trigger_def["cronExpression"] = trigger["cronExpression"]
    elif trigger["triggerType"] == "triggerTime":
        raw = trigger.get("rawValue", "")
        time_match = re.match(r"(\d{1,2}):?(\d{2})", raw)
        if time_match:
            trigger_def["hours"]   = time_match.group(1)
            trigger_def["minutes"] = time_match.group(2)

    if run_calendar:
        trigger_def["calendar"] = _clean_quoted(str(run_calendar))

    if job.get("exclude_calendar"):
        WARNINGS.add(job_name,
            "⚠️ exclude_calendar has no direct UAC equivalent. "
            "Manually create a UAC Calendar excluding those dates and attach to the trigger."
        )

    return trigger_def


# ---------------------------------------------------------------------------
# Condition / dependency converter
# ---------------------------------------------------------------------------

CONDITION_STATUS_MAP = {
    "success":    "Success",
    "failure":    "Failure",
    "terminated": "Cancelled",
    "done":       "Success/Failure",      # approximate
    "notrunning": "Success/Failure",      # approximate — no direct UAC equivalent
    "exitcode":   "Exit Code",
}

APPROXIMATE_CONDITIONS = {"done", "notrunning"}


def _parse_single_condition(raw: str) -> Optional[Dict]:
    """
    Parse one AutoSys condition string like 'success(job_name)' or 'exitcode(job_name,"0")'.
    Returns a structured dict or None if unparseable.
    """
    raw = raw.strip()
    match = re.match(
        r"(?P<status>\w+)\((?P<job>[^,)]+)(?:,\s*\"?(?P<code>[^\")\s]+)\"?)?\)",
        raw, re.IGNORECASE
    )
    if not match:
        return None

    status   = match.group("status").lower()
    job_ref  = match.group("job").strip()
    exitcode = match.group("code")

    uac_condition = CONDITION_STATUS_MAP.get(status, status.capitalize())

    result = {
        "sourceTaskName": job_ref,
        "condition":      uac_condition,
        "_originalStatus": status,
    }
    if exitcode:
        result["exitCode"] = exitcode

    return result


def _convert_conditions(job_name: str, raw_conditions: Any) -> List[Dict]:
    """
    Convert all JIL condition strings to UAC dependency/edge dicts.
    Warns about approximate mappings.
    """
    if not raw_conditions:
        return []

    if isinstance(raw_conditions, str):
        raw_conditions = [raw_conditions]

    edges = []
    for raw in raw_conditions:
        # Split on AND/OR — each becomes a separate edge
        parts = re.split(r"\s+(?:AND|OR|&|\|)\s+", raw, flags=re.IGNORECASE)
        for part in parts:
            edge = _parse_single_condition(part.strip())
            if edge:
                orig = edge["_originalStatus"]
                if orig in APPROXIMATE_CONDITIONS:
                    WARNINGS.add(job_name,
                        f"⚠️ Condition '{orig}({edge['sourceTaskName']})' has no direct UAC equivalent. "
                        f"Mapped to 'Success/Failure' — verify this is the intended behaviour."
                    )
                edges.append(edge)
            elif part.strip():
                WARNINGS.add(job_name,
                    f"⚠️ Could not parse condition '{part.strip()}' — added as a comment. "
                    "Review manually."
                )
    return edges


# ---------------------------------------------------------------------------
# Alarm / action converter
# ---------------------------------------------------------------------------

def _build_actions(job_name: str, job: Dict) -> List[Dict]:
    """
    Convert AutoSys alarm fields into UAC task actions.
    """
    actions = []

    alarm_if_fail       = job.get("alarm_if_fail")
    alarm_if_terminated = job.get("alarm_if_terminated")
    max_run_alarm       = job.get("max_run_alarm")
    min_run_alarm       = job.get("min_run_alarm")
    must_start_times    = job.get("must_start_times")
    must_complete_times = job.get("must_complete_times")

    def _is_truthy(val):
        if val is None: return False
        if isinstance(val, bool): return val
        if isinstance(val, list): return any(_is_truthy(v) for v in val)
        return str(val).strip() in ("1", "yes", "true", "y")

    if _is_truthy(alarm_if_fail):
        actions.append({
            "type":    "emailNotification",
            "trigger": "onFailure",
            "_note":   "Converted from alarm_if_fail. Configure email recipients in UAC."
        })

    if _is_truthy(alarm_if_terminated):
        actions.append({
            "type":    "emailNotification",
            "trigger": "onCancelled",
            "_note":   "Converted from alarm_if_terminated. Configure email recipients in UAC."
        })

    if max_run_alarm:
        actions.append({
            "type":             "lateFinish",
            "lateFinishEnabled": True,
            "lateFinishDuration": int(max_run_alarm),
            "_note":            f"Converted from max_run_alarm={max_run_alarm} seconds."
        })

    if min_run_alarm:
        actions.append({
            "type":              "earlyFinish",
            "earlyFinishEnabled": True,
            "earlyFinishDuration": int(min_run_alarm),
            "_note":             f"Converted from min_run_alarm={min_run_alarm} seconds."
        })

    if must_start_times:
        cleaned = _clean_quoted(str(must_start_times))
        actions.append({
            "type":            "lateStart",
            "lateStartEnabled": True,
            "lateStartTime":   cleaned,
            "_note":           f"Converted from must_start_times={cleaned}. Business SLA — verify."
        })

    if must_complete_times:
        cleaned = _clean_quoted(str(must_complete_times))
        actions.append({
            "type":             "lateFinishTime",
            "lateFinishEnabled": True,
            "lateFinishTime":   cleaned,
            "_note":            f"Converted from must_complete_times={cleaned}. Business SLA — verify."
        })

    return actions


# ---------------------------------------------------------------------------
# Per-type task converters
# ---------------------------------------------------------------------------

def _convert_cmd(job_name: str, job: Dict) -> Dict:
    """Convert CMD job → taskUnix or taskWindows."""
    detected_os = job.get("detected_os", "linux")
    task_type   = "taskWindows" if detected_os == "windows" else "taskUnix"

    command = _clean_quoted(job.get("command", ""))
    machine = job.get("machine", "")
    owner   = job.get("owner", "")

    task: Dict[str, Any] = {
        "type":        task_type,
        "name":        job_name,
        "description": _clean_quoted(job.get("description", "")),
        "agentVar":    machine,
        "command":     command,
    }

    if owner:
        task["credentials"] = owner

    stdout = job.get("std_out_file", "")
    stderr = job.get("std_err_file", "")
    if stdout:
        task["stdoutLogFile"] = _clean_quoted(str(stdout))
    if stderr:
        task["stderrLogFile"] = _clean_quoted(str(stderr))

    # Retry
    n_retrys       = job.get("n_retrys")
    retry_interval = job.get("retry_interval")
    if n_retrys:
        task["retryMaximum"]  = int(n_retrys)
        task["retryInterval"] = int(retry_interval) if retry_interval else 60

    # Exit codes
    max_exit = job.get("max_exit_success")
    if max_exit is not None:
        task["successExitCodes"] = str(max_exit)

    # Actions (alarms)
    actions = _build_actions(job_name, job)
    if actions:
        task["actions"] = actions

    # Conditions (for standalone CMD jobs — not inside BOX)
    raw_conds = job.get("condition", [])
    edges = _convert_conditions(job_name, raw_conds)
    if edges and not job.get("box_name"):
        task["_standaloneConditions"] = edges
        WARNINGS.add(job_name,
            "ℹ️ This CMD job has conditions but is not inside a BOX. "
            "In UAC, conditions between standalone tasks are handled via a Workflow. "
            "Consider wrapping these in a workflow or use a Task Monitor trigger."
        )

    if not machine:
        WARNINGS.add(job_name, "⚠️ No 'machine' field found — agentVar is empty. Set the agent in UAC.")
    if not command:
        WARNINGS.add(job_name, "⚠️ No 'command' field found — command is empty. Set the command in UAC.")

    return task


def _convert_fw(job_name: str, job: Dict) -> Dict:
    """Convert FW (File Watcher) → taskFileMonitor (Agent File Monitor)."""
    watch_file    = _clean_quoted(job.get("watch_file", ""))
    machine       = job.get("machine", "")
    watch_interval = job.get("watch_interval")
    min_size      = job.get("watch_minimum_size")

    task: Dict[str, Any] = {
        "type":        "taskFileMonitor",
        "name":        job_name,
        "description": _clean_quoted(job.get("description", "")),
        "agent":       machine,
        "filename":    watch_file,
        "monitorType": "fileExists",   # default — most common FW use case
    }

    if watch_interval:
        task["scanInterval"] = int(watch_interval)
    if min_size:
        task["minimumFileSize"] = int(min_size)

    actions = _build_actions(job_name, job)
    if actions:
        task["actions"] = actions

    if not watch_file:
        WARNINGS.add(job_name, "⚠️ No 'watch_file' field found — filename is empty. Set the file path in UAC.")
    if not machine:
        WARNINGS.add(job_name, "⚠️ No 'machine' field found — agent is empty. Set the agent in UAC.")

    return task


def _convert_ft(job_name: str, job: Dict) -> Dict:
    """Convert FT (File Trigger/FTP) → taskFileMonitorRemote (Remote File Monitor)."""
    watch_file     = _clean_quoted(job.get("watch_file", ""))
    watch_interval = job.get("watch_interval")

    task: Dict[str, Any] = {
        "type":        "taskFileMonitorRemote",
        "name":        job_name,
        "description": _clean_quoted(job.get("description", "")),
        "filename":    watch_file,
    }

    if watch_interval:
        task["scanInterval"] = int(watch_interval)

    actions = _build_actions(job_name, job)
    if actions:
        task["actions"] = actions

    WARNINGS.add(job_name,
        "ℹ️ FT job converted to taskFileMonitorRemote. "
        "You must configure the FTP connection / credentials in the UAC task. "
        "This cannot be auto-converted from JIL."
    )

    if not watch_file:
        WARNINGS.add(job_name, "⚠️ No 'watch_file' field found — filename is empty. Set the file path in UAC.")

    return task


def _convert_box(box_name: str, box_job: Dict, all_jobs: Dict) -> Dict:
    """
    Convert BOX job → UAC Workflow with vertices (tasks) and edges (dependencies).
    Also converts all member jobs embedded inside.
    """
    members = box_job.get("members", [])
    vertices = []
    edges    = []

    # Build vertices from all member jobs
    for member_name in members:
        member_job = all_jobs.get(member_name)
        if member_job:
            vertices.append({"task": member_name})
        else:
            WARNINGS.add(box_name,
                f"⚠️ BOX member '{member_name}' not found in parsed jobs. "
                "It may be defined in a different JIL file — add it manually as a workflow vertex."
            )
            vertices.append({"task": member_name, "_missing": True})

    # Build edges from conditions on member jobs
    for member_name in members:
        member_job = all_jobs.get(member_name)
        if not member_job:
            continue
        raw_conds = member_job.get("condition", [])
        member_edges = _convert_conditions(member_name, raw_conds)
        for edge in member_edges:
            edges.append({
                "sourceTaskName": edge["sourceTaskName"],
                "targetTaskName": member_name,
                "condition":      edge["condition"],
            })

    workflow: Dict[str, Any] = {
        "type":        "workflow",
        "name":        box_name,
        "description": _clean_quoted(box_job.get("description", "")),
        "vertex":      vertices,
    }

    if edges:
        workflow["edge"] = edges

    # BOX-level actions (alarms on the BOX itself)
    actions = _build_actions(box_name, box_job)
    if actions:
        workflow["actions"] = actions

    if not members:
        WARNINGS.add(box_name,
            "⚠️ No member jobs found for this BOX. "
            "Member jobs may be in a different JIL file — add vertices manually."
        )

    return workflow


# ---------------------------------------------------------------------------
# Trigger builder (attached alongside task)
# ---------------------------------------------------------------------------

def _maybe_build_trigger(job_name: str, job: Dict) -> Optional[Dict]:
    """Build trigger if the job has a schedule defined."""
    return _build_trigger(job_name, job)


# ---------------------------------------------------------------------------
# Main conversion orchestrator
# ---------------------------------------------------------------------------

def convert_jobs(all_jobs: Dict[str, Dict]) -> Dict[str, Any]:
    """
    Convert all parsed JIL jobs into Stonebranch UAC task definitions.

    Returns a dict:
      {
        "tasks":    { job_name: task_dict, ... },
        "triggers": { job_name: trigger_dict, ... },
        "warnings": { job_name: [warning, ...], ... },
        "summary":  { ... }
      }
    """
    tasks:    Dict[str, Dict] = {}
    triggers: Dict[str, Dict] = {}

    # Track which jobs are BOX members — we handle them inside BOX conversion
    box_members: set = set()
    for job in all_jobs.values():
        box_name = job.get("box_name", "")
        if box_name and box_name != job.get("job_name"):
            box_members.add(job.get("job_name", ""))

    for job_name, job in all_jobs.items():
        job_type = job.get("job_type", "").upper()

        if job_type == "BOX":
            tasks[job_name] = _convert_box(job_name, job, all_jobs)

            # Also convert each member job as a standalone task definition
            for member_name in job.get("members", []):
                member_job = all_jobs.get(member_name)
                if member_job:
                    member_type = member_job.get("job_type", "").upper()
                    if member_type == "CMD":
                        tasks[member_name] = _convert_cmd(member_name, member_job)
                    elif member_type == "FW":
                        tasks[member_name] = _convert_fw(member_name, member_job)
                    elif member_type == "FT":
                        tasks[member_name] = _convert_ft(member_name, member_job)

        elif job_type == "CMD" and job_name not in box_members:
            tasks[job_name] = _convert_cmd(job_name, job)

        elif job_type == "FW" and job_name not in box_members:
            tasks[job_name] = _convert_fw(job_name, job)

        elif job_type == "FT" and job_name not in box_members:
            tasks[job_name] = _convert_ft(job_name, job)

        # Build trigger for any job that has a schedule
        trigger = _maybe_build_trigger(job_name, job)
        if trigger:
            triggers[job_name] = trigger

    # Summary
    type_counts: Dict[str, int] = {}
    for t in tasks.values():
        tt = t.get("type", "unknown")
        type_counts[tt] = type_counts.get(tt, 0) + 1

    summary = {
        "converted_at":   datetime.now().isoformat(timespec="seconds"),
        "total_tasks":    len(tasks),
        "total_triggers": len(triggers),
        "by_type":        type_counts,
        "total_warnings": sum(len(w) for w in WARNINGS.all().values()),
    }

    return {
        "tasks":    tasks,
        "triggers": triggers,
        "warnings": WARNINGS.all(),
        "summary":  summary,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _task_to_output_list(result: Dict) -> List[Dict]:
    """
    Flatten tasks + triggers into a single list ready for JSON output.
    Triggers are embedded inside their task definition for readability.
    """
    output = []
    tasks    = result["tasks"]
    triggers = result["triggers"]
    warnings = result["warnings"]

    for task_name, task in tasks.items():
        entry = dict(task)

        # Embed trigger if exists
        if task_name in triggers:
            entry["_trigger"] = triggers[task_name]

        # Embed conversion warnings as comments
        job_warnings = warnings.get(task_name, [])
        if job_warnings:
            entry["_conversionNotes"] = job_warnings

        # Remove internal parse keys
        entry.pop("_standaloneConditions", None)

        output.append(entry)

    return output


def write_outputs(result: Dict, output_dir: str, output_mode: str = "per_box"):
    """
    Write converted tasks to JSON files.

    output_mode:
      per_box   — one file per BOX (workflow + all members), one file per standalone job
      combined  — one single file with all tasks
      per_job   — one file per task
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    tasks    = result["tasks"]
    triggers = result["triggers"]
    warnings = result["warnings"]

    if output_mode == "combined":
        output_list = _task_to_output_list(result)
        out_file = out_path / "converted_all_jobs.json"
        out_file.write_text(
            json.dumps(output_list, indent=2, default=str), encoding="utf-8"
        )
        logger.info("Written: %s (%d tasks)", out_file, len(output_list))
        return [str(out_file)]

    # per_box or per_job — group BOX + members together
    written_files = []
    written_tasks: set = set()

    # First pass: write BOX workflows with their members
    for task_name, task in tasks.items():
        if task.get("type") != "workflow":
            continue

        group = [task]
        if task_name in triggers:
            task["_trigger"] = triggers[task_name]
        if task_name in warnings:
            task["_conversionNotes"] = warnings[task_name]

        # Include member tasks in the same file
        for vertex in task.get("vertex", []):
            member_name = vertex.get("task", "")
            if member_name and member_name in tasks:
                member = dict(tasks[member_name])
                if member_name in warnings:
                    member["_conversionNotes"] = warnings[member_name]
                group.append(member)
                written_tasks.add(member_name)

        written_tasks.add(task_name)
        safe_name = re.sub(r"[^\w\-]", "_", task_name)
        out_file = out_path / f"{safe_name}.json"
        out_file.write_text(
            json.dumps(group, indent=2, default=str), encoding="utf-8"
        )
        logger.info("Written: %s (%d tasks)", out_file, len(group))
        written_files.append(str(out_file))

    # Second pass: write standalone tasks not inside any BOX
    for task_name, task in tasks.items():
        if task_name in written_tasks:
            continue

        entry = dict(task)
        if task_name in triggers:
            entry["_trigger"] = triggers[task_name]
        if task_name in warnings:
            entry["_conversionNotes"] = warnings[task_name]

        safe_name = re.sub(r"[^\w\-]", "_", task_name)
        out_file = out_path / f"{safe_name}.json"
        out_file.write_text(
            json.dumps([entry], indent=2, default=str), encoding="utf-8"
        )
        logger.info("Written: %s", out_file)
        written_files.append(str(out_file))

    return written_files


def print_stdout(result: Dict):
    """Print converted tasks to stdout (for paste mode / chat use)."""
    output_list = _task_to_output_list(result)
    summary = result["summary"]

    print("\n" + "=" * 60)
    print("  Stonebranch UAC Conversion Result")
    print("=" * 60)
    print(f"  Tasks converted:  {summary['total_tasks']}")
    print(f"  Triggers created: {summary['total_triggers']}")
    print(f"  Warnings:         {summary['total_warnings']}")
    print(f"  Types: " + ", ".join(f"{k}: {v}" for k, v in summary["by_type"].items()))
    print("=" * 60)

    if result["warnings"]:
        print("\n⚠️  CONVERSION NOTES (review before importing to UAC):\n")
        for job_name, warns in result["warnings"].items():
            for w in warns:
                print(f"  [{job_name}] {w}")

    print("\n📄  CONVERTED JSON:\n")
    print(json.dumps(output_list, indent=2, default=str))


# ---------------------------------------------------------------------------
# Paste-mode handler
# ---------------------------------------------------------------------------

def convert_from_text(jil_text: str) -> Dict:
    """
    Convert JIL text directly (for paste mode).
    Writes to a temp file, parses it, then converts.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jil", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(jil_text)
        tmp_path = tmp.name

    try:
        jobs = parse_jil_file(tmp_path)
        jobs = _resolve_box_members(jobs)
        return convert_jobs(jobs)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert Autosys JIL jobs to Stonebranch UAC JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert a single JIL file
  python scripts/convert_jil.py --input autosys/my_jobs.jil --output stonebranch/

  # Convert all JIL files in a directory
  python scripts/convert_jil.py --input autosys/ --output stonebranch/

  # Convert and print to terminal (paste mode from file)
  python scripts/convert_jil.py --input autosys/my_jobs.jil --stdout

  # Read pasted JIL from stdin and print to terminal
  cat my_jobs.jil | python scripts/convert_jil.py --stdin

  # Single combined output file
  python scripts/convert_jil.py --input autosys/ --output stonebranch/ --output-mode combined
        """
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input",  "-i", help="JIL file or directory of JIL files")
    source.add_argument("--stdin",        action="store_true",
                        help="Read JIL from stdin (paste mode)")

    parser.add_argument("--output", "-o", default=None,
                        help="Output directory for JSON files (not needed with --stdout)")
    parser.add_argument("--stdout",       action="store_true",
                        help="Print converted JSON to terminal instead of writing files")
    parser.add_argument("--output-mode",  default="per_box",
                        choices=["per_box", "combined", "per_job"],
                        help="per_box (default): one file per BOX + members | "
                             "combined: all in one file | per_job: one file per task")

    args = parser.parse_args()

    # --- Load jobs ---
    if args.stdin:
        logger.info("Reading JIL from stdin...")
        jil_text = sys.stdin.read()
        if not jil_text.strip():
            print("❌ No JIL content received on stdin.")
            sys.exit(1)
        result = convert_from_text(jil_text)

    else:
        input_path = Path(args.input)
        if input_path.is_dir():
            all_jobs = parse_all_jil_files(str(input_path))
        elif input_path.is_file():
            all_jobs = parse_jil_file(str(input_path))
            all_jobs = _resolve_box_members(all_jobs)
        else:
            print(f"❌ Input not found: {args.input}")
            sys.exit(1)

        if not all_jobs:
            print("❌ No jobs parsed. Check your JIL file.")
            sys.exit(1)

        result = convert_jobs(all_jobs)

    # --- Output ---
    if args.stdout or (not args.output):
        print_stdout(result)
    else:
        written = write_outputs(result, args.output, args.output_mode)
        s = result["summary"]
        print(f"\n✅ Conversion complete.")
        print(f"   Tasks converted:  {s['total_tasks']}")
        print(f"   Triggers created: {s['total_triggers']}")
        print(f"   Warnings:         {s['total_warnings']}")
        print(f"   Files written:    {len(written)}")
        for f in written:
            print(f"     → {f}")
        if s["total_warnings"] > 0:
            print(f"\n⚠️  {s['total_warnings']} conversion note(s) embedded in _conversionNotes fields.")
            print("   Review these in the output JSON before importing to UAC.")
