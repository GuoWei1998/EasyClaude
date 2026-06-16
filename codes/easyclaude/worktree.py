import json
import re
import subprocess
import time
from pathlib import Path

from .config import WORKDIR


WORKTREES_DIR = WORKDIR / ".worktrees"
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class WorktreeManager:
    """Manage isolated git worktrees and task bindings."""

    def __init__(self, task_manager=None, worktrees_dir: Path = None):
        self.task_manager = task_manager
        self.dir = worktrees_dir or WORKTREES_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def validate_name(self, name: str) -> str | None:
        if not name:
            return "Worktree name cannot be empty"
        if name in {".", ".."}:
            return f"'{name}' is not a valid worktree name"
        if not VALID_WORKTREE_NAME.match(name):
            return (
                f"Invalid worktree name '{name}': only letters, digits, dots, "
                "underscores, and dashes are allowed (1-64 chars)"
            )
        return None

    def path_for(self, name: str) -> Path:
        return self.dir / name

    def create(self, name: str, task_id: int = None) -> str:
        err = self.validate_name(name)
        if err:
            return f"Error: {err}"
        path = self.path_for(name)
        if path.exists():
            return f"Worktree '{name}' already exists at {path}"

        ok, output = self.run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
        if not ok:
            return f"Git error: {output}"

        if task_id is not None:
            bound = self.bind_task(task_id, name)
            if bound.startswith("Error:"):
                return f"Worktree '{name}' created at {path}, but binding failed: {bound}"
        self.log_event("create", name, task_id)
        return f"Worktree '{name}' created at {path}"

    def bind_task(self, task_id: int, name: str) -> str:
        err = self.validate_name(name)
        if err:
            return f"Error: {err}"
        if not self.path_for(name).exists():
            return f"Error: Worktree '{name}' not found"
        if not self.task_manager:
            return "Error: task manager is not configured"
        return self.task_manager.bind_worktree(task_id, name)

    def remove(self, name: str, discard_changes: bool = False) -> str:
        err = self.validate_name(name)
        if err:
            return f"Error: {err}"
        path = self.path_for(name)
        if not path.exists():
            return f"Worktree '{name}' not found"

        if not discard_changes:
            files, commits = self.count_changes(path)
            if files < 0:
                return (
                    f"Cannot verify worktree '{name}' status. "
                    "Use discard_changes=true to force removal."
                )
            if files > 0 or commits > 0:
                return (
                    f"Worktree '{name}' has {files} uncommitted file(s) "
                    f"and {commits} unpushed commit(s). Use discard_changes=true "
                    "to force removal, or keep_worktree to preserve it for review."
                )

        ok, output = self.run_git(["worktree", "remove", str(path), "--force"])
        if not ok:
            return f"Git error: {output}"
        self.run_git(["branch", "-D", f"wt/{name}"])
        self.log_event("remove", name)
        return f"Worktree '{name}' removed"

    def keep(self, name: str) -> str:
        err = self.validate_name(name)
        if err:
            return f"Error: {err}"
        self.log_event("keep", name)
        return f"Worktree '{name}' kept for review (branch: wt/{name})"

    def list_all(self) -> str:
        entries = [path for path in sorted(self.dir.iterdir()) if path.is_dir()]
        if not entries:
            return "No worktrees."
        return "\n".join(f"{path.name}: {path}" for path in entries)

    def log_event(self, event_type: str, worktree_name: str, task_id: int = None) -> None:
        event = {
            "type": event_type,
            "worktree": worktree_name,
            "task_id": task_id,
            "ts": time.time(),
        }
        with (self.dir / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def run_git(self, args: list[str]) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False, "Error: git timeout"
        except (FileNotFoundError, OSError) as exc:
            return False, f"Error: {exc}"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, (output[:5000] if output else "(no output)")

    def count_changes(self, path: Path) -> tuple[int, int]:
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            files = len([line for line in status.stdout.splitlines() if line.strip()])
            commits = subprocess.run(
                ["git", "log", "@{push}..HEAD", "--oneline"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            ahead = len([line for line in commits.stdout.splitlines() if line.strip()])
            return files, ahead
        except Exception:
            return -1, -1
