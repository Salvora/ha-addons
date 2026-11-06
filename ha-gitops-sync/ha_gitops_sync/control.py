"""Check, reload and restart (plan §3.4 to §3.6); reload and restart run only after the config check passed."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import NamedTuple

from ha_gitops_sync.state import Outcome, Result, State
from ha_gitops_sync.supervisor import CheckFailed, Supervisor, Unavailable

log = logging.getLogger(__name__)


class Reload(NamedTuple):
    domain: str
    service: str
    label: str


NAMED_RELOADS = {
    "all": Reload("homeassistant", "reload_all", "All YAML"),
    "core": Reload("homeassistant", "reload_core_config", "Core (location, customize)"),
    "themes": Reload("frontend", "reload_themes", "Themes"),
}
DOMAIN = re.compile(r"[a-z0-9_]+")


def reload_service(target: str) -> tuple[str, str]:
    if not (isinstance(target, str) and DOMAIN.fullmatch(target)):
        raise ValueError("target must be all, core, themes or a domain such as automation")
    if named := NAMED_RELOADS.get(target):
        return named.domain, named.service
    return target, "reload"


def reload_targets(services: set[str]) -> list[str]:
    """What the panel offers: the named reloads, then every domain with a reload service, both as Core has them."""
    named = [target for target, reload in NAMED_RELOADS.items() if f"{reload.domain}.{reload.service}" in services]
    domains = sorted(s.removesuffix(".reload") for s in services if s.endswith(".reload"))
    return named + domains


class Control:
    def __init__(self, supervisor: Supervisor, state: State):
        self.supervisor = supervisor
        self.state = state

    def check(self, by: str) -> Result:
        return self._gated("check", by, lambda: Result(Outcome.CHECKED, "The config check passed."))

    def reload(self, domain: str, service: str, by: str) -> Result:
        try:
            offered = f"{domain}.{service}" in self.supervisor.services()
        except Unavailable as e:
            return self._record("reload", by, Result(Outcome.UNAVAILABLE, f"Home Assistant is not answering: {e}"))
        if not offered:
            return self._record(
                "reload", by, Result(Outcome.BAD_REQUEST, f"Home Assistant offers no {domain}.{service}.")
            )

        def reload() -> Result:
            self.supervisor.call_service(domain, service)
            return Result(Outcome.RELOADED, f"The config check passed; called {domain}.{service}.")

        return self._gated("reload", by, reload)

    def restart(self, by: str) -> Result:
        def restart() -> Result:
            started = time.monotonic()
            try:
                self.supervisor.restart()
            except Unavailable as e:
                return Result(Outcome.UNAVAILABLE, f"The config check passed; {e}.")
            seconds = round(time.monotonic() - started)
            return Result(
                Outcome.RESTARTED,
                f"The config check passed; Home Assistant was back after {seconds} s.",
                seconds=seconds,
            )

        return self._gated("restart", by, restart)

    def _gated(self, action: str, by: str, then: Callable[[], Result]) -> Result:
        try:
            self.supervisor.check()
            result = then()
        except CheckFailed as e:
            result = Result(Outcome.CHECK_FAILED, "The config check failed; nothing was done.", output=e.output)
        except Unavailable as e:
            result = Result(Outcome.UNAVAILABLE, f"Home Assistant is not answering: {e}")
        return self._record(action, by, result)

    def _record(self, action: str, by: str, result: Result) -> Result:
        log.info("%s by %s: %s", action, by, result.message)
        self.state.record(action, result, by)
        return result
