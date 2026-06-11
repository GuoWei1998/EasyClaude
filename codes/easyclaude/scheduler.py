import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .background import NotificationQueue
from .config import WORKDIR


SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = WORKDIR / ".claude" / "cron.lock"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = {0, 30}
JITTER_OFFSET_MAX = 4


class CronLock:
    """PID-file lock to prevent multiple sessions from firing durable schedules."""

    def __init__(self, lock_path: Path = None):
        self.lock_path = lock_path or CRON_LOCK_FILE
        self.acquired = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                stored_pid = int(self.lock_path.read_text().strip())
                os.kill(stored_pid, 0)
                return False
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(str(os.getpid()))
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.lock_path.exists() and int(self.lock_path.read_text().strip()) == os.getpid():
                self.lock_path.unlink()
        except (ValueError, OSError):
            pass
        self.acquired = False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Return whether a 5-field cron expression matches a datetime."""

    if not validate_cron(expr):
        return False
    fields = expr.strip().split()
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(
        _field_matches(field, value, lo, hi)
        for field, value, (lo, hi) in zip(fields, values, ranges)
    )


def validate_cron(expr: str) -> bool:
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    try:
        for field, (lo, hi) in zip(fields, ranges):
            _validate_field(field, lo, hi)
        return True
    except ValueError:
        return False


def _validate_field(field: str, lo: int, hi: int) -> None:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError("step must be positive")
        if part == "*":
            continue
        if "-" in part:
            start, end = [int(piece) for piece in part.split("-", 1)]
            if not (lo <= start <= end <= hi):
                raise ValueError("range out of bounds")
            continue
        exact = int(part)
        if not lo <= exact <= hi:
            raise ValueError("exact value out of bounds")


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                return False
        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = [int(piece) for piece in part.split("-", 1)]
            if not (lo <= start <= end <= hi):
                continue
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            exact = int(part)
            if lo <= exact <= hi and exact == value:
                return True
    return False


class CronScheduler:
    """Schedule future prompts and inject them back into the agent loop."""

    def __init__(
        self,
        tasks_file: Path = None,
        lock_file: Path = None,
        check_interval_seconds: int = 1,
    ):
        self.tasks_file = tasks_file or SCHEDULED_TASKS_FILE
        self.lock = CronLock(lock_file or CRON_LOCK_FILE)
        self.check_interval_seconds = check_interval_seconds
        self.tasks = []
        self.notifications = NotificationQueue()
        self._stop_event = threading.Event()
        self._thread = None
        self._last_check_minute = None
        self._started = False

    def start(self) -> None:
        self.load_durable()
        if not self.lock.acquire():
            print("[Cron] Another EasyClaude session owns the scheduler lock; durable checks disabled here.")
            return
        self._thread = threading.Thread(target=self._check_loop, name="cron-scheduler", daemon=True)
        self._thread.start()
        self._started = True
        if self.tasks:
            print(f"[Cron] Loaded {len(self.tasks)} scheduled task(s)")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.lock.release()

    def create(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
    ) -> str:
        if not validate_cron(cron):
            return f"Error: invalid cron expression: {cron}"
        if not prompt.strip():
            return "Error: prompt is required"
        task = {
            "id": f"cron_{uuid.uuid4().hex[:8]}",
            "cron": cron,
            "prompt": prompt,
            "recurring": bool(recurring),
            "durable": bool(durable),
            "createdAt": time.time(),
            "last_fired": None,
            "jitter_offset": self._compute_jitter(cron) if recurring else 0,
        }
        self.tasks.append(task)
        if durable:
            self.save_durable()
        mode = "recurring" if task["recurring"] else "one-shot"
        store = "durable" if task["durable"] else "session-only"
        return f"Created scheduled task {task['id']} ({mode}, {store}): cron={cron}"

    def delete(self, task_id: str) -> str:
        before = len(self.tasks)
        self.tasks = [task for task in self.tasks if task["id"] != task_id]
        if len(self.tasks) == before:
            return f"Scheduled task not found: {task_id}"
        self.save_durable()
        return f"Deleted scheduled task {task_id}"

    def list_tasks(self) -> str:
        if not self.tasks:
            return "No scheduled tasks."
        lines = []
        for task in self.tasks:
            mode = "recurring" if task.get("recurring") else "one-shot"
            store = "durable" if task.get("durable") else "session"
            age_hours = (time.time() - float(task.get("createdAt") or time.time())) / 3600
            last = self._format_time(task.get("last_fired"))
            lines.append(
                f"{task['id']}  {task['cron']}  [{mode}/{store}] "
                f"age={age_hours:.1f}h last_fired={last}: {task['prompt'][:80]}"
            )
        return "\n".join(lines)

    def drain_notifications(self) -> list[dict]:
        return self.notifications.drain()

    def format_notifications(self, notifications: list[dict]) -> str:
        if not notifications:
            return ""
        parts = ["<scheduled-tasks>"]
        for item in notifications:
            parts.append(
                "\n".join(
                    [
                        f"task_id: {item.get('task_id')}",
                        f"cron: {item.get('cron')}",
                        f"fired_at: {item.get('fired_at')}",
                        "prompt:",
                        str(item.get("prompt", "")).strip(),
                    ]
                )
            )
        parts.append("</scheduled-tasks>")
        return "\n\n".join(parts)

    def detect_missed_tasks(self, lookback_hours: int = 24) -> list[dict]:
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            check = datetime.fromtimestamp(float(last_fired)) + timedelta(minutes=1)
            cap = min(now, datetime.fromtimestamp(float(last_fired)) + timedelta(hours=lookback_hours))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append(
                        {
                            "id": task["id"],
                            "cron": task["cron"],
                            "prompt": task["prompt"],
                            "missed_at": check.isoformat(timespec="minutes"),
                        }
                    )
                    break
                check += timedelta(minutes=1)
        return missed

    def load_durable(self) -> None:
        if not self.tasks_file.exists():
            return
        try:
            data = json.loads(self.tasks_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[Cron] Error loading scheduled tasks: {exc}")
            return
        self.tasks = [task for task in data if task.get("durable")]

    def save_durable(self) -> None:
        durable = [task for task in self.tasks if task.get("durable")]
        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        self.tasks_file.write_text(json.dumps(durable, ensure_ascii=False, indent=2) + "\n")

    def check_now(self, now: datetime = None) -> None:
        self._check_tasks(now or datetime.now())

    def _check_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)
            self._stop_event.wait(timeout=self.check_interval_seconds)

    def _check_tasks(self, now: datetime) -> None:
        remove_ids = set()
        for task in list(self.tasks):
            age_days = (time.time() - float(task.get("createdAt") or time.time())) / 86400
            if task.get("recurring") and age_days > AUTO_EXPIRY_DAYS:
                remove_ids.add(task["id"])
                print(f"[Cron] Auto-expired: {task['id']}")
                continue

            check_time = now - timedelta(minutes=int(task.get("jitter_offset") or 0))
            if not cron_matches(task["cron"], check_time):
                continue

            task["last_fired"] = time.time()
            self.notifications.push(
                {
                    "key": f"cron:{task['id']}:{now.strftime('%Y%m%d%H%M')}",
                    "task_id": task["id"],
                    "cron": task["cron"],
                    "prompt": task["prompt"],
                    "fired_at": now.isoformat(timespec="seconds"),
                }
            )
            print(f"[Cron] Fired: {task['id']}")
            if not task.get("recurring"):
                remove_ids.add(task["id"])

        if remove_ids:
            self.tasks = [task for task in self.tasks if task["id"] not in remove_ids]
        if remove_ids or any(task.get("durable") for task in self.tasks):
            self.save_durable()

    def _compute_jitter(self, cron: str) -> int:
        fields = cron.strip().split()
        if len(fields) != 5:
            return 0
        try:
            minute = int(fields[0])
        except ValueError:
            return 0
        if minute not in JITTER_MINUTES:
            return 0
        return (sum(ord(ch) for ch in cron) % JITTER_OFFSET_MAX) + 1

    @staticmethod
    def _format_time(value) -> str:
        if not value:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
