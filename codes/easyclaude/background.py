import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import WORKDIR


BACKGROUND_TASKS_DIR = WORKDIR / ".runtime-tasks"
BACKGROUND_TIMEOUT_SECONDS = 300
STALL_THRESHOLD_SECONDS = 120
MAX_NOTIFICATION_CHARS = 8000


class NotificationQueue:
    """Thread-safe notification queue with simple same-key folding."""

    def __init__(self):
        self._items = []
        self._lock = threading.Lock()

    def push(self, item: dict) -> None:
        key = item.get("key")
        with self._lock:
            if key:
                self._items = [existing for existing in self._items if existing.get("key") != key]
            self._items.append(item)

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items


class BackgroundManager:
    """Run long shell commands outside the foreground agent turn."""

    def __init__(self, tasks_dir: Path = None):
        self.tasks_dir = tasks_dir or BACKGROUND_TASKS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.notifications = NotificationQueue()

    def run(self, command: str) -> str:
        if not command.strip():
            return "Error: command is required"
        task_id = f"bg_{uuid.uuid4().hex[:12]}"
        record = {
            "id": task_id,
            "command": command,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "returncode": None,
            "log_path": str(self._log_path(task_id)),
        }
        self._write_record(task_id, record)
        thread = threading.Thread(
            target=self._execute,
            args=(task_id, command),
            name=f"background-task-{task_id}",
            daemon=True,
        )
        thread.start()
        return (
            f"Started background task {task_id}\n"
            f"Log: {record['log_path']}\n"
            "Use check_background with this task_id to inspect status."
        )

    def check(self, task_id: str = None) -> str:
        if task_id:
            record = self._read_record(task_id)
            if not record:
                return f"Background task not found: {task_id}"
            return self._format_record(record)

        records = [self._read_record(path.stem) for path in sorted(self.tasks_dir.glob("bg_*.json"))]
        records = [record for record in records if record]
        if not records:
            return "(no background tasks)"
        return "\n\n".join(self._format_record(record) for record in records)

    def drain_notifications(self) -> list[dict]:
        return self.notifications.drain()

    def detect_stalled(self, threshold_seconds: int = STALL_THRESHOLD_SECONDS) -> list[dict]:
        now = time.time()
        stalled = []
        for path in sorted(self.tasks_dir.glob("bg_*.json")):
            record = self._read_record(path.stem)
            if not record or record.get("status") != "running":
                continue
            age = now - float(record.get("started_at") or now)
            if age >= threshold_seconds:
                stalled.append(record)
        return stalled

    def format_notifications(self, notifications: list[dict]) -> str:
        if not notifications:
            return ""
        parts = ["<background-results>"]
        for item in notifications:
            output = str(item.get("output", "")).strip() or "(no output)"
            if len(output) > MAX_NOTIFICATION_CHARS:
                output = output[:MAX_NOTIFICATION_CHARS] + "\n... (truncated)"
            parts.append(
                "\n".join(
                    [
                        f"task_id: {item.get('task_id')}",
                        f"status: {item.get('status')}",
                        f"returncode: {item.get('returncode')}",
                        f"log_path: {item.get('log_path')}",
                        "output:",
                        output,
                    ]
                )
            )
        parts.append("</background-results>")
        return "\n\n".join(parts)

    def _execute(self, task_id: str, command: str) -> None:
        record = self._read_record(task_id) or {}
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=BACKGROUND_TIMEOUT_SECONDS,
            )
            output = (result.stdout + result.stderr).strip()
            status = "completed" if result.returncode == 0 else "failed"
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") + (exc.stderr or "")).strip()
            output = f"{output}\nError: Timeout ({BACKGROUND_TIMEOUT_SECONDS}s)".strip()
            status = "timeout"
            returncode = None
        except (FileNotFoundError, OSError) as exc:
            output = f"Error: {exc}"
            status = "failed"
            returncode = None

        self._log_path(task_id).write_text(output or "(no output)")
        record.update(
            {
                "status": status,
                "finished_at": time.time(),
                "returncode": returncode,
                "output_preview": (output or "(no output)")[:2000],
            }
        )
        self._write_record(task_id, record)
        self.notifications.push(
            {
                "key": task_id,
                "task_id": task_id,
                "status": status,
                "returncode": returncode,
                "log_path": str(self._log_path(task_id)),
                "output": (output or "(no output)")[:4000],
            }
        )

    def _record_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _log_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.log"

    def _read_record(self, task_id: str) -> dict | None:
        path = self._record_path(task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def _write_record(self, task_id: str, record: dict) -> None:
        self._record_path(task_id).write_text(json.dumps(record, ensure_ascii=False, indent=2))

    def _format_record(self, record: dict) -> str:
        started = self._format_time(record.get("started_at"))
        finished = self._format_time(record.get("finished_at"))
        lines = [
            f"[{record.get('status')}] {record.get('id')}",
            f"command: {record.get('command')}",
            f"started_at: {started}",
            f"finished_at: {finished}",
            f"returncode: {record.get('returncode')}",
            f"log_path: {record.get('log_path')}",
        ]
        preview = record.get("output_preview")
        if preview:
            lines.extend(["output_preview:", preview])
        return "\n".join(lines)

    @staticmethod
    def _format_time(value) -> str:
        if not value:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
