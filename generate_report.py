"""
generate_report.py
------------------
Reads reports/validation_results.json produced by validate_jobs.py
and writes a formatted Markdown report to reports/validation_report.md.

Report sections:
  1. Summary table (PASS / REVIEW / FAIL counts)
  2. ❌ FAILED jobs table
  3. ⚠️ REVIEW jobs table
  4. ✅ PASSED jobs table
  5. Orphan UAC tasks (in Stonebranch but not in JIL)
  6. Global issues (circular dependencies, etc.)

Usage:
  python generate_report.py
  python generate_report.py --input reports/validation_results.json --output reports/
"""

import json
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PASS   = "PASS"
REVIEW = "REVIEW"
FAIL   = "FAIL"


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    """Render a Markdown table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    def _pad(text: str, width: int) -> str:
        return str(text).ljust(width)

    header_row  = "| " + " | ".join(_pad(h, col_widths[i]) for i, h in enumerate(headers)) + " |"
    divider_row = "| " + " | ".join("-" * col_widths[i] for i in range(len(headers))) + " |"
    data_rows   = [
        "| " + " | ".join(_pad(str(row[i]) if i < len(row) else "", col_widths[i])
                          for i in range(len(headers))) + " |"
        for row in rows
    ]
    return "\n".join([header_row, divider_row] + data_rows)


def _truncate(text: str, max_len: int = 60) -> str:
    """Truncate long strings for table display."""
    text = str(text)
    return text[:max_len - 3] + "..." if len(text) > max_len else text


def _escape_pipe(text: str) -> str:
    """Escape pipe characters so they don't break Markdown tables."""
    return str(text).replace("|", "\\|")


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_header(summary: Dict) -> str:
    total      = summary["total_jil_jobs"]
    pass_count = summary["pass_count"]
    rev_count  = summary["review_count"]
    fail_count = summary["fail_count"]
    timestamp  = summary.get("timestamp", datetime.now().isoformat(timespec="seconds"))
    jil_dir    = summary.get("jil_dir", "autosys/")
    sb_dir     = summary.get("sb_dir", "stonebranch/")

    pass_pct   = f"{pass_count/total*100:.1f}%" if total else "0%"
    review_pct = f"{rev_count/total*100:.1f}%"  if total else "0%"
    fail_pct   = f"{fail_count/total*100:.1f}%" if total else "0%"

    return f"""# Autosys → Stonebranch UAC Validation Report

**Generated:** {timestamp}
**JIL Source:** `{jil_dir}`
**Stonebranch JSON:** `{sb_dir}`

---

## Summary

{_md_table(
    ["Status", "Count", "% of Total"],
    [
        ["✅ PASS",   str(pass_count),   pass_pct],
        ["⚠️ REVIEW", str(rev_count),    review_pct],
        ["❌ FAIL",   str(fail_count),   fail_pct],
        ["**Total**", f"**{total}**",    "**100%**"],
    ]
)}

**Jobs in JIL not found in Stonebranch (not converted):** {summary.get("orphan_count", 0)}
**UAC orphan tasks (in Stonebranch but not in JIL):** {summary.get("orphan_count", 0)}
"""


def _build_global_issues(global_issues: List[Dict]) -> str:
    if not global_issues:
        return ""

    lines = [
        "---",
        "",
        "## 🔄 Global Issues (Circular Dependencies)",
        "",
    ]
    for issue in global_issues:
        lines.append(f"- ❌ **{issue['field']}**: {issue['description']}")
    lines.append("")
    return "\n".join(lines)


def _build_top_failures(top_fail_reasons: List) -> str:
    if not top_fail_reasons:
        return ""

    lines = [
        "---",
        "",
        "## 📊 Top Failure Reasons",
        "",
    ]
    for reason, count in top_fail_reasons:
        lines.append(f"- **[{count}x]** {reason}")
    lines.append("")
    return "\n".join(lines)


