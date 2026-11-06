"""The ingress panel: one server-rendered page with the status, the two switches and the gated buttons."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import TimeoutError as Pending
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ha_gitops_sync.api import ApiError
from ha_gitops_sync.capture import Capture
from ha_gitops_sync.control import NAMED_RELOADS, Control, reload_service, reload_targets
from ha_gitops_sync.deploy import MAX_IMPACT, Deploy, Request
from ha_gitops_sync.git import Git, GitError
from ha_gitops_sync.gitea import CHECKS, IMPACT, Gitea
from ha_gitops_sync.state import Outcome, Result, State, Switch
from ha_gitops_sync.supervisor import Supervisor, Unavailable
from ha_gitops_sync.worker import Worker
from ha_gitops_sync.worktree import Changes, changes

log = logging.getLogger(__name__)

PORT = 8099  # the Supervisor's default ingress_port; config.yaml does not set it
INGRESS = "172.30.32.2"
CALL_TIMEOUT = 5
FETCH_TIMEOUT = 10
GATHER_TIMEOUT = 30
MAX_FORM = 16 * 1024
SWITCHES = {Switch.AUTO_DEPLOY: "Auto-deploy", Switch.CAPTURE: "Capture"}
GITEA_DOWN = "Gitea's API is not answering: checks and pull request unknown"
REFUSED = {
    "stale": "This page was out of date, so nothing was done. Press the button again.",
    "bad-form": "The form was not understood, so nothing was done.",
}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    at: float


@dataclass(frozen=True)
class View:
    live: Commit
    main: Commit | None
    fetched: bool
    gitea: bool
    waiting: int
    checks: str
    checks_url: str
    impact: str
    edits: Changes
    unpushed: int
    pull_request: dict | None
    conflict: bool
    targets: list[str]
    at: float


def tone(outcome: Outcome) -> str:
    if outcome.status < 300:
        return "ok"
    return "warn" if outcome.status in (409, 423) else "error"


def clock(at: float) -> str:
    moment = time.localtime(at)
    same_day = moment[:3] == time.localtime()[:3]
    return time.strftime("%H:%M" if same_day else "%d %b %H:%M", moment)


def deploy_refused(view: View, capture_on: bool) -> str:
    """Why Deploy is disabled when the panel already knows the deploy would be refused."""
    if view.edits.files and not capture_on:
        count = len(view.edits.files)
        return f"Switch Capture on first: {count} file{'s' if count != 1 else ''} not committed"
    if view.conflict:
        if view.pull_request:
            return f"Resolve the conflict first (see #{view.pull_request['number']})"
        return "Resolve the conflict first"
    return ""


class Panel:
    def __init__(
        self,
        git: Git,
        gitea: Gitea,
        supervisor: Supervisor,
        state: State,
        worker: Worker,
        capture: Capture,
        deploy: Deploy,
        control: Control,
    ):
        self.git = git
        self.gitea = gitea
        self.supervisor = supervisor
        self.state = state
        self.worker = worker
        self.capture = capture
        self.deploy = deploy
        self.control = control
        self.csrf = secrets.token_urlsafe(32)
        self._view: View | None = None
        self._target = "all"
        self._templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)
        self._templates.globals.update(tone=tone, clock=clock, commit_url=gitea.commit_url)
        self.actions: dict[str, Callable[[dict[str, str], str], None]] = {
            "switch": self._switch,
            "deploy": self._deploy,
            "check": self._check,
            "reload": self._reload,
            "restart": self._restart,
        }

    def _commit(self, ref: str) -> Commit | None:
        if not self.git.rev(ref):
            return None
        sha, at, subject = self.git.run("log", "-1", "--format=%H%x00%ct%x00%s", ref).rstrip("\n").split("\0")
        return Commit(sha, subject, float(at))

    def _count(self, revisions: str) -> int:
        return int(self.git.run("rev-list", "--count", revisions))

    def _gather(self) -> View:
        """Runs on the worker: main is fetched each time the page is opened, never in between."""
        try:
            self.git.run("fetch", "--prune", "origin", timeout=FETCH_TIMEOUT)
            fetched = True
        except GitError as e:
            log.info("panel: fetch failed: %s", e)
            fetched = False
        main = self._commit("origin/main")
        statuses, pull, answered = {}, None, True
        try:
            statuses = self.gitea.statuses(main.sha, timeout=CALL_TIMEOUT) if main else {}
            pull = self.gitea.pull_request(timeout=CALL_TIMEOUT)
        except ApiError as e:
            log.info("panel: Gitea is not answering: %s", e)
            answered = False
        try:
            targets = reload_targets(self.supervisor.services(timeout=CALL_TIMEOUT))
        except Unavailable:
            targets = list(NAMED_RELOADS)
        conflict = main is not None and not self.git.check(
            "merge-tree", "--write-tree", "--name-only", "HEAD", main.sha
        )
        checks = statuses.get(CHECKS, {})
        return View(
            live=self._commit("HEAD"),
            main=main,
            fetched=fetched,
            gitea=answered,
            waiting=self._count("HEAD..origin/main") if main else 0,
            checks=checks.get("status", ""),
            checks_url=checks.get("target_url", ""),
            impact=statuses.get(IMPACT, {}).get("description", ""),
            edits=changes(self.git),
            unpushed=self._count("origin/live..HEAD" if self.git.rev("origin/live") else "HEAD"),
            pull_request=pull,
            conflict=conflict,
            targets=targets,
            at=time.time(),
        )

    def page(self, base: str, confirm: str = "", refused: str = "") -> str:
        stale = ""
        idle = self.worker.idle
        if idle:
            try:
                self._view = self.worker.submit(self._gather, "reading the status").result(timeout=GATHER_TIMEOUT)
            except Pending:
                log.info("panel: the status took longer than %s s; showing the last one", GATHER_TIMEOUT)
                stale = f"Gitea or Home Assistant did not answer within {GATHER_TIMEOUT} s."
            except Exception as e:
                log.exception("panel: the status could not be read")
                stale = f"The status could not be read: {e}. Open the panel again to retry."
        capture_on = self.state.is_on(Switch.CAPTURE)
        return self._templates.get_template("panel.html").render(
            base=base,
            view=self._view,
            running=self.worker.running,
            stale=stale,
            waiting=not idle,
            refused=REFUSED.get(refused, ""),
            switches=[(switch, label, self.state.is_on(switch)) for switch, label in SWITCHES.items()],
            capture_on=capture_on,
            deploy_refused=deploy_refused(self._view, capture_on) if self._view else "",
            gitea_down=GITEA_DOWN,
            named=NAMED_RELOADS,
            target=self._target,
            history=self.state.history(),
            confirm=confirm,
            csrf=self.csrf,
        )

    def _queue(self, action: str, user: str, job: Callable[[], Result]) -> None:
        self.worker.submit(job, f"{action} by {user}")

    def _switch(self, form: dict[str, str], user: str) -> None:
        switch, on = Switch(form["switch"]), form["on"] == "1"
        self.state.set(switch, on)
        result = Result(Outcome.OK, f"{SWITCHES[switch]} {'switched on' if on else 'paused'}.")
        self.state.record("switch", result, user)
        log.info("%s by %s", result.message, user)
        if switch is Switch.CAPTURE and on:
            self.worker.submit(self.capture.run, "capture")

    def _deploy(self, form: dict[str, str], user: str) -> None:
        request = Request.parse(
            {"sha": form["sha"], "trigger": "manual", "impact": form.get("impact", "")[:MAX_IMPACT]}
        )
        self._queue(f"deploy {request.sha[:7]}", user, lambda: self.deploy.run(request, user))

    def _check(self, form: dict[str, str], user: str) -> None:
        self._queue("check", user, lambda: self.control.check(user))

    def _restart(self, form: dict[str, str], user: str) -> None:
        self._queue("restart", user, lambda: self.control.restart(user))

    def _reload(self, form: dict[str, str], user: str) -> None:
        target = form["target"]
        domain, service = reload_service(target)
        self._target = target
        self._queue(f"reload {target}", user, lambda: self.control.reload(domain, service, user))


def _handler(panel: Panel, ingress: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ha-gitops-sync"

        def log_message(self, format, *args):
            log.debug("panel: %s", format % args)

        def _send(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _back(self, refused: str = "") -> None:
            """To the page, which names a refusal; nothing was done then."""
            self.send_response(303)
            self.send_header("Location", f"{self._base()}/" + (f"?error={refused}" if refused else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _allowed(self) -> bool:
            if self.client_address[0] == ingress:
                return True
            self._send(403, "Only Home Assistant's ingress may open this panel.")
            return False

        def _base(self) -> str:
            return self.headers.get("X-Ingress-Path", "").rstrip("/")

        def do_GET(self):
            if not self._allowed():
                return
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/jobs":
                self._send(200, json.dumps({"running": panel.worker.running}), "application/json")
                return
            if url.path != "/":
                self._send(404, "Not found.")
                return
            query = urllib.parse.parse_qs(url.query)
            page = panel.page(self._base(), query.get("confirm", [""])[0], query.get("error", [""])[0])
            self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            if not self._allowed():
                return
            length = self.headers.get("Content-Length", "0")
            if not (length.isascii() and length.isdigit() and int(length) <= MAX_FORM):
                self._send(400, "The form is too large.")
                return
            form = dict(urllib.parse.parse_qsl(self.rfile.read(int(length)).decode()))
            user = self.headers.get("X-Remote-User-Name") or "unknown"
            action = panel.actions.get(self.path.strip("/"))
            if action is None:
                self._send(404, "Not found.")
                return
            if not hmac.compare_digest(form.get("csrf", "").encode(), panel.csrf.encode()):
                self._back("stale")
                return
            try:
                action(form, user)
            except (KeyError, ValueError) as e:
                log.info("panel: %s refused: %s", self.path, e)
                self._back("bad-form")
                return
            self._back()

    return Handler


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, panel: Panel, ingress: str = INGRESS):
        super().__init__(("0.0.0.0", port), _handler(panel, ingress))
