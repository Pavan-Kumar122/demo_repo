"""
parse_jil.py
------------
Parses Autosys JIL files into structured Python dicts.

Handles:
  - Single-job JIL files (CMD only)
  - Multi-job JIL files (BOX + CMD + FW + FT mixed)
  - insert_job and update_job blocks
  - All 4 Autosys job types: CMD, BOX, FW, FT
  - Inline and block comments
  - Global variable references
  - Condition / dependency parsing
  - Multiple JIL files in the /autosys/ directory

Usage:
  from parse_jil import parse_all_jil_files
  jobs = parse_all_jil_files("autosys/")

  # Or parse a single file:
  from parse_jil import parse_jil_file
  jobs = parse_jil_file("autosys/all_jobs.jil")
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_JOB_TYPES = {"CMD", "BOX", "FW", "FT"}

# Fields that can appear multiple times (e.g., condition lines) — stored as lists
MULTI_VALUE_FIELDS = {"condition", "alarm_if_fail", "alarm_if_terminated"}

# Fields that are purely numeric
NUMERIC_FIELDS = {
    "watch_interval", "watch_minimum_size", "max_run_alarm", "min_run_alarm",
    "n_retrys", "retry_interval", "max_exit_success"
}

# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Remove /* ... */ block comments and /* ... */ inline comments from JIL text."""
    # Remove block comments spanning multiple lines
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove single-line /* ... */ comments on a line
    text = re.sub(r"/\*[^\n]*", "", text)
    return text


# ---------------------------------------------------------------------------
# Single job block parser
# ---------------------------------------------------------------------------

def _parse_job_block(block: str) -> Optional[Dict]:
    """
    Parse one JIL job block (everything between insert_job/update_job header
    and the next header or end of file) into a dict.

    Returns None if the block cannot be parsed.
    """
    lines = block.strip().splitlines()
    if not lines:
        return None

    job = {}
    current_key = None
    continuation_value = []

    for raw_line in lines:
        line = raw_line.strip()

        # Skip empty lines
        if not line:
            current_key = None
            continue

        # Key: value  — standard JIL attribute line
        if ":" in line:
            # Flush any ongoing continuation
            if current_key and continuation_value:
                job[current_key] = " ".join(continuation_value).strip()
                continuation_value = []
                current_key = None

            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()

            # Handle multi-value fields (e.g., condition can appear multiple times)
            if key in MULTI_VALUE_FIELDS:
                if key not in job:
                    job[key] = []
                if value:
                    job[key].append(value)
            else:
                # Numeric coercion
                if key in NUMERIC_FIELDS:
                    try:
                        value = int(value)
                    except ValueError:
                        try:
                            value = float(value)
                        except ValueError:
                            pass
                job[key] = value
                current_key = key if value == "" else None

        elif current_key:
            # Continuation line (value spanned across lines)
            continuation_value.append(line)

    # Flush final continuation
    if current_key and continuation_value:
        job[current_key] = " ".join(continuation_value).strip()

    return job if job else None


# ---------------------------------------------------------------------------
# BOX member resolution
# ---------------------------------------------------------------------------

