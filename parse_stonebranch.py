"""
parse_stonebranch.py
--------------------
Parses Stonebranch UAC JSON files into structured Python dicts.

Handles:
  - Single-task JSON files  { "task": { ... } }
  - Array JSON files        [ { "task": { ... } }, ... ]
  - Nested task objects with type-specific fields
  - Workflow vertex/edge extraction
  - Trigger extraction (if triggers are embedded in export)
  - Multiple .json files in the /stonebranch/ directory

Usage:
  from parse_stonebranch import parse_all_sb_files
  tasks = parse_all_sb_files("stonebranch/")

  # Or parse a single file:
  from parse_stonebranch import parse_sb_file
  tasks = parse_sb_file("stonebranch/all_jobs.json")
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — Stonebranch task type identifiers
# ---------------------------------------------------------------------------

TASK_TYPE_FIELD = "type"

# Known UAC task types (from official documentation)
KNOWN_TASK_TYPES = {
    "taskUnix",
    "taskWindows",
    "workflow",
    "taskFileMonitor",
    "taskFileMonitorRemote",
    "taskMonitor",
    "taskTimer",
    "taskSql",
    "taskEmail",
    "taskManual",
    "taskApproval",
    "taskWebService",
    "taskSap",
    "taskPeopleSoft",
    "taskFileTransfer",
    "taskStoredProcedure",
    "taskRecurring",
    "taskSystemMonitor",
    "taskVariableMonitor",
    "taskEmailMonitor",
    "taskUniversalMonitor",
    "taskZosMonitor",
    "taskApplicationControl",
    "taskUniversal",
    "taskIbmi",
    "taskZos",
    "taskUniversalCommand",
}

# Fields that might hold the task name across different export formats
NAME_CANDIDATES = ["name", "taskName", "task_name", "sysId", "label"]

# ---------------------------------------------------------------------------
# Name resolution helper
# ---------------------------------------------------------------------------

def _resolve_name(task_obj: Dict) -> Optional[str]:
    """
    Try to extract the canonical task name from a task object.
    UAC exports can use 'name', 'taskName', or other variants.
    """
    for candidate in NAME_CANDIDATES:
        val = task_obj.get(candidate)
        if val and isinstance(val, str):
            return val.strip()
    return None


# ---------------------------------------------------------------------------
# Workflow vertex/edge extraction
# ---------------------------------------------------------------------------

def _extract_workflow_structure(task_obj: Dict) -> Dict:
    """
    For workflow type tasks, extract:
      - vertices: list of task names in the workflow
      - edges: list of dependency pairs { from, to, condition }
    """
    vertices = []
    edges = []

    # UAC workflow exports typically have a 'vertex' list under different keys
    for vertex_key in ("vertex", "vertices", "workflowVertices", "tasks"):
        raw_vertices = task_obj.get(vertex_key, [])
        if raw_vertices and isinstance(raw_vertices, list):
            for v in raw_vertices:
                if isinstance(v, dict):
                    # Vertex can have task name under different fields
                    task_ref = (
                        v.get("task") or v.get("taskName") or
                        v.get("name") or v.get("label") or ""
                    )
                    if task_ref:
                        vertices.append(task_ref.strip())
                elif isinstance(v, str):
                    vertices.append(v.strip())
            break

    # UAC edge / dependency extraction
    for edge_key in ("edge", "edges", "workflowEdges", "dependencies"):
        raw_edges = task_obj.get(edge_key, [])
        if raw_edges and isinstance(raw_edges, list):
            for e in raw_edges:
                if isinstance(e, dict):
                    edges.append({
                        "from": e.get("sourceTaskName") or e.get("from") or e.get("source", ""),
                        "to": e.get("targetTaskName") or e.get("to") or e.get("target", ""),
                        "condition": e.get("condition") or e.get("successFailureCriteria", ""),
                    })
            break

    return {"workflow_vertices": vertices, "workflow_edges": edges}


# ---------------------------------------------------------------------------
# Trigger extraction
# ---------------------------------------------------------------------------

def _extract_triggers(task_obj: Dict) -> List[Dict]:
    """
    Extract trigger information if embedded in the task export.
    Some UAC exports include triggers inline; others export them separately.
    """
    triggers = []

    for trigger_key in ("trigger", "triggers", "associatedTriggers"):
        raw = task_obj.get(trigger_key, [])
        if raw:
            if isinstance(raw, dict):
                raw = [raw]
            for t in raw:
                if isinstance(t, dict):
                    triggers.append({
                        "trigger_name": t.get("name") or t.get("triggerName", ""),
                        "trigger_type": t.get("type") or t.get("triggerType", ""),
                        "cron_expression": t.get("cronExpression") or t.get("minutes") or "",
                        "active": t.get("active", True),
                        "_raw": t,
                    })
            break

    return triggers


# ---------------------------------------------------------------------------
# Single task normaliser
# ---------------------------------------------------------------------------

def _normalise_task(task_obj: Dict, source_file: str) -> Optional[Dict]:
    """
    Normalise a raw task dict from a UAC JSON export into a consistent shape.

    Returns None if the task cannot be identified.
    """
    if not task_obj or not isinstance(task_obj, dict):
        return None

    # UAC sometimes wraps the actual task under a key like "task" or "workflowTask"
    for wrapper_key in ("task", "workflowTask", "taskDefinition"):
        if wrapper_key in task_obj and isinstance(task_obj[wrapper_key], dict):
            # Merge wrapper fields with top-level (top-level wins)
            merged = {**task_obj[wrapper_key], **{
                k: v for k, v in task_obj.items() if k != wrapper_key
            }}
            task_obj = merged
            break

    name = _resolve_name(task_obj)
    if not name:
        logger.warning("Could not determine task name from object in %s: %s",
                       source_file, str(task_obj)[:120])
        return None

    task_type = task_obj.get(TASK_TYPE_FIELD, "").strip()
    if task_type and task_type not in KNOWN_TASK_TYPES:
        logger.warning("Unknown UAC task type '%s' for task '%s'", task_type, name)

    normalised: Dict[str, Any] = {
        "name": name,
        "type": task_type,
        "_source_file": source_file,
        "_raw": task_obj,   # Keep original for deep inspection
    }

    # --- Universal fields ---
    for field in (
        "description", "agentVar", "agent", "credentials",
        "command", "commandOrScript", "script",
        "stdoutLogFile", "stderrLogFile",
        "lateFinishDuration", "earlyFinishDuration",
        "lateStart", "lateFinish", "earlyStart", "earlyFinish",
        "retryOptions", "failureAction", "successAction",
        "variables", "variable",
        "active", "enabled",
    ):
        val = task_obj.get(field)
        if val is not None:
            normalised[field] = val

    # Aliases — some exports use different casing/naming
    if "command" not in normalised:
        normalised["command"] = task_obj.get("cmd") or task_obj.get("commandLine") or ""

    if "agentVar" not in normalised:
        normalised["agentVar"] = task_obj.get("agent") or task_obj.get("agentName") or ""

    # --- File Monitor specific fields ---
    if task_type in ("taskFileMonitor", "taskFileMonitorRemote"):
        normalised["filename"] = (
            task_obj.get("filename") or
            task_obj.get("watchFile") or
            task_obj.get("filePattern") or ""
        )
        normalised["scanInterval"] = task_obj.get("scanInterval") or task_obj.get("pollInterval")
        normalised["minimumFileSize"] = task_obj.get("minimumFileSize") or task_obj.get("minFileSize")
        normalised["monitorType"] = task_obj.get("monitorType") or ""

    # --- Workflow specific fields ---
    if task_type == "workflow":
        workflow_data = _extract_workflow_structure(task_obj)
        normalised.update(workflow_data)

    # --- Task Monitor ---
    if task_type == "taskMonitor":
        normalised["monitorTask"] = (
            task_obj.get("monitorTask") or
            task_obj.get("watchJob") or
            task_obj.get("monitoredTask") or ""
        )
        normalised["monitorStatus"] = task_obj.get("monitorStatus") or ""

    # --- Triggers ---
    normalised["triggers"] = _extract_triggers(task_obj)

    # --- Actions / Notifications ---
    for action_key in ("actions", "taskActions", "notifications"):
        raw_actions = task_obj.get(action_key, [])
        if raw_actions:
            normalised["actions"] = raw_actions
            break

    return normalised


# ---------------------------------------------------------------------------
# File-level parser
# ---------------------------------------------------------------------------

def parse_sb_file(filepath: str) -> Dict[str, Dict]:
    """
    Parse a single Stonebranch JSON export file.

    Handles:
      - Top-level dict (single task)
      - Top-level list (array of tasks)
      - Wrapped exports { "results": [...] } or { "tasks": [...] }

    Returns: { task_name: normalised_task_dict, ... }
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Stonebranch JSON file not found: {filepath}")

    logger.info("Parsing Stonebranch file: %s", filepath)

    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        logger.error("JSON parse error in %s: %s", filepath, exc)
        return {}

    # Normalise to a flat list of task objects
    task_list: List[Dict] = []

    if isinstance(raw, list):
        task_list = raw

    elif isinstance(raw, dict):
        # Check common wrapper keys used in bulk UAC exports
        for wrapper in ("results", "tasks", "task", "items", "records"):
            if wrapper in raw and isinstance(raw[wrapper], list):
                task_list = raw[wrapper]
                break
        else:
            # Single task object
            task_list = [raw]

    tasks: Dict[str, Dict] = {}
    for obj in task_list:
        normalised = _normalise_task(obj, path.name)
        if normalised:
            name = normalised["name"]
            if name in tasks:
                logger.warning(
                    "Duplicate task name '%s' in %s. Using latest.",
                    name, path.name
                )
            tasks[name] = normalised

    logger.info("  → Parsed %d tasks from %s", len(tasks), path.name)
    return tasks


