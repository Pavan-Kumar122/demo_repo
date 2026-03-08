# Skill: Convert JIL → Stonebranch UAC JSON

## Purpose
Convert Autosys JIL job definitions into Stonebranch UAC-compatible JSON task files.
Supports two modes: converting a file from the repo, or converting JIL pasted directly in chat.

---

## DETECT WHICH MODE THE USER WANTS

### Mode A — File conversion
**Trigger phrases:**
"convert this jil", "convert jil to stonebranch", "convert autosys jobs",
"migrate jil file", "generate stonebranch json", "convert [filename].jil"

Ask: "Do you want to convert a JIL file that's already in the repo,
or paste JIL content directly in the chat?"

If they say **file / repo**: follow **FILE MODE** below.
If they say **paste / chat**: follow **PASTE MODE** below.
If the user has already pasted JIL text in their message: go directly to **PASTE MODE**.

---

## FILE MODE

### Step 1 — Identify the file or directory
If the user specified a filename, use it.
Otherwise ask: "Which JIL file do you want to convert?
- Type a filename (e.g. `autosys/my_jobs.jil`)
- Or type `autosys/` to convert all JIL files in the autosys directory"

### Step 2 — Confirm output location
Ask: "Where should the converted JSON files be saved?
(Default: `stonebranch/` — press Enter to use default)"

### Step 3 — Ask output mode
Ask: "How should the output be structured?
- `per_box` (default) — one JSON file per BOX/workflow and its members, one per standalone job
- `combined` — all converted tasks in one single JSON file
- `per_job` — one JSON file per individual task"

### Step 4 — Show command and confirm
Show the user:
```
python scripts/convert_jil.py \
  --input <input_path> \
  --output <output_dir> \
  --output-mode <mode>
```
Ask: "Shall I run this? (yes/no)"

### Step 5 — Run and report
After running, report:
- How many tasks were converted
- How many triggers were created
- How many warnings/notes exist
- Which files were written (list them)
- Tell the user: "Review any `_conversionNotes` fields in the JSON before importing to UAC."

---

## PASTE MODE

### Step 1 — Get the JIL content
If the user has already pasted JIL text in their message, use it directly.
Otherwise ask: "Please paste your JIL content now."

### Step 2 — Save the pasted content to a temp file
Save the pasted JIL text to a temporary file:
```
temp_file = /tmp/pasted_jil_<timestamp>.jil
```
Write the pasted content to that file exactly as-is.

### Step 3 — Run conversion and show output
```
python scripts/convert_jil.py --input /tmp/pasted_jil_<timestamp>.jil --stdout
```

### Step 4 — Display results in chat
Show the user:
- The conversion summary (tasks, triggers, warnings)
- All conversion notes / warnings
- The full converted JSON inline in the chat

### Step 5 — Ask if they want to save
Ask: "Do you want to save this to a file in the `stonebranch/` directory? (yes/no)"
If yes, run:
```
python scripts/convert_jil.py --input /tmp/pasted_jil_<timestamp>.jil \
  --output stonebranch/ --output-mode per_box
```

---

## CONVERSION MAPPING REFERENCE

| JIL Field | UAC Field | Notes |
|-----------|-----------|-------|
| `job_type: CMD` (Linux) | `type: taskUnix` | Detected from machine name |
| `job_type: CMD` (Windows) | `type: taskWindows` | Detected from machine name |
| `job_type: BOX` | `type: workflow` | Members become vertices, conditions become edges |
| `job_type: FW` | `type: taskFileMonitor` | Agent File Monitor |
| `job_type: FT` | `type: taskFileMonitorRemote` | Remote File Monitor (FTP) |
| `command` | `command` | Direct mapping |
| `machine` | `agentVar` | Direct mapping |
| `owner` | `credentials` | Direct mapping |
| `start_times` | `_trigger.cronExpression` | Converted to Cron or Time trigger |
| `days_of_week` | `_trigger.cronExpression` | Merged into cron day-of-week field |
| `run_calendar` | `_trigger.calendar` | Calendar name attached to trigger |
| `exclude_calendar` | ⚠️ Not converted | No UAC equivalent — review note added |
| `condition: success(X)` | `edge: {source:X, condition:Success}` | Direct mapping |
| `condition: failure(X)` | `edge: {source:X, condition:Failure}` | Direct mapping |
| `condition: done(X)` | `edge: {source:X, condition:Success/Failure}` | ⚠️ Approximate — review note |
| `condition: notrunning(X)` | `edge: {source:X, condition:Success/Failure}` | ⚠️ Approximate — review note |
| `condition: terminated(X)` | `edge: {source:X, condition:Cancelled}` | Direct mapping |
| `alarm_if_fail: 1` | `actions: [{type:emailNotification, trigger:onFailure}]` | Configure recipients in UAC |
| `alarm_if_terminated: 1` | `actions: [{type:emailNotification, trigger:onCancelled}]` | Configure recipients in UAC |
| `max_run_alarm` | `actions: [{type:lateFinish, lateFinishDuration:N}]` | In seconds |
| `min_run_alarm` | `actions: [{type:earlyFinish, earlyFinishDuration:N}]` | In seconds |
| `must_start_times` | `actions: [{type:lateStart, lateStartTime:HH:MM}]` | Business SLA — always review |
| `must_complete_times` | `actions: [{type:lateFinishTime, lateFinishTime:HH:MM}]` | Business SLA — always review |
| `std_out_file` | `stdoutLogFile` | Direct mapping |
| `std_err_file` | `stderrLogFile` | Direct mapping |
| `watch_file` | `filename` | For FW and FT jobs |
| `watch_interval` | `scanInterval` | Direct mapping |

---

## WHAT TO REVIEW AFTER CONVERSION

Always tell the user to review these in the output JSON before importing to UAC:

1. **`_conversionNotes`** fields — warnings embedded in each task JSON
2. **`agentVar` / `agent`** — verify the agent name matches exactly what's registered in UAC
3. **`credentials`** — verify the credentials alias exists in UAC
4. **`_trigger`** — verify the schedule is correct, especially day-of-week
5. **`done` / `notrunning` conditions** — these were approximated as `Success/Failure`
6. **`exclude_calendar`** — must be manually configured in UAC
7. **FTP connections** for `taskFileMonitorRemote` — must be configured manually
8. **Email notification recipients** — must be configured manually in UAC
