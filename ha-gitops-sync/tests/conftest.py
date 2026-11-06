"""Real git against temporary bare repositories; fakes for Gitea, the Supervisor and Core."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from ha_gitops_sync.api import ApiError
from ha_gitops_sync.capture import Capture
from ha_gitops_sync.control import Control
from ha_gitops_sync.deploy import Deploy
from ha_gitops_sync.git import Git
from ha_gitops_sync.reconcile import Reconcile
from ha_gitops_sync.state import State
from ha_gitops_sync.supervisor import ESPHOME, CheckFailed, Unavailable
from ha_gitops_sync.worker import Worker

INITIAL = {
    "configuration.yaml": "homeassistant:\n  name: Home\nautomation: !include automations.yaml\n",
    "automations.yaml": "- id: '1'\n  alias: Lights on\n",
    "scripts.yaml": "{}\n",
    "esphome/archive/old.yaml": "esphome:\n  name: old\n",
    ".gitignore": "*.log\n.storage/\ndeps/\n",
    ".gitattributes": "* text=auto eol=lf\n",
    ".gitops/repository-only": "# kept off Home Assistant\n.gitea/\nci/\nREADME.md\n",
    ".gitea/workflows/validate.yml": "on: pull_request\n",
    "ci/check.py": "print('ok')\n",
    "README.md": "# config\n",
}
REPOSITORY_ONLY = (".gitea/workflows/validate.yml", "ci/check.py", "README.md")
CONTROL_FILES = (".gitignore", ".gitattributes", ".gitops/repository-only")
SELF_SIGNED = (
    "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=localhost "
    "-addext subjectAltName=DNS:localhost,IP:127.0.0.1 -days 1"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def isolate(tmp_path_factory, monkeypatch) -> None:
    """This machine's global git config signs commits; tests run with none."""
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Dev")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "dev@example.invalid")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    isolate(tmp_path_factory, monkeypatch)


@pytest.fixture(scope="session")
def tls(tmp_path_factory) -> Path:
    """Two self-signed pairs for localhost and 127.0.0.1, a.pem/a.key and b.pem/b.key, made for this run."""
    folder = tmp_path_factory.mktemp("tls")
    for name in ("a", "b"):
        subprocess.run(
            [*SELF_SIGNED.split(), "-keyout", str(folder / f"{name}.key"), "-out", str(folder / f"{name}.pem")],
            check=True,
            capture_output=True,
        )
    return folder


class Dev:
    """A developer's clone of the config repository, pushing to main or another branch."""

    def __init__(self, path: Path):
        self.path = path

    def commit(self, files: dict[str, str | None], message: str = "Change", branch: str = "main") -> str:
        git(self.path, "fetch", "--quiet", "origin")
        known = git(self.path, "branch", "--remotes", "--list", f"origin/{branch}")
        base = f"origin/{branch}" if known else "origin/main"
        git(self.path, "checkout", "--quiet", "-B", branch, base)
        for name, text in files.items():
            target = self.path / name
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, newline="\n")
        git(self.path, "add", "--all")
        git(self.path, "commit", "--quiet", "--message", message)
        git(self.path, "push", "--quiet", "origin", branch)
        return git(self.path, "rev-parse", "HEAD").strip()


class FakeGitea:
    def __init__(self):
        self.url = "https://gitea.example"
        self.repository = "home/config"
        self.down = False
        self.checks: dict[str, str] = {}
        self.checks_url: dict[str, str] = {}
        self.impact: dict[str, str] = {}
        self.deploy_statuses: list[tuple[str, str]] = []
        self.pulls: list[dict] = []
        self.refused_pulls = 0
        self.pull_attempts: list[float] = []

    def _answer(self) -> None:
        if self.down:
            raise ApiError(0, "GET https://gitea.example/api/v1: timed out")

    def commit_url(self, sha: str) -> str:
        return f"{self.url}/{self.repository}/commit/{sha}"

    def statuses(self, sha: str, timeout: float = 30) -> dict[str, dict]:
        self._answer()
        found = {}
        if sha in self.checks:
            found["ha/checks"] = {"status": self.checks[sha], "target_url": self.checks_url.get(sha, "")}
        if sha in self.impact:
            found["ha/impact"] = {"status": "success", "description": self.impact[sha]}
        return found

    def set_status(self, sha: str, state: str, description: str, target_url: str = "") -> None:
        self.deploy_statuses.append((sha, state))

    def pull_request(self, timeout: float = 30) -> dict | None:
        self._answer()
        return next(iter(self.pulls), None)

    def open_pull_request(self) -> dict:
        self.pull_attempts.append(time.monotonic())
        if self.refused_pulls:
            self.refused_pulls -= 1
            raise ApiError(502, "Bad Gateway")
        if not self.pulls:
            number = 7
            self.pulls.append(
                {
                    "number": number,
                    "title": "Changes made in Home Assistant",
                    "html_url": f"{self.url}/{self.repository}/pulls/{number}",
                    "head": {"ref": "live"},
                    "base": {"ref": "main"},
                    "mergeable": True,
                }
            )
        return self.pulls[0]


