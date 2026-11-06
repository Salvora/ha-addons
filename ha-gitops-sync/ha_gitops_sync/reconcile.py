"""Start-up and daily: attach /config on the first start, put back a deploy cut off by a stop, recover after a backup
restore, catch up on drift."""

from __future__ import annotations

import logging

from ha_gitops_sync.capture import Capture
from ha_gitops_sync.deploy import Deploy
from ha_gitops_sync.git import NAME, Git, GitError
from ha_gitops_sync.state import Outcome, Result, State
from ha_gitops_sync.worktree import apply_sparse, nested_repositories

log = logging.getLogger(__name__)

BRANCH = "refs/heads/live"
CONTROL_FILES = (".gitignore", ".gitattributes", ".gitops")


class StartError(RuntimeError):
    pass


class Reconcile:
    def __init__(self, git: Git, remote_url: str, state: State, capture: Capture, deploy: Deploy):
        self.git = git
        self.remote_url = remote_url
        self.state = state
        self.capture = capture
        self.deploy = deploy

    def attach(self) -> None:
        """Connects /config to the repository: HEAD and the index move, and the files stay, except that the first
        start adds the control files /config lacks. Without .gitignore, git would take in .storage/ and the logs."""
        if not (self.git.root / ".git").exists():
            self.git.run("init", "--quiet", "--initial-branch=live")
        branch = self.git.output("symbolic-ref", "--quiet", "HEAD").strip()
        if branch != BRANCH:
            raise StartError(f"/config/.git is on {branch or 'a detached HEAD'}, not live: another setup owns it")
        self.git.run("config", "remote.origin.url", self.remote_url)
        self.git.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        if not self.git.rev("HEAD"):
            try:
                self.git.run("fetch", "--prune", "origin")
            except GitError as e:
                raise StartError(f"cannot fetch the repository to attach /config: {e}") from None
            base = "origin/live" if self.git.rev("origin/live") else "origin/main"
            if not self.git.rev(base):
                raise StartError("the repository has no main branch; upload /config to it first")
            self.git.run("reset", "--quiet", base)
            missing = self.git.run("ls-files", "--deleted", "-z", "--", *CONTROL_FILES).split("\0")
            added = [path for path in missing if path]
            if added:
                self.git.run("--literal-pathspecs", "checkout", "HEAD", "--", *added)
            apply_sparse(self.git)
            sha = self.git.rev("HEAD")
            changed = (
                f"added {', '.join(added)} from the repository; no other file changed" if added else "no file changed"
            )
            result = Result(Outcome.OK, f"Attached /config to {base} at {sha[:7]}; {changed}.", live=sha)
            log.info(result.message)
            self.state.record("attach", result, NAME)
        if nested := nested_repositories(self.git):
            raise StartError(f"nested repositories in /config: {', '.join(nested)}; git would not track their files")

    def run(self) -> None:
        self.deploy.recover()
        if self.capture.run() is None:
            self.capture.publish()