def _build_fail_section(fail_results: List[Dict]) -> str:
    if not fail_results:
        return "---\n\n## ❌ Failed Jobs\n\n*No failed jobs — great!*\n"

    lines = [
        "---",
        "",
        f"## ❌ Failed Jobs — Manual Fix Required ({len(fail_results)} jobs)",
        "",
        "> These jobs have critical issues that **must** be corrected before migration is complete.",
        "",
    ]

    row_num = 0
    for result in fail_results:
        fail_issues = [i for i in result.get("issues", []) if i["severity"] == FAIL]
        for issue in fail_issues:
            row_num += 1
            lines.append(
                f"**{row_num}. `{result['job_name']}`** "
                f"({result['job_type']} → {result['uac_type'] or 'N/A'})"
            )
            lines.append(f"   - **Field:** `{issue['field']}`")
            lines.append(f"   - **Issue:** {issue['description']}")
            if issue["jil_value"]:
                lines.append(f"   - **JIL Value:** `{_truncate(issue['jil_value'], 80)}`")
            if issue["uac_value"]:
                lines.append(f"   - **UAC Value:** `{_truncate(issue['uac_value'], 80)}`")
            lines.append("")

    # Also a compact table view for quick scanning
    lines.append("")
    lines.append("### Quick Reference Table")
    lines.append("")

    table_rows = []
    for result in fail_results:
        fail_issues = [i for i in result.get("issues", []) if i["severity"] == FAIL]
        for issue in fail_issues:
            table_rows.append([
                f"`{result['job_name']}`",
                result["job_type"],
                _truncate(_escape_pipe(issue["description"]), 55),
                _truncate(_escape_pipe(issue["jil_value"]), 25),
                _truncate(_escape_pipe(issue["uac_value"]), 25),
                f"`{issue['field']}`",
            ])

    lines.append(_md_table(
        ["Job Name", "JIL Type", "Issue", "JIL Value", "UAC Value", "Field"],
        table_rows
    ))
    lines.append("")
    return "\n".join(lines)


def _build_review_section(review_results: List[Dict]) -> str:
    if not review_results:
        return "---\n\n## ⚠️ Review Jobs\n\n*No jobs require review — excellent!*\n"

    lines = [
        "---",
        "",
        f"## ⚠️ Review Jobs — Check & Confirm ({len(review_results)} jobs)",
        "",
        "> These jobs have been converted but contain values that are **suspicious, approximate,**",
        "> **or have no direct UAC equivalent**. Review each one and confirm or correct manually.",
        "",
    ]

    table_rows = []
    for result in review_results:
        review_issues = [i for i in result.get("issues", []) if i["severity"] == REVIEW]
        for issue in review_issues:
            table_rows.append([
                f"`{result['job_name']}`",
                result["job_type"],
                result["uac_type"] or "N/A",
                _truncate(_escape_pipe(issue["description"]), 55),
                _truncate(_escape_pipe(issue["jil_value"]), 25),
                _truncate(_escape_pipe(issue["uac_value"]), 25),
                f"`{issue['field']}`",
            ])

    lines.append(_md_table(
        ["Job Name", "JIL Type", "UAC Type", "Concern", "JIL Value", "UAC Value", "Field"],
        table_rows
    ))
    lines.append("")
    return "\n".join(lines)


def _build_pass_section(pass_results: List[Dict]) -> str:
    if not pass_results:
        return "---\n\n## ✅ Passed Jobs\n\n*No jobs fully passed validation.*\n"

    lines = [
        "---",
        "",
        f"## ✅ Passed Jobs ({len(pass_results)} jobs)",
        "",
        "> All required fields present and correctly mapped.",
        "",
    ]

    table_rows = []
    for result in pass_results:
        table_rows.append([
            f"`{result['job_name']}`",
            result["job_type"],
            result["uac_type"] or "N/A",
            "All fields matched ✅",
        ])

    lines.append(_md_table(
        ["Job Name", "JIL Type", "UAC Type", "Notes"],
        table_rows
    ))
    lines.append("")
    return "\n".join(lines)


