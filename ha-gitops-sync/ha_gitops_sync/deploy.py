"""Deploy (plan §3.2): place a commit of main in /config, check it, push live. It never reloads or restarts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ha_gitops_sync.api import ApiError
from ha_gitops_sync.capture import Capture
from ha_gitops_sync.git import Git, GitError
from ha_gitops_sync.gitea import CHECKS, Gitea
from ha_gitops_sync.state import Outcome, Result, State, Switch
from ha_gitops_sync.supervisor import CheckFailed, Supervisor, Unavailable
from ha_gitops_sync.worktree import apply_sparse, changes, listed, refresh, repository_only

log = logging.getLogger(__name__)

SHA = re.compile(r"[0-9a-f]{40}")
TRIGGERS = ("merge", "manual")
MAX_IMPACT = 200

DEPLOY_STATUS = {
    Outcome.DEPLOYED: "success",
    Outcome.ALREADY_LIVE: "success",
    Outcome.DIRTY: "failure",
    Outcome.CONFLICT: "failure",
    Outcome.CHECK_FAILED: "failure",
}
EVENTS = {
    Outcome.DEPLOYED: "gitops_deployed",
    Outcome.CHECK_FAILED: "gitops_deploy_failed",
    Outcome.CONFLICT: "gitops_conflict",
}


@dataclass(frozen=True)
class Request:
    sha: str
    trigger: str
    run_url: str = ""
    impact: str = ""

    @classmethod
    def parse(cls, body: dict) -> Request:
        request = cls(
            sha=body.get("sha"),
            trigger=body.get("trigger"),
            run_url=body.get("run_url", ""),
            impact=body.get("impact", ""),
        )
        if not (isinstance(request.sha, str) and SHA.fullmatch(request.sha)):
            raise ValueError("sha must be a full commit id, 40 lowercase hex digits")
        if request.trigger not in TRIGGERS:
            raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}")
        if not (isinstance(request.run_url, str) and isinstance(request.impact, str)):
            raise ValueError("run_url and impact must be text")
        if len(request.impact) > MAX_IMPACT:
            raise ValueError(f"impact must be at most {MAX_IMPACT} characters")
        return request


def _except(kept: tuple[str, ...]) -> str:
    if not kept:
        return ""
    return f", except {', '.join(kept)}, edited in Home Assistant while the check ran, which stay as edited"


class Deploy:
    def __init__(self, git: Git, gitea: Gitea, supervisor: Supervisor, state: State, capture: Capture):
        self.git = git
        self.gitea = gitea
        self.supervisor = supervisor
        self.state = state
        self.capture = capture

    def run(self, request: Request, by: str) -> Result:
        try:
            result = self._deploy(request)
        except GitError as e:
            result = Result(Outcome.ERROR, f"Deploying {request.sha[:7]} stopped: {e}")
        log.info("deploy %s by %s: %s", request.sha[:7], by, result.message)
        self.state.record("deploy", result, by, request.run_url)
        self._report(request, result, by)
        return result

    def _deploy(self, request: Request) -> Result:
        sha, short = request.sha, request.sha[:7]
        if request.trigger == "merge" and not self.state.is_on(Switch.AUTO_DEPLOY):
            return Result(Outcome.PAUSED, f"Auto-deploy is paused in the panel; {short} waits for a manual deploy.")
        try:
            self.git.run("fetch", "--prune", "origin")
        except GitError as e:
            return Result(Outcome.UNAVAILABLE, f"Gitea is not answering: {e}")
        if self.capture.restore():
            self.capture.publish(fetched=True)
        if not (self.git.rev(sha) and self.git.is_ancestor(sha, "origin/main")):
            return Result(Outcome.NOT_ON_MAIN, f"{short} is not on main.")
        if self.git.is_ancestor(sha, "HEAD"):
            return Result(Outcome.ALREADY_LIVE, f"{short} is already live.", live=self.git.rev("HEAD"))
        try:
            checks = self.gitea.statuses(sha).get(CHECKS, {}).get("status")
        except ApiError as e:
            return Result(Outcome.UNAVAILABLE, f"Gitea is not answering: {e}")
        if checks != "success":
            return Result(Outcome.CHECKS_NOT_PASSED, f"The checks have not passed for {short} yet.")
        pending = changes(self.git).files
        if pending and not self.state.is_on(Switch.CAPTURE):
            return Result(
                Outcome.DIRTY,
                "Home Assistant has edits that are not committed, and capture is paused; /config is untouched; "
                "switch Capture on, then deploy again.",
                files=pending,
            )
        if pending:
            self.capture.run()
        return self._place(request)

    def _place(self, request: Request) -> Result:
        sha, short, previous = request.sha, request.sha[:7], self.git.rev("HEAD")
        refresh(self.git)
        if self.git.is_ancestor("HEAD", sha):
            self.git.run("merge", "--ff-only", "--quiet", sha)
        elif self.git.check("merge-tree", "--write-tree", "--name-only", "HEAD", sha):
            self.git.run("merge", "--no-ff", "--quiet", "--message", f"Deploy {short}: merge main into live", sha)
        else:
            return Result(
                Outcome.CONFLICT,
                f"{short} conflicts with edits made in Home Assistant; /config is untouched. Resolve it on a branch "
                "from live with main merged in, merged into main with a merge commit; that merge deploys.",
                pull_request=self._pull_request(),
            )
        apply_sparse(self.git)
        try:
            self.supervisor.check()
        except CheckFailed as e:
            kept = self._put_back(previous)
            return Result(
                Outcome.CHECK_FAILED,
                f"The config check failed on {short}; the files were put back{_except(kept)}.",
                output=e.output,
                files=kept,
            )
        except Unavailable as e:
            kept = self._put_back(previous)
            return Result(
                Outcome.UNAVAILABLE,
                f"Home Assistant did not run the config check: {e}; the files were put back{_except(kept)}.",
                files=kept,
            )
        published = self.capture.publish()
        files = self.git.run("diff", "--name-only", "-z", previous, "HEAD").split("\0")
        subject = self.git.run("log", "-1", "--format=%s", sha).strip()
        needs = f"; needs: {request.impact}" if request.impact else ""
        return Result(
            Outcome.DEPLOYED,
            f"Deployed {short} ({subject}); {published.where}{needs}.",
            live=self.git.rev("HEAD"),
            pull_request=published.pull_request,
            files=tuple(filter(None, files)),
        )

    def _put_back(self, previous: str) -> tuple[str, ...]:
        """Back to previous with no step that can refuse or discard: a placed file edited while the check ran stays as
        edited and is returned; every other placed file still held the placed commit's content."""
        edited = set(changes(self.git).files)
        entries = repository_only(self.git)
        status = self.git.run("diff", "--name-status", "-z", "--no-renames", previous, "HEAD").split("\0")[:-1]
        placed = {path: kind for kind, path in zip(status[::2], status[1::2], strict=True) if not listed(path, entries)}
        self.git.run("reset", "--quiet", previous)
        restore = [path for path, kind in placed.items() if path not in edited and kind != "A"]
        if restore:
            self.git.run(
                "--literal-pathspecs",
                "checkout",
                previous,
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                stdin="\0".join(restore),
            )
        for path, kind in placed.items():
            if path not in edited and kind == "A":
                self._delete(path)
        apply_sparse(self.git)
        return tuple(sorted(edited & placed.keys()))

    def _delete(self, path: str) -> None:
        """A placed file previous lacks, and the folders it leaves empty, as git would remove them."""
        file = self.git.root / path
        file.unlink()
        for folder in file.parents:
            if folder == self.git.root or any(folder.iterdir()):
                break
            folder.rmdir()

    def _pull_request(self) -> str:
        try:
            pull = self.gitea.pull_request()
        except ApiError as e:
            log.warning("no link to the live → main pull request: %s", e)
            return ""
        return pull["html_url"] if pull else ""

    def _report(self, request: Request, result: Result, by: str) -> None:
        if state := DEPLOY_STATUS.get(result.outcome):
            try:
                self.gitea.set_status(request.sha, state, result.message, request.run_url)
            except ApiError as e:
                log.warning("ha/deploy status on %s not set: %s", request.sha[:7], e)
        if event := EVENTS.get(result.outcome):
            data = {"commit": request.sha, "files": list(result.files), "impact": request.impact, "by": by}
            self.supervisor.fire_event(event, {key: value for key, value in data.items() if value})
