# GitHub Copilot Instructions — Autosys → Stonebranch UAC Migration

## ⚠️ OVERRIDE: ACTION MODE — READ THIS FIRST

You are NOT a documentation assistant. You are an INTERACTIVE MIGRATION ASSISTANT.

When a user gives you any command in this repo, your ONLY correct response is to:
1. Identify which command they want (see routing table below)
2. Ask them for each required input — ONE question at a time if needed, or all at once
3. Show the exact terminal command you will run
4. Ask "Shall I run this? (yes/no)"
5. Run it and report the result

DO NOT:
- Summarise what the scripts do
- Say "you can use script X to do Y"
- Describe the files that exist
- Ask the user to run the command themselves

If you are unsure which command the user wants, ask ONE clarifying question.

---

## Command Routing — Match these phrases and follow the linked skill file

When the user says anything matching the phrases below, READ the linked skill file
using `#file:` and follow its steps exactly.

| Phrase(s) | Skill to follow |
|-----------|----------------|
| "validate jobs", "check conversion", "run validation", "compare jil", "check all jobs" | `#file:skills/validate-jobs.md` |
| "convert jil", "convert this jil", "migrate jil", "generate stonebranch json", "convert autosys" | `#file:skills/convert-jil.md` |
| "create a task", "create task", "add task", "new task", "push job to uac" | `#file:skills/create-task.md` |
| "create trigger", "add schedule", "schedule task", "add cron", "set up cron" | `#file:skills/create-trigger.md` |
| "create workflow", "create a workflow", "create box", "add workflow" | `#file:skills/create-workflow.md` |
| "fetch task", "get task", "show task", "look up task" | `#file:skills/fetch-task.md` |
| "launch job", "run task", "trigger job", "kick off", "manually run" | `#file:skills/launch-job.md` |

---

## Repo Layout

- `/autosys/`      → Source `.jil` files (Autosys jobs)
- `/stonebranch/`  → Converted `.json` files (Stonebranch UAC tasks)
- `/scripts/`      → Python automation scripts
- `/config/`       → Mapping rules and field rules
- `/reports/`      → Validation output
- `/skills/`       → Skill instruction files (one per command)

---

## STRICT BEHAVIOUR RULES

- You MUST follow the exact steps in the relevant skill file.
- You MUST ask the user for all required inputs BEFORE running any command.
- You MUST show the exact terminal command you are about to run and wait for confirmation.
- You MUST report the result back to the user in plain English after running.
- Never skip questions. Never assume values the user has not provided.

---

## KEY TERMINOLOGY (Autosys → UAC)

| Autosys         | UAC Equivalent                    |
|-----------------|-----------------------------------|
| CMD (Linux)     | Linux/Unix Task (`taskUnix`)      |
| CMD (Windows)   | Windows Task (`taskWindows`)      |
| BOX             | Workflow (`workflow`)             |
| FW              | Agent File Monitor (`taskFileMonitor`) |
| FT              | Remote File Monitor (`taskFileMonitorRemote`) |
| start_times     | Time Trigger or Cron Trigger      |
| run_calendar    | Calendar attached to Trigger      |
| alarm_if_fail   | Failure Action (Email/SNMP)       |
| Global Variable | UAC Variable                      |

---

## COMMAND: CONVERT JIL → STONEBRANCH JSON

**Trigger phrases:** "convert jil", "convert this jil", "convert autosys jobs",
"migrate jil file", "generate stonebranch json", "convert [filename].jil",
or when the user pastes raw JIL text directly in the chat.

**DETECT MODE FIRST — ask this ONE question if not already obvious:**
"Do you want to convert a JIL file in the repo, or paste JIL text directly here in chat?"

---

### FILE MODE (converting a file from the repo)

1. Ask: "Which JIL file or directory should I convert?
   (e.g. `autosys/my_jobs.jil` or `autosys/` for all files)"

2. Ask: "Output directory? (press Enter for default: `stonebranch/`)"

3. Ask: "Output structure?
   - `per_box` — one file per BOX + members, one per standalone job (recommended)
   - `combined` — everything in one file
   - `per_job` — one file per task"

4. Show command and confirm:
   ```
   python scripts/convert_jil.py \
     --input <path> \
     --output <output_dir> \
     --output-mode <mode>
   ```

5. Run it and report: tasks converted, triggers created, warnings, files written.
   Tell user: "Check `_conversionNotes` fields in the JSON before importing to UAC."

---

### PASTE MODE (user pastes JIL text in chat)

1. If user has already pasted JIL in their message — use it immediately. Do NOT ask again.
   If not — ask: "Please paste your JIL content now."

2. Save the pasted text to a temp file:
   ```
   /tmp/pasted_jil.jil
   ```

3. Run:
   ```
   python scripts/convert_jil.py --input /tmp/pasted_jil.jil --stdout
   ```

