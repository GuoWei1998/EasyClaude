import json
import threading
import time
import uuid
from pathlib import Path

from .config import MODEL, WORKDIR, client
from .tools import run_bash, run_edit, run_read, run_write


TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TEAM_CONFIG_FILE = TEAM_DIR / "config.json"
REQUESTS_DIR = TEAM_DIR / "requests"
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval",
    "plan_approval_response",
}
TEAMMATE_IDLE_POLL_SECONDS = 1
TEAMMATE_MAX_MODEL_TURNS = 50


class MessageBus:
    """Append-only JSONL inboxes for named teammates."""

    def __init__(self, inbox_dir: Path = None):
        self.dir = inbox_dir or INBOX_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict = None,
    ) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid message type '{msg_type}'. Valid: {sorted(VALID_MSG_TYPES)}"
        if not to.strip():
            return "Error: recipient is required"
        message = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            message.update(extra)
        with self._lock:
            inbox_path = self.dir / f"{to}.jsonl"
            with inbox_path.open("a") as handle:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list[dict]:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        with self._lock:
            lines = inbox_path.read_text().splitlines()
            inbox_path.write_text("")
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                messages.append({"type": "message", "from": "system", "content": line})
        return messages

    def broadcast(self, sender: str, content: str, teammates: list[str]) -> str:
        count = 0
        for name in teammates:
            if name == sender:
                continue
            self.send(sender, name, content, "broadcast")
            count += 1
        return f"Broadcast to {count} teammate(s)"


