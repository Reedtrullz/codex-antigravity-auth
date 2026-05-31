import json
import os
import threading
from pathlib import Path
from typing import Any
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home

_accounts_lock = threading.RLock()

def get_accounts_json_path() -> Path:
    p = Path(os.path.expanduser(ANTIGRAVITY_ACCOUNTS_FILE))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def load_accounts() -> dict[str, Any]:
    with _accounts_lock:
        path = get_accounts_json_path()
        if not path.is_file():
            return {"accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}
        try:
            with open(path, "r") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                data.setdefault("accounts", [])
                data.setdefault("activeIndex", 0)
                data.setdefault("activeIndexByFamily", {"claude": 0, "gemini": 0})
                return data
        except Exception:
            return {"accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}

def save_accounts(data: dict[str, Any]) -> None:
    with _accounts_lock:
        path = get_accounts_json_path()
        try:
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, path)
        except Exception as e:
            raise RuntimeError(f"Failed to save accounts file: {e}")
