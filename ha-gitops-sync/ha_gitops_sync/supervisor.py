"""The Supervisor's API and, through its proxy, Home Assistant Core's: config check, restart, reloads, events."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ha_gitops_sync import api
from ha_gitops_sync.api import ApiError

log = logging.getLogger(__name__)

URL = "http://supervisor"
ESPHOME = "5c53de3b_esphome"
CHECK_TIMEOUT = 240
RELOAD_TIMEOUT = 240
RESTART_TIMEOUT = 870
SECRET_FILES = ("secrets.yaml", "esphome/secrets.yaml")
SECRET = re.compile(r"[^\s#][^:]*:\s+(?P<value>.*\S)")
NOT_SECRETS = frozenset({"true", "false", "yes", "no", "null", "~"})
SECRET_MIN_LENGTH = 4


class Unavailable(RuntimeError):
    """The Supervisor or Home Assistant Core did not answer as expected; status 0 when nothing answered."""

    def __init__(self, message: str, status: int):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.message = message
        self.status = status


class CheckFailed(RuntimeError):
    def __init__(self, output: str):
        super().__init__("the config check failed")
        self.output = output


class Supervisor:
    def __init__(self, token: str, root: Path, url: str = URL):
        self.root = root
        self.url = url
        self._token = token

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = api.TIMEOUT):
        try:
            return api.call(method, f"{self.url}{path}", f"Bearer {self._token}", body, timeout)
        except ApiError as e:
            raise Unavailable(e.message, e.status) from None

    def check(self) -> None:
        """Runs check_config on the files in /config; the Supervisor answers 400 with its output when they fail."""
        try:
            self._call("POST", "/core/check", timeout=CHECK_TIMEOUT)
        except Unavailable as e:
            if e.status == 400:
                raise CheckFailed(self._redact(e.message)) from None
            raise

    def _redact(self, output: str) -> str:
        """The output quotes files no CI job has masked yet, so the secrets' current values become ***."""
        for value in sorted(self._secrets(), key=len, reverse=True):
            output = output.replace(value, "***")
        return output

    def _secrets(self) -> set[str]:
        found = set()
        for name in SECRET_FILES:
            try:
                text = (self.root / name).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            for line in text.splitlines():
                if match := SECRET.fullmatch(line):
                    value = match["value"]
                    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                        value = value[1:-1]
                    if value.lower() not in NOT_SECRETS and len(value) >= SECRET_MIN_LENGTH:
                        found.add(value)
        return found

    def restart(self) -> None:
        """Blocks until Core is back."""
        try:
            self._call("POST", "/core/restart", timeout=RESTART_TIMEOUT)
        except Unavailable as e:
            if e.status:
                raise
            raise Unavailable(
                f"Home Assistant did not come back within {RESTART_TIMEOUT} s; the restart may still be in progress", 0
            ) from None

    def services(self, timeout: float = api.TIMEOUT) -> set[str]:
        """Every service Core offers, as "domain.service"."""
        domains = self._call("GET", "/core/api/services", timeout=timeout)
        return {f"{d['domain']}.{service}" for d in domains for service in d["services"]}

    def call_service(self, domain: str, service: str) -> None:
        self._call("POST", f"/core/api/services/{domain}/{service}", {}, timeout=RELOAD_TIMEOUT)

    def fire_event(self, event: str, data: dict) -> None:
        """Events are for whoever listens; one that cannot be fired is logged and the action goes on."""
        try:
            self._call("POST", f"/core/api/events/{event}", data)
        except Unavailable as e:
            log.warning("event %s not fired: %s", event, e)

    def addon_version(self, slug: str) -> str | None:
        """The installed version of an add-on, None when it is not installed or its info cannot be read."""
        try:
            return self._call("GET", f"/addons/{slug}/info")["data"].get("version")
        except Unavailable as e:
            if e.status != 404:
                log.warning(
                    "no version for %s (status %s: %s); this add-on's role may not be allowed to read another "
                    "add-on's info",
                    slug,
                    e.status,
                    e.message,
                )
            return None