def _resolve_box_members(all_jobs: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    For each BOX job, find all CMD/FW/FT jobs that have box_name pointing to it
    and add them as a 'members' list on the BOX job dict.
    """
    # Build reverse map: box_name → [member job names]
    box_members: Dict[str, List[str]] = {}
    for job_name, job in all_jobs.items():
        box_name = job.get("box_name", "").strip()
        if box_name and box_name != job_name:
            box_members.setdefault(box_name, []).append(job_name)

    for box_name, members in box_members.items():
        if box_name in all_jobs:
            all_jobs[box_name]["members"] = sorted(members)
        else:
            logger.warning(
                "box_name '%s' referenced by members %s but no BOX job found",
                box_name, members
            )

    return all_jobs


# ---------------------------------------------------------------------------
# Condition parser
# ---------------------------------------------------------------------------

def _parse_conditions(raw_conditions: List[str]) -> List[Dict]:
    """
    Parse raw condition strings into structured dicts.

    AutoSys condition formats:
      success(job_name)
      failure(job_name)
      notrunning(job_name)
      done(job_name)
      terminated(job_name)
      exitcode(job_name, "0")
      v(variable_name) = "value"        (variable check)
    """
    parsed = []
    # Pattern: status(job_name) or status(job_name, "exitcode")
    cond_pattern = re.compile(
        r"(?P<status>\w+)\((?P<job>[^,)]+)(?:,\s*\"?(?P<exitcode>[^\")\s]+)\"?)?\)",
        re.IGNORECASE
    )
    # Variable pattern: v(var_name) = "value" or v(var_name) = value
    var_pattern = re.compile(
        r"v\((?P<var>[^)]+)\)\s*=\s*\"?(?P<value>[^\"]+)\"?",
        re.IGNORECASE
    )

    for raw in raw_conditions:
        # Handle AND / OR logical operators — split and parse individually
        parts = re.split(r"\s+(?:AND|OR|&|\|)\s+", raw, flags=re.IGNORECASE)
        for part in parts:
            part = part.strip()
            var_match = var_pattern.search(part)
            if var_match:
                parsed.append({
                    "type": "variable",
                    "variable": var_match.group("var").strip(),
                    "value": var_match.group("value").strip(),
                    "raw": part
                })
                continue

            cond_match = cond_pattern.search(part)
            if cond_match:
                parsed.append({
                    "type": "job_status",
                    "status": cond_match.group("status").lower(),
                    "job": cond_match.group("job").strip(),
                    "exitcode": cond_match.group("exitcode"),
                    "raw": part
                })
            else:
                if part:
                    parsed.append({"type": "unknown", "raw": part})

    return parsed


# ---------------------------------------------------------------------------
# Post-processing per job
# ---------------------------------------------------------------------------

def _enrich_job(job: Dict) -> Dict:
    """
    Post-process a parsed job dict:
      - Normalise job_type to uppercase
      - Resolve OS hint (linux vs windows) from machine name
      - Parse condition strings into structured list
      - Tag each job with detected_os
    """
    # Normalise type
    job_type = job.get("job_type", "").upper().strip()
    job["job_type"] = job_type

    # Parse conditions
    raw_conds = job.get("condition", [])
    if isinstance(raw_conds, str):
        raw_conds = [raw_conds]
    if raw_conds:
        job["parsed_conditions"] = _parse_conditions(raw_conds)

    # Detect OS from machine name heuristics
    machine = job.get("machine", "").lower()
    if job_type == "CMD":
        if any(kw in machine for kw in ["win", "wnd", "windows", "w2k", "w32", "w64"]):
            job["detected_os"] = "windows"
        else:
            job["detected_os"] = "linux"   # Default assumption for CMD

    # Normalise boolean-ish string fields
    for bool_field in ["alarm_if_fail", "alarm_if_terminated", "send_notification"]:
        val = job.get(bool_field)
        if isinstance(val, list):
            # Multi-value — keep as list
            pass
        elif isinstance(val, str):
            job[bool_field] = val.lower() in ("1", "yes", "true", "y")

    return job


# ---------------------------------------------------------------------------
# File-level parser
# ---------------------------------------------------------------------------

def parse_jil_file(filepath: str) -> Dict[str, Dict]:
    """
    Parse a single JIL file.

    Returns a dict: { job_name: job_dict, ... }
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"JIL file not found: {filepath}")

    logger.info("Parsing JIL file: %s", filepath)
    raw_text = path.read_text(encoding="utf-8", errors="replace")

    # Strip comments
    cleaned = _strip_comments(raw_text)

    # Split into blocks on insert_job or update_job lines
    # Handles both:
    #   insert_job: "job_name"   job_type: CMD
    #   insert_job "job_name" job_type: CMD
    header_pattern = re.compile(
        r"^(insert_job|update_job)\s*:?\s+\"?(?P<job_name>[^\"\s]+)\"?\s+job_type\s*:\s*(?P<job_type>\S+)",
        re.IGNORECASE | re.MULTILINE
    )

    jobs: Dict[str, Dict] = {}
    matches = list(header_pattern.finditer(cleaned))

    for i, match in enumerate(matches):
        job_name = match.group("job_name").strip()
        job_type_raw = match.group("job_type").strip().upper()

        # Block content: from end of this header line to start of next header
        block_start = match.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        block_content = cleaned[block_start:block_end]

        # Parse the attribute block
        job = _parse_job_block(block_content) or {}

        # Set top-level fields from header
        job["job_name"] = job_name
        job["job_type"] = job_type_raw
        job["_source_file"] = str(path.name)
        job["_operation"] = match.group(1).lower()   # insert_job or update_job

        # Warn on unknown job types
        if job_type_raw not in KNOWN_JOB_TYPES:
            logger.warning("Unknown job type '%s' for job '%s'", job_type_raw, job_name)

        job = _enrich_job(job)
        jobs[job_name] = job

    logger.info("  → Parsed %d jobs from %s", len(jobs), path.name)
    return jobs


# ---------------------------------------------------------------------------
# Directory-level parser
# ---------------------------------------------------------------------------

def parse_all_jil_files(directory: str) -> Dict[str, Dict]:
    """
    Parse all .jil files in a directory (non-recursive).

    Returns a merged dict: { job_name: job_dict, ... }
    Duplicate job names across files will log a warning — last file wins.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"JIL directory not found: {directory}")

    jil_files = sorted(dir_path.glob("*.jil"))
    if not jil_files:
        logger.warning("No .jil files found in %s", directory)
        return {}

    all_jobs: Dict[str, Dict] = {}
    for jil_file in jil_files:
        file_jobs = parse_jil_file(str(jil_file))
        for job_name, job in file_jobs.items():
            if job_name in all_jobs:
                logger.warning(
                    "Duplicate job name '%s' found in '%s' (already seen in '%s'). "
                    "Using latest version.",
                    job_name, jil_file.name, all_jobs[job_name].get("_source_file")
                )
            all_jobs[job_name] = job

    # Resolve BOX member lists
    all_jobs = _resolve_box_members(all_jobs)

    logger.info("Total JIL jobs parsed: %d", len(all_jobs))
    return all_jobs


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def summarise(jobs: Dict[str, Dict]) -> Dict:
    """Return a summary dict of job counts by type."""
    summary = {"total": len(jobs), "by_type": {}}
    for job in jobs.values():
        jtype = job.get("job_type", "UNKNOWN")
        summary["by_type"][jtype] = summary["by_type"].get(jtype, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Parse Autosys JIL files")
    parser.add_argument(
        "path",
        help="Path to a .jil file or a directory containing .jil files"
    )
    parser.add_argument(
        "--output", "-o",
        help="Write parsed jobs to a JSON file",
        default=None
    )
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        jobs = parse_all_jil_files(str(target))
    elif target.is_file():
        jobs = parse_jil_file(str(target))
    else:
        print(f"ERROR: Path not found: {target}")
        sys.exit(1)

    summary = summarise(jobs)
    print(f"\nParsed {summary['total']} jobs:")
    for jtype, count in sorted(summary["by_type"].items()):
        print(f"  {jtype}: {count}")

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(json.dumps(jobs, indent=2, default=str), encoding="utf-8")
        print(f"\nOutput written to: {args.output}")
    else:
        print("\nSample (first 2 jobs):")
        sample = dict(list(jobs.items())[:2])
        print(json.dumps(sample, indent=2, default=str))