class RequestStore:
    """Durable request records for team protocols."""

    def __init__(self, requests_dir: Path = None):
        self.dir = requests_dir or REQUESTS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, record: dict) -> dict:
        request_id = record["request_id"]
        with self._lock:
            self._path(request_id).write_text(json.dumps(record, ensure_ascii=False, indent=2))
        return record

    def get(self, request_id: str) -> dict | None:
        path = self._path(request_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def update(self, request_id: str, **changes) -> dict | None:
        with self._lock:
            record = self.get(request_id)
            if not record:
                return None
            record.update(changes)
            record["updated_at"] = time.time()
            self._path(request_id).write_text(json.dumps(record, ensure_ascii=False, indent=2))
            return record

    def list_all(self) -> str:
        records = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        if not records:
            return "No protocol requests."
        lines = []
        for record in records:
            lines.append(
                f"{record.get('request_id')} [{record.get('kind')}/{record.get('status')}] "
                f"{record.get('from')} -> {record.get('to')}"
            )
        return "\n".join(lines)

    def _path(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"


class TeammateManager:
    """Persistent teammate registry plus worker-thread launcher."""

    def __init__(self, team_dir: Path = None, bus: MessageBus = None, request_store: RequestStore = None):
        self.dir = team_dir or TEAM_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.bus = bus or MessageBus(self.dir / "inbox")
        self.request_store = request_store or RequestStore(self.dir / "requests")
        self.config = self._load_config()
        self.threads = {}
        self.stop_events = {}
        self._lock = threading.Lock()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        name = name.strip()
        if not name:
            return "Error: teammate name is required"
        with self._lock:
            member = self._find_member(name)
            if member and member.get("status") not in {"idle", "shutdown", "error"}:
                return f"Error: '{name}' is currently {member.get('status')}"
            if member:
                member["role"] = role
                member["status"] = "working"
            else:
                self.config["members"].append({"name": name, "role": role, "status": "working"})
            self._save_config()

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt, stop_event),
            name=f"teammate-{name}",
            daemon=True,
        )
        self.stop_events[name] = stop_event
        self.threads[name] = thread
        thread.start()
        return f"Spawned teammate '{name}' (role: {role})"

    def shutdown(self, name: str) -> str:
        event = self.stop_events.get(name)
        if event:
            event.set()
        with self._lock:
            member = self._find_member(name)
            if not member:
                return f"Teammate not found: {name}"
            member["status"] = "shutdown"
            self._save_config()
        return f"Shutdown requested for teammate '{name}'"

    def request_shutdown(self, teammate: str) -> str:
        if not self._find_member(teammate):
            return f"Teammate not found: {teammate}"
        request_id = f"req_{uuid.uuid4().hex[:8]}"
        self.request_store.create(
            {
                "request_id": request_id,
                "kind": "shutdown",
                "from": "lead",
                "to": teammate,
                "status": "pending",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        )
        self.bus.send(
            "lead",
            teammate,
            "Please shut down gracefully. Reply with shutdown_response.",
            "shutdown_request",
            {"request_id": request_id},
        )
        return f"Shutdown request {request_id} sent to '{teammate}' (status: pending)"

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        request = self.request_store.get(request_id)
        if not request:
            return f"Error: Unknown plan request_id '{request_id}'"
        if request.get("kind") != "plan_approval":
            return f"Error: Request {request_id} is not a plan approval request"
        status = "approved" if approve else "rejected"
        self.request_store.update(
            request_id,
            status=status,
            reviewed_by="lead",
            resolved_at=time.time(),
            feedback=feedback,
        )
        self.bus.send(
            "lead",
            request["from"],
            feedback,
            "plan_approval_response",
            {"request_id": request_id, "approve": approve, "feedback": feedback},
        )
        return f"Plan {status} for '{request['from']}'"

    def check_request(self, request_id: str) -> str:
        record = self.request_store.get(request_id)
        if not record:
            return json.dumps({"error": "not found", "request_id": request_id}, ensure_ascii=False)
        return json.dumps(record, ensure_ascii=False, indent=2)

    def list_requests(self) -> str:
        return self.request_store.list_all()

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config.get('team_name', 'default')}"]
        for member in self.config["members"]:
            lines.append(f"  {member['name']} ({member['role']}): {member['status']}")
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [member["name"] for member in self.config["members"]]

    def lead_inbox(self) -> list[dict]:
        return self.bus.read_inbox("lead")

    def format_inbox(self, messages: list[dict]) -> str:
        if not messages:
            return ""
        return "<team-inbox>\n" + json.dumps(messages, ensure_ascii=False, indent=2) + "\n</team-inbox>"

    def _teammate_loop(self, name: str, role: str, prompt: str, stop_event: threading.Event) -> None:
        system = (
            f"You are '{name}', a persistent teammate with role '{role}' at {WORKDIR}. "
            "Use send_message to report findings to lead or coordinate with teammates. "
            "Submit plans with plan_approval before major work. "
            "Respond to shutdown_request messages with shutdown_response. "
            "When you have no immediate work, provide a short status update."
        )
        messages = [{"role": "user", "content": prompt}]
        turns = 0
        should_exit = False
        try:
            while not stop_event.is_set() and not should_exit and turns < TEAMMATE_MAX_MODEL_TURNS:
                inbox = self.bus.read_inbox(name)
                for item in inbox:
                    messages.append({"role": "user", "content": self.format_inbox([item])})
                if turns > 0 and not inbox:
                    self._set_status(name, "idle")
                    stop_event.wait(timeout=TEAMMATE_IDLE_POLL_SECONDS)
                    continue

                self._set_status(name, "working")
                response = client.messages.create(
                    model=MODEL,
                    system=system,
                    messages=messages,
                    tools=self._teammate_tools(),
                    max_tokens=8000,
                )
                turns += 1
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    self.bus.send(name, "lead", self._extract_text(response.content) or "(no summary)")
                    continue

                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output = self._exec(name, block.name, dict(block.input or {}))
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)[:50000],
                        }
                    )
                    if block.name == "shutdown_response" and dict(block.input or {}).get("approve"):
                        should_exit = True
                if results:
                    messages.append({"role": "user", "content": results})
        except Exception as exc:
            self.bus.send(name, "lead", f"Teammate error: {exc}")
            self._set_status(name, "error")
            return
        self._set_status(name, "shutdown" if stop_event.is_set() or should_exit else "idle")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "read_file":
            return run_read(args["path"], args.get("limit"))
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return self.bus.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(self.bus.read_inbox(sender), ensure_ascii=False, indent=2)
        if tool_name == "shutdown_response":
            request_id = args["request_id"]
            approve = bool(args["approve"])
            status = "approved" if approve else "rejected"
            updated = self.request_store.update(
                request_id,
                status=status,
                resolved_by=sender,
                resolved_at=time.time(),
                response={"approve": approve, "reason": args.get("reason", "")},
            )
            if not updated:
                return f"Error: Unknown shutdown request {request_id}"
            self.bus.send(
                sender,
                "lead",
                args.get("reason", ""),
                "shutdown_response",
                {"request_id": request_id, "approve": approve},
            )
            return f"Shutdown {status}"
        if tool_name == "plan_approval":
            request_id = f"req_{uuid.uuid4().hex[:8]}"
            plan = args["plan"]
            self.request_store.create(
                {
                    "request_id": request_id,
                    "kind": "plan_approval",
                    "from": sender,
                    "to": "lead",
                    "status": "pending",
                    "plan": plan,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            )
            self.bus.send(
                sender,
                "lead",
                plan,
                "plan_approval",
                {"request_id": request_id, "plan": plan},
            )
            return f"Plan submitted (request_id={request_id}). Waiting for lead approval."
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list[dict]:
        return [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read file contents.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write content to file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
            {
                "name": "edit_file",
                "description": "Replace exact text in file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
            {
                "name": "send_message",
                "description": "Send a message to a teammate or lead.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                        "msg_type": {"type": "string", "enum": sorted(VALID_MSG_TYPES)},
                    },
                    "required": ["to", "content"],
                },
            },
            {
                "name": "read_inbox",
                "description": "Read and drain your inbox.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "shutdown_response",
                "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["request_id", "approve"],
                },
            },
            {
                "name": "plan_approval",
                "description": "Submit a plan for lead approval before major work.",
                "input_schema": {
                    "type": "object",
                    "properties": {"plan": {"type": "string"}},
                    "required": ["plan"],
                },
            },
        ]

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {"team_name": "default", "members": []}
        try:
            return json.loads(self.config_path.read_text())
        except json.JSONDecodeError:
            return {"team_name": "default", "members": []}

    def _save_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2))

    def _find_member(self, name: str) -> dict | None:
        for member in self.config["members"]:
            if member["name"] == name:
                return member
        return None

    def _set_status(self, name: str, status: str) -> None:
        with self._lock:
            member = self._find_member(name)
            if member:
                member["status"] = status
                self._save_config()

    @staticmethod
    def _extract_text(blocks) -> str:
        return "".join(block.text for block in blocks if hasattr(block, "text")).strip()