# ---------------------------------------------------------------------------
# Directory-level parser
# ---------------------------------------------------------------------------

def parse_all_sb_files(directory: str) -> Dict[str, Dict]:
    """
    Parse all .json files in a directory (non-recursive).

    Returns a merged dict: { task_name: normalised_task_dict, ... }
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"Stonebranch directory not found: {directory}")

    json_files = sorted(dir_path.glob("*.json"))
    if not json_files:
        logger.warning("No .json files found in %s", directory)
        return {}

    all_tasks: Dict[str, Dict] = {}
    for json_file in json_files:
        file_tasks = parse_sb_file(str(json_file))
        for name, task in file_tasks.items():
            if name in all_tasks:
                logger.warning(
                    "Duplicate task name '%s' found in '%s' (already seen in '%s'). "
                    "Using latest version.",
                    name, json_file.name, all_tasks[name].get("_source_file")
                )
            all_tasks[name] = task

    logger.info("Total Stonebranch tasks parsed: %d", len(all_tasks))
    return all_tasks


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def summarise(tasks: Dict[str, Dict]) -> Dict:
    """Return a summary dict of task counts by type."""
    summary = {"total": len(tasks), "by_type": {}}
    for task in tasks.values():
        ttype = task.get("type", "UNKNOWN")
        summary["by_type"][ttype] = summary["by_type"].get(ttype, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Parse Stonebranch UAC JSON files")
    parser.add_argument(
        "path",
        help="Path to a .json file or a directory containing .json files"
    )
    parser.add_argument(
        "--output", "-o",
        help="Write parsed tasks to a JSON file",
        default=None
    )
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        tasks = parse_all_sb_files(str(target))
    elif target.is_file():
        tasks = parse_sb_file(str(target))
    else:
        print(f"ERROR: Path not found: {target}")
        sys.exit(1)

    summary = summarise(tasks)
    print(f"\nParsed {summary['total']} tasks:")
    for ttype, count in sorted(summary["by_type"].items()):
        print(f"  {ttype}: {count}")

    if args.output:
        out_path = Path(args.output)
        # Strip _raw before writing to keep output clean
        clean_tasks = {
            name: {k: v for k, v in t.items() if k != "_raw"}
            for name, t in tasks.items()
        }
        out_path.write_text(
            json.dumps(clean_tasks, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nOutput written to: {args.output}")
    else:
        print("\nSample (first 2 tasks):")
        sample = dict(list(tasks.items())[:2])
        clean_sample = {
            name: {k: v for k, v in t.items() if k != "_raw"}
            for name, t in sample.items()
        }
        print(json.dumps(clean_sample, indent=2, default=str))
