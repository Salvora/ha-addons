"""Capture (plan §3.3): Home Assistant's edits committed, and the push policy every new commit on live goes through."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum

from ha_gitops_sync.api import ApiError
from ha_gitops_sync.git import NAME, Git, GitError
from ha_gitops_sync.gitea import Gitea
from ha_gitops_sync.state import Outcome, Result, State, Switch
from ha_gitops_sync.supervisor import ESPHOME, Supervisor
from ha_gitops_sync.worker import Worker
from ha_gitops_sync.worktree import apply_sparse, changes, commit

log = logging.getLogger(__name__)

RETRY_DELAYS = (60, 300, 900, 3600)
SUBJECT_LENGTH = 72
# A capture commits even when this fetch fails, so it does not wait long for an unreachable Gitea.
FETCH_TIMEOUT = 30
ESPHOME_VERSION = ".gitops/esphome-version"


class Pushed(StrEnum):
    MAIN = "pushed to main and live"
    PULL_REQUEST = "pushed to live, waiting in the live → main pull request"
    LIVE = "pushed to live"
    WAITING = "Gitea did not answer; it is tried again later"
    HELD = "not pushed while a deploy that never ended waits to be put back"


@dataclass(frozen=True)
class Published:
    where: Pushed
    pull_request: str = ""


def message(files: tuple[str, ...]) -> str:
    subject = f"Edited in Home Assistant: {', '.join(files)}"
    if len(subject) > SUBJECT_LENGTH:
        subject = f"Edited in Home Assistant: {len(files)} files"
    return subject + "\n\n" + "\n".join(files) + "\n"


class Capture:
    def __init__(
        self,
        git: Git,
        gitea: Gitea,
        supervisor: Supervisor,
        state: State,
        worker: Worker,
        delays: tuple[float, ...] = RETRY_DELAYS,
    ):
        self.git = git
        self.gitea = gitea
        self.supervisor = supervisor
        self.state = state
        self.worker = worker
        self.delays = delays
        self._retry: threading.Timer | None = None
        self._failures = 0

    def run(self) -> Result | None:
        if not self.state.is_on(Switch.CAPTURE) or self._deploy_unfinished():
            return None
        try:
            self.git.run("fetch", "--prune", "origin", timeout=FETCH_TIMEOUT)
        except GitError as e:
            log.warning("fetch failed; comparing with what was fetched before: %s", e)
        restored = self.restore()
        self._write_esphome_version()
        found = changes(self.git)
        if found.skipped:
            log.warning(
                "not captured (repository-only, a new file over 20 MB or a nested repository): %s",
                ", ".join(found.skipped),
            )
        if not found.files:
            if restored:
                self.publish(fetched=True)
            return restored
        text = message(found.files)
        sha = commit(self.git, found.files, text)
        published = self.publish(fetched=True)
        result = Result(
            Outcome.OK,
            f"{text.splitlines()[0]}; {published.where}.",
            live=sha,
            pull_request=published.pull_request,
            files=found.files,
        )
        self.state.record("capture", result, NAME)
        self.supervisor.fire_event("gitops_captured", {"commit": sha, "files": list(found.files), "by": NAME})
        return result

    def _write_esphome_version(self) -> None:
        version = self.supervisor.addon_version(ESPHOME)
        path = self.git.root / ESPHOME_VERSION
        if version and (not path.exists() or path.read_text().strip() != version):
            path.parent.mkdir(exist_ok=True)
            path.write_text(version + "\n")

    def restore(self) -> Result | None:
        """Commits a backup restore's older files on top of live, never forced; returns the recorded Result, or None."""
        if not self.git.rev("origin/live") or self.git.is_ancestor("origin/live", "HEAD"):
            return None
        restored = self.git.rev("HEAD")
        self.git.run("reset", "--quiet", "origin/live")
        apply_sparse(self.git)
        files = changes(self.git).files
        if not files:
            log.info("the history was behind live, the files were not; caught up with live")
            return None
        sha = commit(self.git, files, f"Restored from backup: back to {restored[:7]}")
        result = Result(
            Outcome.OK,
            f"/config was restored from a backup to {restored[:7]}; committed on top of live.",
            live=sha,
            files=files,
        )
        log.warning(result.message)
        self.state.record("restore", result, NAME)
        return result

    def publish(self, *, fetched: bool = False) -> Published:
        """Push live, and main too when that is a fast-forward; otherwise keep the live → main pull request open."""
        if self._deploy_unfinished():
            return Published(Pushed.HELD)
        try:
            if not fetched:
                self.git.run("fetch", "--prune", "origin")
                self.restore()
            published = self._push()
        except (GitError, ApiError) as e:
            log.warning("push failed: %s", e)
            if self._retry is None and self._unpublished():
                delay = self.delays[min(self._failures, len(self.delays) - 1)]
                self._failures += 1
                self._retry = threading.Timer(delay, self.worker.submit, (self._retry_publish, "pushing to Gitea"))
                self._retry.daemon = True
                self._retry.start()
            return Published(Pushed.WAITING)
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        self._failures = 0
        return published

    def _deploy_unfinished(self) -> bool:
        """A deploy whose check never ended may have left its commit in HEAD: nothing goes on top of it or to Gitea
        until the next deploy, start or daily check puts it back."""
        placed = self.state.placed()
        if placed:
            log.warning("holding edits and pushes until the deploy of %s is put back", placed.sha[:7])
        return placed is not None

    def _unpublished(self) -> bool:
        """Commits not on live yet, or not on main yet, which then need the live → main pull request."""
        return self.git.rev("HEAD") != self.git.rev("origin/live") or not self.git.is_ancestor("HEAD", "origin/main")

    def _retry_publish(self) -> None:
        self._retry = None
        self.publish()

    def _push(self) -> Published:
        live, main = self.git.rev("HEAD"), self.git.rev("origin/main")
        refs = [] if live == self.git.rev("origin/live") else ["HEAD:refs/heads/live"]
        along = live != main and self.git.is_ancestor("origin/main", "HEAD")
        if along:
            refs.append("HEAD:refs/heads/main")
        if refs:
            self.git.run("push", "--atomic", "--quiet", "origin", *refs)
        if along or self.git.is_ancestor("HEAD", "origin/main"):
            return Published(Pushed.MAIN if along else Pushed.LIVE)
        return Published(Pushed.PULL_REQUEST, self.gitea.open_pull_request()["html_url"])