def _build_orphan_section(orphans: List[Dict]) -> str:
    if not orphans:
        return ""

    lines = [
        "---",
        "",
        f"## 👻 Orphan UAC Tasks ({len(orphans)} tasks)",
        "",
        "> These tasks exist in Stonebranch UAC but have **no corresponding JIL job**.",
        "> They may be new tasks added by the central team, or incorrectly named.",
        "",
    ]

    table_rows = [
        [f"`{o['uac_name']}`", o.get("uac_type", ""), o.get("source_file", "")]
        for o in orphans
    ]

    lines.append(_md_table(["UAC Task Name", "UAC Type", "Source File"], table_rows))
    lines.append("")
    return "\n".join(lines)


def _build_footer(summary: Dict) -> str:
    return f"""---

## 📋 Validation Notes

| Item | Note |
|---|---|
| `exclude_calendar` | AutoSys `exclude_calendar` has no direct UAC equivalent. Must manually construct a UAC Calendar selecting all days but excluded ones. |
| `notrunning` / `done` | These dependency conditions have no direct UAC equivalent. Mapped to Success/Failure combined — verify intended behaviour. |
| OS detection | CMD job OS (Linux vs Windows) was inferred from the `machine` field name. Confirm with the target agent configuration. |
| FW vs FT | FW (File Watcher) = Agent File Monitor (`taskFileMonitor`). FT (File Trigger/FTP) = Remote File Monitor (`taskFileMonitorRemote`). These are NOT interchangeable. |
| Triggers | Every JIL job with `start_times` must have a corresponding UAC trigger (Time or Cron). Missing triggers are marked ❌ FAIL. |

---
*Report generated by autosys-sb-migration-validator*
*Powered by GHCP automation skills — see `/skills/` for instructions*
"""


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def generate_report(results_file: str, output_dir: str = "reports") -> str:
    """
    Read validation_results.json and write validation_report.md.
    Returns the path to the written report.
    """
    results_path = Path(results_file)
    if not results_path.exists():
        raise FileNotFoundError(f"Validation results not found: {results_file}")

    logger.info("Reading validation results from %s", results_file)
    data = json.loads(results_path.read_text(encoding="utf-8"))

    summary        = data.get("summary", {})
    all_results    = data.get("results", [])
    orphans        = data.get("orphans", [])
    global_issues  = summary.get("global_issues", [])
    top_failures   = summary.get("top_fail_reasons", [])

    fail_results   = [r for r in all_results if r["status"] == FAIL]
    review_results = [r for r in all_results if r["status"] == REVIEW]
    pass_results   = [r for r in all_results if r["status"] == PASS]

    # Build report sections
    sections = [
        _build_header(summary),
        _build_global_issues(global_issues),
        _build_top_failures(top_failures),
        _build_fail_section(fail_results),
        _build_review_section(review_results),
        _build_pass_section(pass_results),
        _build_orphan_section(orphans),
        _build_footer(summary),
    ]

    report_text = "\n".join(sections)

    # Write output
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_file = output_path / "validation_report.md"
    report_file.write_text(report_text, encoding="utf-8")

    logger.info("Report written to %s", report_file)
    return str(report_file)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Markdown validation report")
    parser.add_argument(
        "--input", "-i",
        default="reports/validation_results.json",
        help="Path to validation_results.json"
    )
    parser.add_argument(
        "--output", "-o",
        default="reports",
        help="Output directory for validation_report.md"
    )
    args = parser.parse_args()

    try:
        report_path = generate_report(args.input, args.output)
        print(f"✅ Report generated: {report_path}")
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        print("   Run validate_jobs.py first to generate validation_results.json")
        raise SystemExit(1)
