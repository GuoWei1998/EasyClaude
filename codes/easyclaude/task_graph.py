import json
import threading
import time
from pathlib import Path

from .config import WORKDIR


TASKS_DIR = WORKDIR / ".tasks"
TASK_STATUSES = ("pending", "in_progress", "completed", "deleted")
TASK_REMINDER_INTERVAL = 3
_claim_lock = threading.Lock()


class TaskManager:
    """Persistent task graph store.

    Tasks are durable work records on disk. They are not worker processes.
    Dependency edges are stored both ways:
    - blockedBy: tasks that must complete first
    - blocks: tasks this task unlocks later
    """

    def __init__(self, tasks_dir: Path = None):
        self.dir = tasks_dir or TASKS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = []
        for path in self.dir.glob("task_*.json"):
            try:
                ids.append(int(path.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict) -> None:
        self._path(task["id"]).write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def _all_tasks(self) -> list[dict]:
        tasks = []
        for path in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(path.read_text()))
        return tasks

    def create(self, subject: str, description: str = "", blocked_by: list[int] = None, owner: str = "") -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],
            "blocks": [],
            "owner": owner,
            "worktree": None,
        }
        self._save(task)
        self._next_id += 1
        if blocked_by:
            self.update(task["id"], add_blocked_by=blocked_by)
            task = self._load(task["id"])
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(
        self,
        task_id: int,
        status: str = None,
        owner: str = None,
        add_blocked_by: list[int] = None,
        add_blocks: list[int] = None,
        remove_blocked_by: list[int] = None,
    ) -> str:
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status

        for blocker_id in add_blocked_by or []:
            self._assert_task_exists(blocker_id)
            self._assert_no_cycle(task_id, blocker_id)
            if blocker_id not in task["blockedBy"]:
                task["blockedBy"].append(blocker_id)
            blocker = self._load(blocker_id)
            if task_id not in blocker["blocks"]:
                blocker["blocks"].append(task_id)
                self._save(blocker)

        for blocked_id in add_blocks or []:
            self._assert_task_exists(blocked_id)
            self._assert_no_cycle(blocked_id, task_id)
            if blocked_id not in task["blocks"]:
                task["blocks"].append(blocked_id)
            blocked = self._load(blocked_id)
            if task_id not in blocked["blockedBy"]:
                blocked["blockedBy"].append(task_id)
                self._save(blocked)

        for blocker_id in remove_blocked_by or []:
            if blocker_id in task["blockedBy"]:
                task["blockedBy"].remove(blocker_id)
            try:
                blocker = self._load(blocker_id)
                if task_id in blocker["blocks"]:
                    blocker["blocks"].remove(task_id)
                    self._save(blocker)
            except ValueError:
                pass

        self._save(task)
        if status == "completed":
            self._clear_dependency(task_id)
            task = self._load(task_id)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [task for task in self._all_tasks() if task.get("status") != "deleted"]
        if not tasks:
            return "No tasks."
        lines = []
        for task in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
                "deleted": "[-]",
            }.get(task["status"], "[?]")
            blocked = f" blockedBy={task['blockedBy']}" if task.get("blockedBy") else ""
            blocks = f" blocks={task['blocks']}" if task.get("blocks") else ""
            owner = f" owner={task['owner']}" if task.get("owner") else ""
            worktree = f" wt:{task['worktree']}" if task.get("worktree") else ""
            lines.append(f"{marker} #{task['id']}: {task['subject']}{owner}{blocked}{blocks}{worktree}")
        ready = self.ready_ids()
        if ready:
            lines.append(f"\nReady: {ready}")
        return "\n".join(lines)

    def ready_ids(self) -> list[int]:
        ready = []
        for task in self._all_tasks():
            if task.get("status") != "pending":
                continue
            if task.get("blockedBy"):
                continue
            ready.append(task["id"])
        return ready

    def unclaimed(self, role: str = None) -> list[dict]:
        return [
            task for task in self._all_tasks()
            if self.is_claimable(task, role)
        ]

    def claim(self, task_id: int, owner: str, role: str = None, source: str = "manual") -> str:
        with _claim_lock:
            task = self._load(task_id)
            if not self.is_claimable(task, role):
                return f"Error: Task {task_id} is not claimable for role={role or '(any)'}"
            task["owner"] = owner
            task["status"] = "in_progress"
            task["claimed_at"] = time.time()
            task["claim_source"] = source
            self._save(task)
        self._append_claim_event(
            {
                "event": "task.claimed",
                "task_id": task_id,
                "owner": owner,
                "role": role,
                "source": source,
                "ts": time.time(),
            }
        )
        return f"Claimed task #{task_id} for {owner} via {source}"

    def claim_first(self, owner: str, role: str = None, source: str = "auto") -> tuple[dict | None, str]:
        for task in self.unclaimed(role):
            result = self.claim(task["id"], owner, role, source)
            if not result.startswith("Error:"):
                return self._load(task["id"]), result
        return None, "No claimable tasks."

    def bind_worktree(self, task_id: int, worktree: str) -> str:
        task = self._load(task_id)
        task["worktree"] = worktree
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def is_claimable(self, task: dict, role: str = None) -> bool:
        return (
            task.get("status") == "pending"
            and not task.get("owner")
            and not task.get("blockedBy")
            and self._task_allows_role(task, role)
        )

    def has_tasks(self) -> bool:
        return any(task.get("status") != "deleted" for task in self._all_tasks())

    def reminder(self) -> str | None:
        if not self.has_tasks():
            return None
        ready = self.ready_ids()
        ready_note = f" Ready task IDs: {ready}." if ready else ""
        return (
            "<reminder>"
            "You have a persistent task graph. Before continuing, call task_list "
            "and use task_update if task status, ownership, or dependencies changed."
            f"{ready_note}"
            "</reminder>"
        )

    def _assert_task_exists(self, task_id: int) -> None:
        self._load(task_id)

    def _task_allows_role(self, task: dict, role: str = None) -> bool:
        required_role = task.get("claim_role") or task.get("required_role") or ""
        if not required_role:
            return True
        return bool(role) and role == required_role

    def _append_claim_event(self, payload: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "claim_events.jsonl").open("a") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _clear_dependency(self, completed_id: int) -> None:
        for task in self._all_tasks():
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def _assert_no_cycle(self, task_id: int, blocker_id: int) -> None:
        """Reject adding blocker_id -> task_id if task_id already reaches blocker_id."""
        if task_id == blocker_id:
            raise ValueError("A task cannot depend on itself")
        if self._reaches(task_id, blocker_id):
            raise ValueError(f"Dependency would create a cycle: {blocker_id} -> {task_id}")

    def _reaches(self, start_id: int, target_id: int) -> bool:
        seen = set()
        stack = [start_id]
        while stack:
            current_id = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            if current_id == target_id:
                return True
            try:
                task = self._load(current_id)
            except ValueError:
                continue
            stack.extend(task.get("blocks", []))
        return False
