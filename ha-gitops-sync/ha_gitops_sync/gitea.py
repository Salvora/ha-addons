"""Gitea's API for the config repository: commit statuses and the live → main pull request."""

from __future__ import annotations

from ha_gitops_sync import api

CHECKS = "ha/checks"
DEPLOY = "ha/deploy"
IMPACT = "ha/impact"
PULL_TITLE = "Changes made in Home Assistant"
PULL_BODY = (
    "Edits made in Home Assistant while `main` had commits not yet deployed. "
    "The next deploy merges them into `live`; once they reach `main`, Gitea marks this pull request merged. "
    "If a deploy reports a conflict, resolve it on a branch from live with main merged in, and merge that into main "
    "with a merge commit; only the add-on pushes live."
)


class Gitea:
    def __init__(self, url: str, repository: str, token: str):
        self.url = url
        self.repository = repository
        self._token = token

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = api.TIMEOUT):
        url = f"{self.url}/api/v1/repos/{self.repository}{path}"
        return api.call(method, url, f"token {self._token}", body, timeout)

    def commit_url(self, sha: str) -> str:
        return f"{self.url}/{self.repository}/commit/{sha}"

    def statuses(self, sha: str, timeout: float = api.TIMEOUT) -> dict[str, dict]:
        """The latest status of each context on a commit."""
        combined = self._call("GET", f"/commits/{sha}/status", timeout=timeout) or {}
        return {status["context"]: status for status in combined.get("statuses") or []}

    def set_status(self, sha: str, state: str, description: str, target_url: str = "") -> None:
        body = {"state": state, "context": DEPLOY, "description": description[:255], "target_url": target_url}
        self._call("POST", f"/statuses/{sha}", body)

    def pull_request(self, timeout: float = api.TIMEOUT) -> dict | None:
        """The open live → main pull request."""
        pulls = self._call("GET", "/pulls?state=open&limit=50", timeout=timeout) or []
        return next((p for p in pulls if p["head"]["ref"] == "live" and p["base"]["ref"] == "main"), None)

    def open_pull_request(self) -> dict:
        body = {"head": "live", "base": "main", "title": PULL_TITLE, "body": PULL_BODY}
        return self.pull_request() or self._call("POST", "/pulls", body)
