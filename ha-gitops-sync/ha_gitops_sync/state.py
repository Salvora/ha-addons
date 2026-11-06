"""What every action ends in, and /data/state.json: the panel's two switches, the last 50 results, and the deploy
whose config check is running."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

HISTORY = 50
KEPT_OUTPUT = 4000


class Switch(StrEnum):
    AUTO_DEPLOY = "auto_deploy"
    CAPTURE = "capture"


class Outcome(StrEnum):
    """The word every answer carries, with the HTTP status the signed API gives it."""

    status: int

    def __new__(cls, word: str, status: int):
        member = str.__new__(cls, word)
        member._value_ = word
        member.status = status
        return member

    DEPLOYED = "deployed", 200
    ALREADY_LIVE = "already-live", 200
    CHECKED = "checked", 200
    RELOADED = "reloaded", 200
    RESTARTED = "restarted", 200
    OK = "ok", 200
    BAD_REQUEST = "bad-request", 400
    UNAUTHORIZED = "unauthorized", 401
    NOT_FOUND = "not-found", 404
    NOT_ON_MAIN = "not-on-main", 409
    CHECKS_NOT_PASSED = "checks-not-passed", 409
    CONFLICT = "conflict", 409
    DIRTY = "dirty", 409
    CHECK_FAILED = "check-failed", 422
    PAUSED = "paused", 423
    ERROR = "error", 500
    UNAVAILABLE = "unavailable", 503


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    message: str
    live: str = ""
    output: str = ""
    seconds: int | None = None
    pull_request: str = ""
    files: tuple[str, ...] = ()

    def answer(self) -> dict:
        extras = {
            "live": self.live,
            "output": self.output,
            "seconds": self.seconds,
            "pull_request": self.pull_request,
            "files": list(self.files),
        }
        return {"outcome": str(self.outcome), "message": self.message} | {
            key: value for key, value in extras.items() if value not in ("", None, [])
        }


@dataclass(frozen=True)
class Entry:
    at: float
    action: str
    by: str
    result: Result
    link: str = ""


@dataclass(frozen=True)
class Placed:
    """A deploy whose config check is running, and previous, the commit to put back if the check never ends."""

    previous: str
    sha: str
    by: str
    run_url: str = ""
    impact: str = ""


def _entry(saved: dict) -> Entry:
    result = saved["result"] | {
        "outcome": Outcome(saved["result"]["outcome"]),
        "files": tuple(saved["result"]["files"]),
    }
    return Entry(**saved | {"result": Result(**result)})


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        saved = json.loads(path.read_text()) if path.exists() else {}
        self._switches = dict.fromkeys(Switch, False) | saved.get("switches", {})
        self._history = [_entry(item) for item in saved.get("history", [])]
        self._placed = Placed(**saved["placed"]) if saved.get("placed") else None

    def _save(self) -> None:
        saved = {
            "switches": self._switches,
            "history": [asdict(e) for e in self._history],
            "placed": asdict(self._placed) if self._placed else None,
        }
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as file:
            json.dump(saved, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, self.path)

    def is_on(self, switch: Switch) -> bool:
        with self._lock:
            return self._switches[switch]

    def set(self, switch: Switch, on: bool) -> None:
        with self._lock:
            self._switches[switch] = on
            self._save()

    def record(self, action: str, result: Result, by: str, link: str = "") -> None:
        kept = replace(result, output=result.output[-KEPT_OUTPUT:])
        with self._lock:
            self._history = [Entry(time.time(), action, by, kept, link), *self._history][:HISTORY]
            self._save()

    def placed(self) -> Placed | None:
        with self._lock:
            return self._placed

    def set_placed(self, placed: Placed | None) -> None:
        with self._lock:
            self._placed = placed
            self._save()

    def history(self) -> list[Entry]:
        """Newest first."""
        with self._lock:
            return list(self._history)