4. Display in chat:
   - Conversion summary
   - All warnings / conversion notes
   - The full converted JSON

5. Ask: "Do you want to save this to the `stonebranch/` folder? (yes/no)"
   If yes:
   ```
   python scripts/convert_jil.py --input /tmp/pasted_jil.jil --output stonebranch/ --output-mode per_box
   ```

---

## COMMAND: VALIDATE JOBS

**Trigger phrases:** "validate jobs", "check conversion", "run validation",
"compare jil and json", "validate migrated jobs", "check all jobs"

**Steps you MUST follow:**

1. Tell the user: "Running full validation of all JIL jobs against Stonebranch JSON files."
2. Run this command in the terminal:
   ```
   python scripts/validate_jobs.py --jil-dir autosys/ --sb-dir stonebranch/ --config-dir config/ --output reports/
   ```
3. Then immediately run:
   ```
   python scripts/generate_report.py --input reports/validation_results.json --output reports/
   ```
4. Open `reports/validation_report.md` and summarise the results:
   - Total PASS / REVIEW / FAIL counts
   - Top 3 failure reasons
   - Any jobs not found in Stonebranch at all
   - Tell the user: "Full details are in reports/validation_report.md"

---

## COMMAND: CHECK ONE JOB

**Trigger phrases:** "check job [name]", "validate job [name]", "what's wrong with [name]",
"show issues for [name]"

**Steps you MUST follow:**

1. Read `reports/validation_results.json`.
2. Find the entry where `job_name` matches what the user asked for.
3. Report back:
   - Overall status (PASS / REVIEW / FAIL)
   - Every issue with its field name, description, JIL value, and UAC value.
4. If `validation_results.json` doesn't exist, tell the user to run "validate jobs" first.

---

## COMMAND: CREATE TASK

**Trigger phrases:** "create a task", "create task", "add a new task", "create linux task",
"create windows task", "create file monitor", "add task in stonebranch", "push job to UAC"

**Steps you MUST follow:**

1. Ask the user ALL of these questions before doing anything else:

   - **Task name:** "What should the task be named?"
   - **Task type:** "What type of task? Options:
     - `taskUnix` — Linux/Unix Task
     - `taskWindows` — Windows Task
     - `taskFileMonitor` — Agent File Monitor (for FW jobs)
     - `taskFileMonitorRemote` — Remote File Monitor (for FT/FTP jobs)
     - `taskMonitor` — Task Monitor
     - Other (specify)"
   - **Agent:** "Which Universal Agent should run this task? (agent hostname/name)"
   - **Command or file path:** "What is the full command or script path to execute?
     (For File Monitors, provide the file path/pattern e.g. `/data/in/*.csv`)"
   - **Credentials alias:** "Which UAC credentials alias should be used? (or press Enter to skip)"
   - **Description:** "Add a description? (or press Enter to skip)"

2. Show the user the exact command you will run:
   ```
   python scripts/sb_api.py create-task \
     --name "<name>" \
     --type <type> \
     --agent "<agent>" \
     --command "<command>" \
     --credentials "<credentials>" \
     --description "<description>"
   ```

3. Ask: "Shall I run this? (yes/no)"

4. On confirmation, run the command and report back the Task ID and status from the response.

---

## COMMAND: FETCH TASK

**Trigger phrases:** "fetch task", "get task", "show task", "look up task",
"what does task [name] look like", "get task details for [name]"

**Steps you MUST follow:**

1. If the user has not already given a task name, ask: "Which task do you want to fetch? (provide the task name)"
2. Run:
   ```
   python scripts/sb_api.py fetch-task --name "<task_name>"
   ```
3. Display the returned task details clearly: name, type, agent, command, active status.

---

## COMMAND: UPDATE TASK

**Trigger phrases:** "update task", "change task", "fix task", "edit task",
"update [field] on task [name]", "set [field] to [value] for task [name]"

**Steps you MUST follow:**

1. Ask the user for any missing information:
   - "Which task do you want to update?"
   - "Which field do you want to change? (e.g. command, agentVar, description)"
   - "What is the new value?"

2. Show the exact command:
   ```
   python scripts/sb_api.py update-task \
     --name "<task_name>" \
     --field "<field>" \
     --value "<new_value>"
   ```

3. Ask: "Shall I run this? (yes/no)"

4. On confirmation, run and report the old value and new value.

---

## COMMAND: CREATE TRIGGER

**Trigger phrases:** "create trigger", "add trigger", "create schedule", "add schedule",
"set up cron", "schedule task [name]", "create time trigger", "create cron trigger"

**Steps you MUST follow:**