class FakeSupervisor:
    def __init__(self):
        self.failing_check: str | None = None
        self.down = False
        self.services_unanswered = False
        self.on_check = None
        self.restart_takes = 0.0
        self.checks = 0
        self.restarts = 0
        self.called: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.offered = {
            "homeassistant.reload_all",
            "homeassistant.reload_core_config",
            "frontend.reload_themes",
            "automation.reload",
            "script.reload",
            "light.turn_on",
        }
        self.esphome_version: str | None = None

    def check(self) -> None:
        if self.down:
            raise Unavailable("Bad Gateway", 502)
        self.checks += 1
        if self.on_check:
            self.on_check()
        if self.failing_check is not None:
            raise CheckFailed(self.failing_check)

    def restart(self) -> None:
        time.sleep(self.restart_takes)
        self.restarts += 1

    def services(self, timeout: float = 30) -> set[str]:
        if self.services_unanswered:
            time.sleep(timeout)
            raise Unavailable("GET http://supervisor/core/api/services: timed out", 0)
        if self.down:
            raise Unavailable("Bad Gateway", 502)
        return set(self.offered)

    def call_service(self, domain: str, service: str) -> None:
        self.called.append(f"{domain}.{service}")

    def fire_event(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    def addon_version(self, slug: str) -> str | None:
        return self.esphome_version if slug == ESPHOME else None


@dataclass
class System:
    root: Path
    remote: Path
    dev: Dev
    gitea: FakeGitea
    supervisor: FakeSupervisor
    state: State
    worker: Worker
    git: Git
    capture: Capture
    deploy: Deploy
    control: Control
    reconcile: Reconcile

    def read(self, name: str) -> str:
        return (self.root / name).read_text()

    def write(self, name: str, text: str) -> None:
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(text, newline="\n")

    def remote_rev(self, branch: str) -> str:
        return git(self.remote, "rev-parse", branch).strip()

    def remote_show(self, ref: str, name: str) -> str:
        return git(self.remote, "show", f"{ref}:{name}")

    def remote_log(self, ref: str, format: str) -> str:
        return git(self.remote, "log", "-1", f"--format={format}", ref).strip()

    def remote_parents(self, ref: str) -> list[str]:
        return self.remote_log(ref, "%P").split()

    def remote_files(self, ref: str) -> list[str]:
        return git(self.remote, "ls-tree", "-r", "--name-only", ref).splitlines()

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD").strip()


def build(base: Path) -> None:
    """The repository with the uploaded config on main, a developer's clone, and /config as Home Assistant has it:
    the same files without the repository-only ones and the control files, plus what .gitignore leaves out."""
    remote = base / "origin.git"
    git(base, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    seed = base / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "--initial-branch=main")
    for name, text in INITIAL.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text, newline="\n")
    git(seed, "add", "--all")
    git(seed, "commit", "--quiet", "--message", "Initial upload of /config")
    git(seed, "push", "--quiet", str(remote), "main")
    git(base, "clone", "--quiet", str(remote), "dev")

    root = base / "config"
    for name, text in INITIAL.items():
        if name not in REPOSITORY_ONLY + CONTROL_FILES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, newline="\n")
    (root / ".storage").mkdir()
    (root / ".storage" / "core.config").write_text("{}\n")
    (root / "home-assistant.log").write_text("started\n")


def wire(base: Path) -> System:
    remote, root = base / "origin.git", base / "config"
    gitea, supervisor = FakeGitea(), FakeSupervisor()
    state, worker = State(base / "state.json"), Worker()
    repo = Git(root, "token-that-never-shows")
    capture = Capture(repo, gitea, supervisor, state, worker)
    return System(
        root=root,
        remote=remote,
        dev=Dev(base / "dev"),
        gitea=gitea,
        supervisor=supervisor,
        state=state,
        worker=worker,
        git=repo,
        capture=capture,
        deploy=Deploy(repo, gitea, supervisor, state, capture),
        control=Control(supervisor, state),
        reconcile=Reconcile(repo, str(remote), state, capture),
    )


@pytest.fixture(scope="session")
def attached(tmp_path_factory) -> Path:
    """Built and attached once, with live pushed; each test gets a copy. Both switches paused, as on a fresh install."""
    base = tmp_path_factory.mktemp("attached")
    with pytest.MonkeyPatch.context() as monkeypatch:
        isolate(tmp_path_factory, monkeypatch)
        build(base)
        system = wire(base)
        system.reconcile.attach()
        system.reconcile.run()
        system.worker.shutdown()
    return base


@pytest.fixture
def fresh(tmp_path):
    """Not attached yet: the first start."""
    build(tmp_path)
    system = wire(tmp_path)
    yield system
    system.worker.shutdown()


@pytest.fixture
def system(attached, tmp_path):
    base = tmp_path / "copy"
    shutil.copytree(attached, base)
    git(base / "config", "config", "remote.origin.url", str(base / "origin.git"))
    git(base / "dev", "remote", "set-url", "origin", str(base / "origin.git"))
    system = wire(base)
    yield system
    system.worker.shutdown()
