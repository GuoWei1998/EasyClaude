import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPT_DIR.parent
load_dotenv(dotenv_path=SCRIPT_DIR / ".env", override=True)

PLAN_REMINDER_INTERVAL = 3
WORKDIR = Path.cwd()
MODEL = os.getenv("MODEL_ID")
SKILLS_DIR = WORKDIR / "skills"
if not SKILLS_DIR.exists():
    SKILLS_DIR = PROJECT_ROOT / "skills"

if not MODEL:
    raise RuntimeError("Missing MODEL_ID environment variable")

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
    base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
)
