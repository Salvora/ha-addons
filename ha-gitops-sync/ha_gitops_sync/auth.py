"""Who may call the signed API: an allowed source, a fresh timestamp, the endpoint's key, and no replay."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import threading
import time
from collections.abc import Callable

from ha_gitops_sync.options import Network

WINDOW = 300
TIMESTAMP = re.compile(r"[0-9]{1,12}")


class Auth:
    def __init__(self, allowed_sources: tuple[Network, ...], clock: Callable[[], float] = time.time):
        self.allowed_sources = allowed_sources
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}

    def source_allowed(self, source: str) -> bool:
        address = ipaddress.ip_address(source)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in network for network in self.allowed_sources)

    def verify(
        self, key: bytes, source: str, timestamp: str | None, signature: str | None, path: str, body: bytes
    ) -> bool:
        if not (self.source_allowed(source) and timestamp and signature and TIMESTAMP.fullmatch(timestamp)):
            return False
        now, sent = self._clock(), int(timestamp)
        if abs(now - sent) > WINDOW:
            return False
        expected = "sha256=" + hmac.new(key, f"{timestamp}.{path}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            return False
        with self._lock:
            self._seen = {seen: until for seen, until in self._seen.items() if until >= now}
            if signature in self._seen:
                return False
            self._seen[signature] = sent + WINDOW
        return True
