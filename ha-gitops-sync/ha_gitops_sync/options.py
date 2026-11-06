"""/data/options.json as Options, with the checks the add-on schema cannot express."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_KEY = 32


@dataclass(frozen=True)
class Options:
    gitea_url: str
    repository: str
    gitea_token: str = field(repr=False)
    deploy_key: bytes = field(repr=False)
    control_key: bytes = field(repr=False)
    allowed_sources: tuple[Network, ...]
    log_level: str

    @property
    def remote_url(self) -> str:
        return f"{self.gitea_url}/{self.repository}.git"


def load(path: Path) -> Options:
    raw = json.loads(path.read_text())
    for name in ("deploy_key", "control_key"):
        if len(raw[name]) < MIN_KEY:
            raise SystemExit(f"options: {name} must be at least {MIN_KEY} characters; use `openssl rand -hex 32`")
    if raw["deploy_key"] == raw["control_key"]:
        raise SystemExit("options: deploy_key and control_key must differ: each grants different powers")
    try:
        sources = tuple(ipaddress.ip_network(source, strict=False) for source in raw["allowed_sources"])
    except ValueError as e:
        raise SystemExit(f"options: allowed_sources: {e}") from None
    return Options(
        gitea_url=raw["gitea_url"].rstrip("/"),
        repository=raw["repository"],
        gitea_token=raw["gitea_token"],
        deploy_key=raw["deploy_key"].encode(),
        control_key=raw["control_key"].encode(),
        allowed_sources=sources,
        log_level=raw["log_level"],
    )
