"""The signed HTTPS API for Gitea Actions on port 8765; the contract lists the endpoints and the answers."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import sys
import threading
from collections.abc import Callable
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO

from ha_gitops_sync.auth import Auth
from ha_gitops_sync.control import Control, reload_service
from ha_gitops_sync.deploy import Deploy, Request
from ha_gitops_sync.state import Outcome, Result
from ha_gitops_sync.worker import Worker

log = logging.getLogger(__name__)

PORT = 8765
MAX_BODY = 64 * 1024
SOCKET_TIMEOUT = 30
BY = "Actions"
CERTIFICATE = Path("/ssl/fullchain.pem")
PRIVATE_KEY = Path("/ssl/privkey.pem")

type Job = tuple[str, Callable[[], Result]]


class Certificate:
    """The TLS context, loaded again once the certificate or the key in /ssl has changed."""

    def __init__(self, certificate: Path = CERTIFICATE, key: Path = PRIVATE_KEY):
        self.certificate = certificate
        self.key = key
        self._lock = threading.Lock()
        self._loaded: tuple[tuple[int, int], ssl.SSLContext] | None = None

    def context(self) -> ssl.SSLContext:
        stamp = (self.certificate.stat().st_mtime_ns, self.key.stat().st_mtime_ns)
        with self._lock:
            if self._loaded is None or self._loaded[0] != stamp:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.certificate, self.key)
                self._loaded = (stamp, context)
            return self._loaded[1]


class Api:
    def __init__(
        self, auth: Auth, deploy_key: bytes, control_key: bytes, worker: Worker, deploy: Deploy, control: Control
    ):
        self.auth = auth
        self.worker = worker
        self.deploy = deploy
        self.control = control
        self._routes: dict[str, tuple[bytes, Callable[[dict], Job]]] = {
            "/api/deploy": (deploy_key, self._deploy),
            "/api/check": (control_key, self._check),
            "/api/reload": (control_key, self._reload),
            "/api/restart": (control_key, self._restart),
        }

    def _deploy(self, body: dict) -> Job:
        request = Request.parse(body)
        return f"deploy {request.sha[:7]} by {BY}", lambda: self.deploy.run(request, BY)

    def _check(self, body: dict) -> Job:
        return f"check by {BY}", lambda: self.control.check(BY)

    def _reload(self, body: dict) -> Job:
        target = body.get("target")
        domain, service = reload_service(target)
        return f"reload {target} by {BY}", lambda: self.control.reload(domain, service, BY)

    def _restart(self, body: dict) -> Job:
        return f"restart by {BY}", lambda: self.control.restart(BY)

    def post(self, path: str, source: str, headers: Message, stream: BinaryIO) -> Result:
        if path not in self._routes:
            return Result(Outcome.NOT_FOUND, f"There is no endpoint {path}.")
        key, job_for = self._routes[path]
        length = headers.get("Content-Length", "0")
        if not (length.isascii() and length.isdigit() and int(length) <= MAX_BODY):
            return Result(Outcome.BAD_REQUEST, f"The body must be at most {MAX_BODY} bytes, with its Content-Length.")
        body = stream.read(int(length))
        if not self.auth.verify(
            key, source, headers.get("X-GitOps-Timestamp"), headers.get("X-GitOps-Signature"), body
        ):
            log.warning("refused %s from %s", path, source)
            return Result(
                Outcome.UNAUTHORIZED,
                "Refused: not signed with this endpoint's key, outside the time window, replayed, or from a source "
                "that is not allowed.",
            )
        try:
            fields = json.loads(body)
            if not isinstance(fields, dict):
                raise ValueError("the body must be a JSON object")
            label, job = job_for(fields)
        except (ValueError, RecursionError) as e:
            return Result(Outcome.BAD_REQUEST, f"Bad request: {e}.")
        try:
            return self.worker.submit(job, label).result()
        except Exception as e:
            return Result(Outcome.ERROR, f"{label} failed: {e}")


def _handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ha-gitops-sync"

        def log_message(self, format, *args):
            log.debug("%s %s", self.client_address[0], format % args)

        def _answer(self, result: Result) -> None:
            data = json.dumps(result.answer()).encode()
            self.send_response(result.outcome.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                self._answer(Result(Outcome.OK, "Running."))
            else:
                self._answer(Result(Outcome.NOT_FOUND, f"There is no endpoint {self.path}."))

        def do_POST(self):
            result = api.post(self.path, self.client_address[0], self.headers, self.rfile)
            log.info("%s from %s: %s", self.path, self.client_address[0], result.outcome)
            self._answer(result)

    return Handler


class Server(ThreadingHTTPServer):
    """HTTPS on IPv6 and IPv4 at once; an IPv4 caller shows up as an IPv4-mapped address, which auth.py unmaps."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def __init__(self, port: int, api: Api, certificate: Certificate):
        self.certificate = certificate
        super().__init__(("::", port), _handler(api))

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def finish_request(self, request, client_address) -> None:
        request.settimeout(SOCKET_TIMEOUT)
        with self.certificate.context().wrap_socket(request, server_side=True) as tls:
            super().finish_request(tls, client_address)

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], ssl.SSLError | ConnectionError | TimeoutError):
            log.debug("connection from %s ended early", client_address[0], exc_info=True)
        else:
            log.exception("request from %s failed", client_address[0])
