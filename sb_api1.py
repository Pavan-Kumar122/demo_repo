"""
sb_api.py
---------
Stonebranch UAC REST API helper.
Reads credentials from config/sb_config.yaml + environment variables.
Provides a CLI interface for all API actions invoked by GHCP skill files.

Commands:
  create-task       Create a new task (Linux/Unix, Windows, File Monitor, etc.)
  fetch-task        Fetch a task by name or ID
  update-task       Update a field on an existing task
  create-trigger    Create a trigger (Time, Cron, Agent File Monitor, etc.)
  create-workflow   Create a workflow with tasks and dependencies
  launch-job        Launch a task instance immediately
  list-tasks        List tasks with optional type/name filter
  get-instance      Get the status of a task instance

Usage (via GHCP skill files):
  python sb_api.py create-task --name my_task --type taskUnix --agent prod-lnx-01 --command /opt/run.sh
  python sb_api.py fetch-task --name my_task
  python sb_api.py launch-job --name my_task
  python sb_api.py create-trigger --name my_trigger --type triggerCron --task my_task --cron "0 2 * * *"
  python sb_api.py list-tasks --type taskUnix
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, Optional, Any
from datetime import datetime

import yaml

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not installed. Run: pip install requests")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config(config_path: str = "config/sb_config.yaml") -> Dict:
    """
    Load Stonebranch connection config.
    Environment variables always override file values.

    Required config:
      stonebranch.base_url
      stonebranch.bearer_token  (or env: SB_BEARER_TOKEN)
    """
    cfg = {}
    config_file = Path(config_path)

    if config_file.exists():
        with open(config_file, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        cfg = raw.get("stonebranch", {})
    else:
        logger.warning("Config file not found at %s — falling back to environment variables only.", config_path)

    # Environment variable overrides
    cfg["base_url"]      = os.environ.get("SB_BASE_URL",      cfg.get("base_url", "")).rstrip("/")
    cfg["bearer_token"]  = os.environ.get("SB_BEARER_TOKEN",  cfg.get("bearer_token", ""))
    cfg["verify_ssl"]    = cfg.get("verify_ssl", True)
    cfg["timeout"]       = int(cfg.get("timeout", 30))

    if not cfg["base_url"]:
        raise ValueError(
            "Stonebranch base_url not set. "
            "Set it in config/sb_config.yaml or via SB_BASE_URL environment variable."
        )
    if not cfg["bearer_token"]:
        raise ValueError(
            "Stonebranch bearer_token not set. "
            "Set the SB_BEARER_TOKEN environment variable, "
            "or add bearer_token to config/sb_config.yaml."
        )

    return cfg


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class StoneBranchClient:
    """
    Thin HTTP client for the Stonebranch UAC REST API.

    All methods return (success: bool, data: dict | str) tuples.
    """

    def __init__(self, config: Dict):
        self.base_url   = config["base_url"]
        self.verify_ssl = config["verify_ssl"]
        self.timeout    = config["timeout"]
        self.session    = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config['bearer_token']}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _handle_response(self, resp: requests.Response) -> tuple:
        """Parse response and return (success, data)."""
        try:
            data = resp.json()
        except Exception:
            data = resp.text

        if resp.status_code in (200, 201):
            return True, data
        else:
            error_msg = (
                data.get("message") or data.get("error") or str(data)
                if isinstance(data, dict) else str(data)
            )
            logger.error("API error %d: %s", resp.status_code, error_msg)
            return False, {"status_code": resp.status_code, "error": error_msg}

    def get(self, path: str, params: Optional[Dict] = None) -> tuple:
        try:
            resp = self.session.get(
                self._url(path), params=params,
                verify=self.verify_ssl, timeout=self.timeout
            )
            return self._handle_response(resp)
        except requests.exceptions.ConnectionError:
            return False, {"error": f"Cannot connect to Stonebranch UAC at {self.base_url}. "
                                     "Check your network and base_url in config/sb_config.yaml."}
        except requests.exceptions.Timeout:
            return False, {"error": f"Request timed out after {self.timeout}s."}
        except Exception as exc:
            return False, {"error": str(exc)}

    def post(self, path: str, payload: Dict) -> tuple:
        try:
            resp = self.session.post(
                self._url(path), json=payload,
                verify=self.verify_ssl, timeout=self.timeout
            )
            return self._handle_response(resp)
        except requests.exceptions.ConnectionError:
            return False, {"error": f"Cannot connect to Stonebranch UAC at {self.base_url}."}
        except requests.exceptions.Timeout:
            return False, {"error": f"Request timed out after {self.timeout}s."}
        except Exception as exc:
            return False, {"error": str(exc)}

    def put(self, path: str, payload: Dict) -> tuple:
        try:
            resp = self.session.put(
                self._url(path), json=payload,
                verify=self.verify_ssl, timeout=self.timeout
            )
            return self._handle_response(resp)
        except Exception as exc:
            return False, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Task operations
# ---------------------------------------------------------------------------

def create_task(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """
    POST /resources/task
    Creates a new task in Stonebranch UAC.
    """
    task_type = args.type

    # Base payload — fields common to all task types
    payload: Dict[str, Any] = {
        "type":        task_type,
        "name":        args.name,
    }

    if args.description:
        payload["description"] = args.description

    # Type-specific fields
    if task_type in ("taskUnix", "taskWindows"):
        if args.command:
            payload["command"] = args.command
        if args.agent:
            payload["agentVar"] = args.agent
        if args.credentials:
            payload["credentials"] = args.credentials

    elif task_type in ("taskFileMonitor", "taskFileMonitorRemote"):
        if args.filename:
            payload["filename"] = args.filename
        elif args.command:
            payload["filename"] = args.command   # Allow --command as alias for --filename
        if args.agent:
            payload["agent"] = args.agent
        if args.scan_interval:
            payload["scanInterval"] = args.scan_interval

    elif task_type == "workflow":
        # Workflow tasks require vertices/edges — use create-workflow command instead
        logger.warning("Use 'create-workflow' command for workflows with vertices and edges.")

    success, data = client.post("/resources/task", payload)

    if success:
        task_id   = _extract_id(data)
        task_name = data.get("name") or args.name if isinstance(data, dict) else args.name
        result = {
            "status":    "created",
            "task_name": task_name,
            "task_id":   task_id,
            "type":      task_type,
        }
        print(f"✅ Task '{args.name}' created successfully.")
        print(f"   Task ID:   {task_id}")
        print(f"   Task Type: {task_type}")
        if args.agent:
            print(f"   Agent:     {args.agent}")
        if args.command or args.filename:
            val = args.command or args.filename
            print(f"   Command/File: {val}")
        return result
    else:
        _print_error("create task", args.name, data)
        return {"status": "error", "error": data}


def fetch_task(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """
    GET /resources/task?taskname=<name>  or  GET /resources/task/<id>
    """
    if args.id:
        success, data = client.get(f"/resources/task/{args.id}")
        identifier = f"ID={args.id}"
    else:
        success, data = client.get("/resources/task", params={"taskname": args.name})
        identifier = f"name='{args.name}'"

    if success:
        # UAC returns array for name search, single object for ID
        tasks = data if isinstance(data, list) else [data]
        if not tasks:
            print(f"⚠️  No task found with {identifier}")
            return {"status": "not_found"}

        task = tasks[0]
        print(f"✅ Task found: {task.get('name', 'N/A')}")
        print(f"   Type:        {task.get('type', 'N/A')}")
        print(f"   Description: {task.get('description', 'N/A')}")
        print(f"   Agent:       {task.get('agentVar') or task.get('agent', 'N/A')}")
        print(f"   Command:     {task.get('command', 'N/A')}")
        print(f"   Active:      {task.get('active', 'N/A')}")
        print("\n   Full JSON:")
        # Print clean version (no internal system fields)
        clean = {k: v for k, v in task.items() if not k.startswith("sys")}
        print(json.dumps(clean, indent=4, default=str))
        return {"status": "found", "task": task}
    else:
        _print_error("fetch task", identifier, data)
        return {"status": "error", "error": data}


def update_task(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """
    Fetch task, update the specified field, then PUT /resources/task.
    """
    # First fetch the existing task
    success, data = client.get("/resources/task", params={"taskname": args.name})
    if not success or not data:
        _print_error("fetch task for update", args.name, data)
        return {"status": "error"}

    tasks = data if isinstance(data, list) else [data]
    if not tasks:
        print(f"❌ Task '{args.name}' not found — cannot update.")
        return {"status": "not_found"}

    task = tasks[0]
    old_value = task.get(args.field)
    task[args.field] = args.value

    success, put_data = client.put("/resources/task", task)
    if success:
        print(f"✅ Task '{args.name}' updated successfully.")
        print(f"   Field:     {args.field}")
        print(f"   Old value: {old_value}")
        print(f"   New value: {args.value}")
        return {"status": "updated", "field": args.field, "old": old_value, "new": args.value}
    else:
        _print_error("update task", args.name, put_data)
        return {"status": "error", "error": put_data}


def list_tasks(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """GET /resources/task with optional filters."""
    params: Dict[str, str] = {}
    if args.type:
        params["type"] = args.type
    if args.filter:
        params["taskname"] = args.filter

    success, data = client.get("/resources/task", params=params)
    if success:
        tasks = data if isinstance(data, list) else [data]
        print(f"✅ Found {len(tasks)} task(s):")
        print()
        headers = ["Name", "Type", "Agent", "Active"]
        rows = []
        for t in tasks:
            rows.append([
                t.get("name", ""),
                t.get("type", ""),
                t.get("agentVar") or t.get("agent", ""),
                str(t.get("active", "")),
            ])
        # Simple table output
        col_w = [max(len(h), max((len(str(r[i])) for r in rows), default=0))
                 for i, h in enumerate(headers)]
        fmt = "  " + "  ".join(f"{{:<{w}}}" for w in col_w)
        print(fmt.format(*headers))
        print("  " + "  ".join("-" * w for w in col_w))
        for row in rows:
            print(fmt.format(*[str(c) for c in row]))
        return {"status": "ok", "count": len(tasks)}
    else:
        _print_error("list tasks", str(params), data)
        return {"status": "error", "error": data}


# ---------------------------------------------------------------------------
# Trigger operations
# ---------------------------------------------------------------------------

def create_trigger(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """POST /resources/trigger"""
    trigger_type = args.type or "triggerTime"

    payload: Dict[str, Any] = {
        "type":   trigger_type,
        "name":   args.name,
        "active": not args.inactive,
    }

    # Tasks to trigger (one or more, comma-separated)
    if args.task:
        task_list = [t.strip() for t in args.task.split(",")]
        payload["tasks"] = task_list

    # Cron trigger
    if trigger_type == "triggerCron":
        if not args.cron:
            print("❌ --cron is required for triggerCron type (e.g. --cron '0 2 * * *')")
            return {"status": "error"}
        payload["cronExpression"] = args.cron

    # Time trigger
    elif trigger_type == "triggerTime":
        if args.cron:
            # Parse simple cron: "0 2 * * *" → minutes=0, hours=2
            parts = args.cron.split()
            if len(parts) >= 2:
                payload["minutes"] = parts[0]
                payload["hours"]   = parts[1]
                if len(parts) >= 3:
                    payload["dayOfMonth"] = parts[2]
                if len(parts) >= 5:
                    payload["dayOfWeek"] = parts[4]
        elif args.time:
            # "02:30" format
            time_parts = args.time.split(":")
            if len(time_parts) == 2:
                payload["hours"]   = time_parts[0]
                payload["minutes"] = time_parts[1]

    # Agent File Monitor trigger
    elif trigger_type == "triggerFileMonitor":
        if args.filename:
            payload["filename"] = args.filename
        if args.agent:
            payload["agent"] = args.agent

    if args.timezone:
        payload["timezone"] = args.timezone

    if args.description:
        payload["description"] = args.description

    success, data = client.post("/resources/trigger", payload)
    if success:
        trigger_id = _extract_id(data)
        print(f"✅ Trigger '{args.name}' created successfully.")
        print(f"   Trigger ID:   {trigger_id}")
        print(f"   Trigger Type: {trigger_type}")
        if args.task:
            print(f"   Tasks:        {args.task}")
        if args.cron:
            print(f"   Schedule:     {args.cron}")
        return {"status": "created", "trigger_id": trigger_id}
    else:
        _print_error("create trigger", args.name, data)
        return {"status": "error", "error": data}


# ---------------------------------------------------------------------------
# Workflow operations
# ---------------------------------------------------------------------------

def create_workflow(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """
    POST /resources/workflow
    Creates a workflow with specified task vertices.
    Dependencies (edges) can be specified as task1:success>task2 pairs.
    """
    task_names = [t.strip() for t in args.tasks.split(",")]

    # Build vertex list
    vertices = [{"task": name} for name in task_names]

    # Build edge list from --dependencies arg
    # Format: "task1:success>task2,task2:failure>task3"
    edges = []
    if args.dependencies:
        for dep_str in args.dependencies.split(","):
            dep_str = dep_str.strip()
            if ">" in dep_str and ":" in dep_str:
                left, target = dep_str.split(">", 1)
                source, condition = left.split(":", 1)
                edges.append({
                    "sourceTaskName": source.strip(),
                    "targetTaskName": target.strip(),
                    "condition":      condition.strip(),
                })

    payload: Dict[str, Any] = {
        "type":     "workflow",
        "name":     args.name,
        "vertex":   vertices,
    }

    if edges:
        payload["edge"] = edges

    if args.description:
        payload["description"] = args.description

    success, data = client.post("/resources/workflow", payload)
    if success:
        wf_id = _extract_id(data)
        print(f"✅ Workflow '{args.name}' created successfully.")
        print(f"   Workflow ID: {wf_id}")
        print(f"   Tasks ({len(task_names)}): {', '.join(task_names)}")
        if edges:
            print(f"   Dependencies ({len(edges)}):")
            for e in edges:
                print(f"     {e['sourceTaskName']} --[{e['condition']}]--> {e['targetTaskName']}")
        return {"status": "created", "workflow_id": wf_id}
    else:
        _print_error("create workflow", args.name, data)
        return {"status": "error", "error": data}


# ---------------------------------------------------------------------------
# Launch / run operations
# ---------------------------------------------------------------------------

def launch_job(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """
    POST /ops/task/launch
    Launches a task instance immediately.
    Always requires confirmation unless --yes flag is set.
    """
    task_name = args.name or f"ID:{args.id}"

    # Confirmation guard
    if not args.yes:
        confirm = input(f"\n⚠️  Confirm: launch '{task_name}' in Stonebranch UAC? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("❌ Launch cancelled.")
            return {"status": "cancelled"}

    payload: Dict[str, Any] = {}
    if args.name:
        payload["name"] = args.name
    elif args.id:
        payload["taskId"] = args.id

    success, data = client.post("/ops/task/launch", payload)
    if success:
        instance_id = (
            data.get("taskInstanceId") or data.get("id") or
            data.get("sysId") or "N/A"
            if isinstance(data, dict) else "N/A"
        )
        print(f"✅ Job '{task_name}' launched successfully.")
        print(f"   Task Instance ID: {instance_id}")
        print(f"   Status lifecycle: Defined → Waiting → Started → Running → Success/Failed")
        print(f"   Check UAC UI for live status.")
        return {"status": "launched", "task_instance_id": instance_id}
    else:
        _print_error("launch job", task_name, data)
        return {"status": "error", "error": data}


def get_instance(client: StoneBranchClient, args: argparse.Namespace) -> Dict:
    """GET /resources/taskinstance?id=<id>"""
    success, data = client.get("/resources/taskinstance", params={"id": args.id})
    if success:
        instances = data if isinstance(data, list) else [data]
        if not instances:
            print(f"⚠️  No task instance found with ID {args.id}")
            return {"status": "not_found"}
        inst = instances[0]
        print(f"✅ Task Instance: {inst.get('id') or args.id}")
        print(f"   Task Name: {inst.get('taskName', 'N/A')}")
        print(f"   Status:    {inst.get('status', 'N/A')}")
        print(f"   Started:   {inst.get('startTime', 'N/A')}")
        print(f"   Ended:     {inst.get('endTime', 'N/A')}")
        print(f"   Exit Code: {inst.get('exitCode', 'N/A')}")
        return {"status": "ok", "instance": inst}
    else:
        _print_error("get instance", args.id, data)
        return {"status": "error", "error": data}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_id(data: Any) -> str:
    """Try to extract a task/trigger/workflow ID from a response dict."""
    if not isinstance(data, dict):
        return "N/A"
    for key in ("sysId", "id", "taskId", "triggerId", "workflowId", "name"):
        val = data.get(key)
        if val:
            return str(val)
    return "N/A"


def _print_error(action: str, target: str, data: Any):
    """Print a formatted error message."""
    error = str(data)
    if isinstance(data, dict):
        error = data.get("error") or data.get("message") or json.dumps(data)
    print(f"❌ Failed to {action} '{target}'")
    print(f"   Error: {error}")
    print("   Verify your credentials, base_url, and Stonebranch UAC connectivity.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stonebranch UAC REST API CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sb_api.py create-task --name etl_job --type taskUnix --agent prod-lnx-01 --command /opt/etl.sh
  python sb_api.py create-task --name file_watcher --type taskFileMonitor --agent prod-lnx-01 --filename /data/in/*.csv
  python sb_api.py fetch-task --name etl_job
  python sb_api.py update-task --name etl_job --field command --value /opt/v2/etl.sh
  python sb_api.py create-trigger --name etl_cron --type triggerCron --task etl_job --cron "0 2 * * 1-5"
  python sb_api.py create-trigger --name etl_time --type triggerTime --task etl_job --time "02:00"
  python sb_api.py create-workflow --name nightly_wf --tasks "etl_job,backup_job,report_job" --dependencies "etl_job:success>backup_job,backup_job:success>report_job"
  python sb_api.py launch-job --name etl_job
  python sb_api.py launch-job --name etl_job --yes
  python sb_api.py list-tasks --type taskUnix
  python sb_api.py get-instance --id TI-9921
        """
    )
    parser.add_argument("--config", default="config/sb_config.yaml", help="Path to sb_config.yaml")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # create-task
    ct = subparsers.add_parser("create-task", help="Create a new task")
    ct.add_argument("--name",          required=True)
    ct.add_argument("--type",          required=True,
        help="taskUnix | taskWindows | taskFileMonitor | taskFileMonitorRemote | workflow | taskMonitor | etc.")
    ct.add_argument("--agent",         default=None, help="Universal Agent name")
    ct.add_argument("--command",       default=None, help="Command / script path (or use --filename for monitors)")
    ct.add_argument("--filename",      default=None, help="File path/pattern for File Monitor tasks")
    ct.add_argument("--credentials",   default=None, help="UAC credentials alias")
    ct.add_argument("--description",   default=None)
    ct.add_argument("--scan-interval", dest="scan_interval", default=None, type=int,
        help="Scan interval in seconds (for File Monitor tasks)")

    # fetch-task
    ft = subparsers.add_parser("fetch-task", help="Fetch task details")
    ft_group = ft.add_mutually_exclusive_group(required=True)
    ft_group.add_argument("--name", default=None)
    ft_group.add_argument("--id",   default=None)

    # update-task
    ut = subparsers.add_parser("update-task", help="Update a field on an existing task")
    ut.add_argument("--name",  required=True)
    ut.add_argument("--field", required=True, help="Field name to update (e.g. command)")
    ut.add_argument("--value", required=True, help="New value for the field")

    # list-tasks
    lt = subparsers.add_parser("list-tasks", help="List tasks")
    lt.add_argument("--type",   default=None, help="Filter by task type")
    lt.add_argument("--filter", default=None, help="Filter by task name (partial)")

    # create-trigger
    ctr = subparsers.add_parser("create-trigger", help="Create a trigger / schedule")
    ctr.add_argument("--name",     required=True)
    ctr.add_argument("--type",     default="triggerTime",
        help="triggerTime | triggerCron | triggerFileMonitor | triggerTemporary | triggerTaskMonitor")
    ctr.add_argument("--task",     default=None, help="Task name(s) to trigger (comma-separated)")
    ctr.add_argument("--cron",     default=None, help="Cron expression e.g. '0 2 * * 1-5'")
    ctr.add_argument("--time",     default=None, help="Simple time e.g. '02:30'")
    ctr.add_argument("--timezone", default=None, help="Timezone e.g. 'America/New_York'")
    ctr.add_argument("--filename", default=None, help="File pattern (for triggerFileMonitor)")
    ctr.add_argument("--agent",    default=None, help="Agent name (for triggerFileMonitor)")
    ctr.add_argument("--inactive", action="store_true", help="Create trigger in inactive state")
    ctr.add_argument("--description", default=None)

    # create-workflow
    cw = subparsers.add_parser("create-workflow", help="Create a workflow")
    cw.add_argument("--name",         required=True)
    cw.add_argument("--tasks",        required=True,
        help="Comma-separated task names e.g. 'task1,task2,task3'")
    cw.add_argument("--dependencies", default=None,
        help="Dependencies as 'source:condition>target' pairs e.g. 'task1:success>task2,task2:failure>task3'")
    cw.add_argument("--description",  default=None)

    # launch-job
    lj = subparsers.add_parser("launch-job", help="Launch a task immediately")
    lj_group = lj.add_mutually_exclusive_group(required=True)
    lj_group.add_argument("--name", default=None)
    lj_group.add_argument("--id",   default=None)
    lj.add_argument("--yes", "-y", action="store_true",
        help="Skip confirmation prompt (use with caution in production)")

    # get-instance
    gi = subparsers.add_parser("get-instance", help="Get task instance status")
    gi.add_argument("--id", required=True, help="Task Instance ID")

    return parser


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = _build_arg_parser()
    args   = parser.parse_args()

    # Load config
    try:
        config = _load_config(args.config)
    except (ValueError, FileNotFoundError) as exc:
        print(f"❌ Configuration error: {exc}")
        sys.exit(1)

    client = StoneBranchClient(config)

    # Dispatch commands
    command_map = {
        "create-task":     create_task,
        "fetch-task":      fetch_task,
        "update-task":     update_task,
        "list-tasks":      list_tasks,
        "create-trigger":  create_trigger,
        "create-workflow": create_workflow,
        "launch-job":      launch_job,
        "get-instance":    get_instance,
    }

    handler = command_map.get(args.command)
    if handler:
        result = handler(client, args)
        if result.get("status") in ("error",):
            sys.exit(1)
    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)
