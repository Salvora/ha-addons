"""A repository on a registry as image.yml needs it: whether a tag is there, whether the login may push, and kaniko's
login file. The token endpoint is the one the registry names when it refuses an anonymous request: ghcr.io's /token,
Gitea's /v2/token."""

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import urlencode, urljoin, urlsplit

from addons.api import Answer, request

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
REFUSED = (401, 403)
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    """An answer the registry's API does not give."""


@dataclass(frozen=True)
class Repository:
    host: str
    name: str
    bearer: str

    @classmethod
    def login(cls, host: str, name: str, user: str, password: str) -> Self | None:
        """The repository with a token for pull and push, None when the registry refuses the login."""
        challenge = request("GET", f"https://{host}/v2/")
        offered = challenge.headers.get_all("WWW-Authenticate", [])
        bearer = next((dict(CHALLENGE.findall(c)) for c in offered if c.lower().startswith("bearer ")), {})
        if challenge.status != 401 or "realm" not in bearer:
            raise RegistryError(f"{host}: /v2/ answered HTTP {challenge.status} without naming its token endpoint")
        if urlsplit(bearer["realm"]).netloc != host:
            raise RegistryError(f"{host} names its token endpoint on another host, {bearer['realm']}: no login sent")
        basic = base64.b64encode(f"{user}:{password}".encode()).decode()
        query = urlencode({"service": bearer.get("service", host), "scope": f"repository:{name}:pull,push"})
        answer = request("GET", f"{bearer['realm']}?{query}", headers={"Authorization": f"Basic {basic}"})
        if answer.status in REFUSED:
            return None
        if answer.status != 200:
            raise RegistryError(f"{host}: token request for {name}: HTTP {answer.status} {answer.said()}")
        return cls(host, name, answer.json()["token"])

    def _call(self, method: str, path: str, headers: dict[str, str] | None = None) -> Answer:
        url = urljoin(f"https://{self.host}/v2/{self.name}/", path)
        return request(method, url, headers={"Authorization": f"Bearer {self.bearer}", **(headers or {})})

    def has_tag(self, tag: str) -> bool:
        answer = self._call("HEAD", f"manifests/{tag}", {"Accept": MANIFEST_TYPES})
        if answer.status not in (200, 404):
            raise RegistryError(f"{self.host}/{self.name}:{tag}: HTTP {answer.status} {answer.said()}")
        return answer.status == 200

    def can_push(self) -> bool:
        """Starts a blob upload and cancels it, as kaniko does before it builds."""
        answer = self._call("POST", "blobs/uploads/")
        if answer.status in REFUSED:
            return False
        if answer.status != 202:
            raise RegistryError(f"{self.host}/{self.name}: starting an upload: HTTP {answer.status} {answer.said()}")
        # The cancel's answer does not matter: an upload left open expires.
        self._call("DELETE", answer.headers["Location"])
        return True


def write_login(path: Path, logins: dict[str, tuple[str, str]]) -> None:
    """kaniko's login for each host, (user, password): a Docker config.json at path that only its owner can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    auths = {host: {"username": user, "password": password} for host, (user, password) in logins.items()}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
        json.dump({"auths": auths}, out)
