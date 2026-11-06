"""Wiring: options, logging, the worker, the start-up reconcile, the watcher, the panel and the signed API."""

from __future__ import annotations

import logging
import os
import signal
import ssl
import sys
import threading
from pathlib import Path

from ha_gitops_sync import http_api, panel
from ha_gitops_sync.auth import Auth
from ha_gitops_sync.capture import Capture
from ha_gitops_sync.control import Control
from ha_gitops_sync.deploy import Deploy
from ha_gitops_sync.git import Git
from ha_gitops_sync.gitea import Gitea
from ha_gitops_sync.options import load
from ha_gitops_sync.reconcile import Reconcile, StartError
from ha_gitops_sync.state import State, Switch
from ha_gitops_sync.supervisor import Supervisor
from ha_gitops_sync.watch import Watcher
from ha_gitops_sync.worker import Worker

log = logging.getLogger("ha_gitops_sync")

CONFIG = Path("/config")
DATA = Path("/data")
DAY = 24 * 3600


def main() -> None:
    options = load(DATA / "options.json")
    logging.basicConfig(
        level=options.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    certificate = http_api.Certificate()
    try:
        certificate.context()
    except (OSError, ssl.SSLError) as e:
        raise SystemExit(f"the signed API serves HTTPS with /ssl/fullchain.pem and /ssl/privkey.pem: {e}") from None

    git = Git(CONFIG, options.gitea_token)
    gitea = Gitea(options.gitea_url, options.repository, options.gitea_token)
    supervisor = Supervisor(os.environ["SUPERVISOR_TOKEN"], CONFIG)
    state = State(DATA / "state.json")
    worker = Worker()
    capture = Capture(git, gitea, supervisor, state, worker)
    deploy = Deploy(git, gitea, supervisor, state, capture)
    control = Control(supervisor, state)
    reconcile = Reconcile(git, options.remote_url, state, capture)
    try:
        worker.submit(reconcile.attach, "attaching /config").result()
    except StartError as e:
        raise SystemExit(f"not starting: {e}") from None
    worker.submit(reconcile.run, "start-up check")

    stopping = threading.Event()
    watcher = Watcher(git, lambda: worker.submit(capture.run, "capture"))
    ingress = panel.PanelServer(
        panel.PORT, panel.Panel(git, gitea, supervisor, state, worker, capture, deploy, control)
    )
    auth = Auth(options.allowed_sources)
    api = http_api.Server(
        http_api.PORT, http_api.Api(auth, options.deploy_key, options.control_key, worker, deploy, control), certificate
    )

    def daily() -> None:
        while not stopping.wait(DAY):
            worker.submit(reconcile.run, "daily check")

    def stop(signum, frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("stopping")
        watcher.stop()
        threading.Thread(target=lambda: (ingress.shutdown(), api.shutdown()), daemon=True).start()

    for target in (watcher.run, daily, ingress.serve_forever):
        threading.Thread(target=target, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "%s: panel on %s, signed API on %s for %s; auto-deploy %s, capture %s",
        options.repository,
        panel.PORT,
        http_api.PORT,
        ", ".join(map(str, options.allowed_sources)) or "no caller (allowed_sources is empty)",
        *("on" if state.is_on(switch) else "paused" for switch in Switch),
    )
    api.serve_forever()
    worker.shutdown()