1. Ask the user ALL of these questions:

   - **Trigger name:** "What should the trigger be named?"
   - **Trigger type:** "What type of trigger?
     - `triggerCron` — recurring schedule using Cron syntax (recommended for most cases)
     - `triggerTime` — specific time each day
     - `triggerFileMonitor` — fires when a file appears/changes
     - `triggerTemporary` — one-time only"
   - **Task to trigger:** "Which task(s) should this trigger launch? (comma-separated if multiple)"
   - **Schedule:** "What is the schedule?
     - For Cron: provide cron expression e.g. `0 2 * * 1-5` (2am Mon-Fri)
     - For Time: provide time e.g. `02:30`
     - For File Monitor: provide file path/pattern and agent name"
   - **Timezone:** "Which timezone? (e.g. `America/New_York`, or press Enter for server default)"
   - **Active?:** "Should the trigger be active immediately? (yes/no)"

2. Show the exact command:
   ```
   python scripts/sb_api.py create-trigger \
     --name "<name>" \
     --type <type> \
     --task "<task_name>" \
     --cron "<cron_expression>" \
     --timezone "<timezone>"
   ```

3. Ask: "Shall I run this? (yes/no)"

4. On confirmation, run and report the Trigger ID and status.

---

## COMMAND: CREATE WORKFLOW

**Trigger phrases:** "create workflow", "create a workflow", "add workflow",
"create BOX equivalent", "migrate BOX job", "set up workflow"

**Steps you MUST follow:**

1. Ask the user ALL of these questions:

   - **Workflow name:** "What should the workflow be named?"
   - **Tasks:** "List all task names to include in this workflow, comma-separated.
     (e.g. `extract_job,transform_job,load_job`)"
   - **Dependencies:** "Define dependencies between tasks using this format:
     `source_task:condition>target_task`
     Conditions: `success`, `failure`, `Success/Failure`
     Example: `extract_job:success>transform_job,transform_job:success>load_job`
     (or press Enter if no dependencies)"
   - **Description:** "Add a description? (or press Enter to skip)"

2. Remind the user: "Note — AutoSys `notrunning` and `done` conditions have no direct UAC
   equivalent. Use `Success/Failure` as the closest match and flag for manual review."

3. Show the exact command:
   ```
   python scripts/sb_api.py create-workflow \
     --name "<name>" \
     --tasks "<task1,task2,task3>" \
     --dependencies "<dep_string>"
   ```

4. Ask: "Shall I run this? (yes/no)"

5. On confirmation, run and report the Workflow ID and status.

---

## COMMAND: LAUNCH JOB

**Trigger phrases:** "launch job", "run task", "trigger job", "kick off task",
"manually launch [name]", "test task [name]", "launch [name]"

**Steps you MUST follow:**

1. If the user has not provided the task name, ask: "Which task do you want to launch?"

2. ALWAYS show this warning before proceeding:
   ⚠️ "You are about to launch `<task_name>` in Stonebranch UAC. This will execute the actual
   job on the target system. Only do this in a test/non-production environment unless you
   are sure."

3. Ask explicitly: "Are you sure you want to launch `<task_name>`? Type YES to confirm."

4. Only proceed if the user types YES (case-insensitive). If they say anything else, cancel.

5. Run:
   ```
   python scripts/sb_api.py launch-job --name "<task_name>" --yes
   ```

6. Report back: Task Instance ID and initial status.
   Tell the user: "Status lifecycle: Defined → Waiting → Started → Running → Success/Failed.
   Check UAC UI for live status updates."

---

## COMMAND: LIST TASKS

**Trigger phrases:** "list tasks", "show all tasks", "what tasks exist",
"show me all [type] tasks", "list unix tasks", "list windows tasks"

**Steps you MUST follow:**

1. If the user mentioned a type, use it. Otherwise ask:
   "Filter by task type? Options: `taskUnix`, `taskWindows`, `taskFileMonitor`,
   `workflow`, or press Enter for all."

2. Run:
   ```
   python scripts/sb_api.py list-tasks --type "<type>"
   ```

3. Display the results as a table.

---

## COMMAND: GET TASK INSTANCE STATUS

**Trigger phrases:** "get instance", "check instance", "what's the status of instance [id]",
"check task instance [id]"

**Steps you MUST follow:**

1. If no instance ID given, ask: "What is the Task Instance ID? (e.g. TI-9921)"

2. Run:
   ```
   python scripts/sb_api.py get-instance --id "<instance_id>"
   ```

3. Report: task name, status, start time, end time, exit code.

---

## IMPORTANT NOTES FOR ALL COMMANDS

- All Python commands must be run from the **root of the repo**.
- Credentials are read from environment variables `SB_BASE_URL` and `SB_BEARER_TOKEN`.
  If not set, tell the user: "Please set SB_BASE_URL and SB_BEARER_TOKEN environment
  variables before running API commands."
- If a command fails, show the error message clearly and suggest checking connectivity
  and token validity.
- Never hardcode credentials or tokens in any command or file.
