"""The git CLI on the repository at /config, as the add-on's commit identity; callers decide what to run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

NAME = "Home Assistant"
EMAIL = "home-assistant@ha-gitops-sync.invalid"
TIMEOUT = 300


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, root: Path, token: str):
        self.root = root
        self._env = {
            "GIT_AUTHOR_NAME": NAME,
            "GIT_AUTHOR_EMAIL": EMAIL,
            "GIT_COMMITTER_NAME": NAME,
            "GIT_COMMITTER_EMAIL": EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: token {token}",
        }

    def _exec(self, args: tuple[str, ...], ok: tuple[int, ...], stdin: str | None, timeout: float):
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env={**os.environ, **self._env},
                input=stdin,
                capture_output=True,
                encoding="utf-8",
                errors="surrogateescape",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {' '.join(args)} timed out after {timeout:.0f} s") from None
        if proc.returncode not in ok:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return proc

    def run(self, *args: str, stdin: str | None = None, timeout: float = TIMEOUT) -> str:
        return self._exec(args, (0,), stdin, timeout).stdout

    def check(self, *args: str, stdin: str | None = None) -> bool:
        """A yes/no question: exit 0 is yes, 1 is no, anything else an error."""
        return self._exec(args, (0, 1), stdin, TIMEOUT).returncode == 0

    def output(self, *args: str, stdin: str | None = None) -> str:
        """The output of a command whose exit 1 is an answer, not a failure (check-ignore, rev-parse --quiet)."""
        return self._exec(args, (0, 1), stdin, TIMEOUT).stdout

    def rev(self, ref: str) -> str | None:
        return self.output("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip() or None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.check("merge-base", "--is-ancestor", ancestor, descendant)
