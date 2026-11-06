import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import shutil
import ssl
import threading
import time

import pytest

from ha_gitops_sync.auth import Auth
from ha_gitops_sync.deploy import Request
from ha_gitops_sync.http_api import Api, Certificate, Server
from ha_gitops_sync.state import Outcome, Result
from ha_gitops_sync.worker import Worker

DEPLOY_KEY, CONTROL_KEY = b"d" * 64, b"c" * 64
SHA = "a" * 40
DEPLOY = {"sha": SHA, "trigger": "merge", "run_url": "https://gitea.example/run/4", "impact": "nothing"}


class Recorder:
    """Stands in for deploy and control; answers with the outcome a test sets."""

    def __init__(self):
        self.calls = []
        self.outcome = Outcome.DEPLOYED

    def run(self, request, by):
        self.calls.append(("deploy", request, by))
        return Result(self.outcome, "Done.")

    def check(self, by):
        self.calls.append(("check", by))
        return Result(Outcome.CHECKED, "Passed.")

    def reload(self, domain, service, by):
        self.calls.append(("reload", domain, service, by))
        return Result(Outcome.RELOADED, "Reloaded.")

    def restart(self, by):
        self.calls.append(("restart", by))
        return Result(Outcome.RESTARTED, "Back.", seconds=12)


class Harness:
    def __init__(self, tmp_path, tls):
        self.tls = tls
        self.client = ssl.create_default_context(cadata=(tls / "a.pem").read_text() + (tls / "b.pem").read_text())
        self.cert, self.key = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
        shutil.copy(tls / "a.pem", self.cert)
        shutil.copy(tls / "a.key", self.key)
        self.recorder = Recorder()
        self.worker = Worker()
        self.servers = []

    def serve(self, allowed=("127.0.0.1/32",)) -> int:
        auth = Auth(tuple(ipaddress.ip_network(network) for network in allowed))
        api = Api(auth, DEPLOY_KEY, CONTROL_KEY, self.worker, self.recorder, self.recorder)
        server = Server(0, api, Certificate(self.cert, self.key))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def connect(self, port: int) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection("127.0.0.1", port, context=self.client, timeout=10)

    def post(self, port, path, data, headers):
        connection = self.connect(port)
        connection.request("POST", path, data, headers)
        response = connection.getresponse()
        answer = response.status, json.loads(response.read())
        connection.close()
        return answer

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.worker.shutdown()


@pytest.fixture
def harness(tmp_path, tls):
    harness = Harness(tmp_path, tls)
    yield harness
    harness.close()


def signed(body, key, timestamp=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    stamp = str(int(time.time())) if timestamp is None else str(timestamp)
    signature = "sha256=" + hmac.new(key, stamp.encode() + b"." + data, hashlib.sha256).hexdigest()
    return data, {"Content-Type": "application/json", "X-GitOps-Timestamp": stamp, "X-GitOps-Signature": signature}


def test_a_signed_deploy_runs_and_its_answer_comes_back(harness):
    port = harness.serve()

    status, answer = harness.post(port, "/api/deploy", *signed(DEPLOY, DEPLOY_KEY))

    assert status == 200
    assert answer == {"outcome": "deployed", "message": "Done."}
    assert harness.recorder.calls == [
        ("deploy", Request(SHA, "merge", "https://gitea.example/run/4", "nothing"), "Actions")
    ]


def test_control_endpoints_take_the_control_key(harness):
    port = harness.serve()
    now = int(time.time())

    assert harness.post(port, "/api/check", *signed({}, CONTROL_KEY, now))[0] == 200
    assert harness.post(port, "/api/reload", *signed({"target": "automation"}, CONTROL_KEY, now))[0] == 200
    assert harness.post(port, "/api/restart", *signed({}, CONTROL_KEY, now - 1)) == (
        200,
        {"outcome": "restarted", "message": "Back.", "seconds": 12},
    )
    assert harness.recorder.calls == [
        ("check", "Actions"),
        ("reload", "automation", "reload", "Actions"),
        ("restart", "Actions"),
    ]


@pytest.mark.parametrize(
    ("target", "domain", "service"),
    [
        ("all", "homeassistant", "reload_all"),
        ("core", "homeassistant", "reload_core_config"),
        ("themes", "frontend", "reload_themes"),
        ("automation", "automation", "reload"),
    ],
)
def test_a_reload_target_names_its_service(harness, target, domain, service):
    port = harness.serve()

    assert harness.post(port, "/api/reload", *signed({"target": target}, CONTROL_KEY))[0] == 200
    assert harness.recorder.calls == [("reload", domain, service, "Actions")]


def test_bodies_with_a_nonce_are_accepted(harness):
    port = harness.serve()
    now = int(time.time())

    assert harness.post(port, "/api/check", *signed({"nonce": "4f1c9e2ab37d6e05"}, CONTROL_KEY, now))[0] == 200
    assert harness.post(port, "/api/restart", *signed({"nonce": "b0e8a3d915c7f264"}, CONTROL_KEY, now))[0] == 200
    assert harness.post(port, "/api/deploy", *signed(DEPLOY | {"nonce": "9d2e"}, DEPLOY_KEY, now))[0] == 200
    assert harness.recorder.calls == [
        ("check", "Actions"),
        ("restart", "Actions"),
        ("deploy", Request(SHA, "merge", "https://gitea.example/run/4", "nothing"), "Actions"),
    ]


@pytest.mark.parametrize(
    ("path", "body", "key"),
    [("/api/deploy", DEPLOY, CONTROL_KEY), ("/api/check", {}, DEPLOY_KEY), ("/api/restart", {}, DEPLOY_KEY)],
)
def test_each_endpoint_refuses_the_other_key(harness, path, body, key):
    port = harness.serve()

    status, answer = harness.post(port, path, *signed(body, key))

    assert (status, answer["outcome"]) == (401, "unauthorized")
    assert harness.recorder.calls == []


@pytest.mark.parametrize(
    "spoil",
    [
        lambda headers: headers.pop("X-GitOps-Signature"),
        lambda headers: headers.pop("X-GitOps-Timestamp"),
        lambda headers: headers.update({"X-GitOps-Signature": "sha256=" + "0" * 64}),
        lambda headers: headers.update({"X-GitOps-Signature": headers["X-GitOps-Signature"].removeprefix("sha256=")}),
        lambda headers: headers.update({"X-GitOps-Timestamp": "soon"}),
    ],
)
def test_a_missing_or_wrong_signature_is_refused(harness, spoil):
    port = harness.serve()
    data, headers = signed(DEPLOY, DEPLOY_KEY)
    spoil(headers)

    assert harness.post(port, "/api/deploy", data, headers)[0] == 401
    assert harness.recorder.calls == []


def test_a_signature_over_a_different_body_is_refused(harness):
    port = harness.serve()
    _, headers = signed(DEPLOY, DEPLOY_KEY)

    assert harness.post(port, "/api/deploy", json.dumps(DEPLOY | {"trigger": "manual"}).encode(), headers)[0] == 401


@pytest.mark.parametrize(("age", "status"), [(-301, 401), (301, 401), (-290, 200), (290, 200)])
def test_the_timestamp_must_be_within_300_seconds_either_way(harness, age, status):
    port = harness.serve()

    assert harness.post(port, "/api/check", *signed({}, CONTROL_KEY, int(time.time()) + age))[0] == status


def test_a_replayed_request_is_refused(harness):
    port = harness.serve()
    request = signed(DEPLOY, DEPLOY_KEY)

    assert harness.post(port, "/api/deploy", *request)[0] == 200
    assert harness.post(port, "/api/deploy", *request)[0] == 401
    assert len(harness.recorder.calls) == 1


def test_a_signed_check_cannot_be_replayed_as_a_restart(harness):
    port = harness.serve()
    request = signed({}, CONTROL_KEY)

    assert harness.post(port, "/api/check", *request)[0] == 200
    assert harness.post(port, "/api/restart", *request)[0] == 401
    assert harness.recorder.calls == [("check", "Actions")]


@pytest.mark.parametrize("allowed", [("10.0.0.0/8", "2001:db8::/32"), ()])
def test_callers_outside_the_allowed_sources_are_refused(harness, allowed):
    port = harness.serve(allowed)

    assert harness.post(port, "/api/deploy", *signed(DEPLOY, DEPLOY_KEY))[0] == 401
    assert harness.recorder.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/deploy", b"not json"),
        ("/api/deploy", b"[1, 2]"),
        ("/api/deploy", {"sha": "abc", "trigger": "merge"}),
        ("/api/deploy", {"sha": SHA, "trigger": "push"}),
        ("/api/reload", {"target": "../secrets"}),
        ("/api/reload", {}),
        ("/api/check", b"[" * 30000 + b"]" * 30000),
    ],
)
def test_malformed_requests_are_bad_requests(harness, path, body):
    port = harness.serve()
    key = DEPLOY_KEY if path == "/api/deploy" else CONTROL_KEY

    status, answer = harness.post(port, path, *signed(body, key))

    assert (status, answer["outcome"]) == (400, "bad-request")
    assert harness.recorder.calls == []


@pytest.mark.parametrize("length", [str(64 * 1024 + 1), "\xb2"], ids=["over 64 KiB", "superscript two"])
def test_a_body_over_64_kib_or_without_a_plain_length_is_refused_unread(harness, length):
    port = harness.serve()
    connection = harness.connect(port)
    connection.putrequest("POST", "/api/deploy")
    connection.putheader("Content-Length", length)
    connection.endheaders()

    response = connection.getresponse()

    assert (response.status, json.loads(response.read())["outcome"]) == (400, "bad-request")


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("deployed", 200),
        ("already-live", 200),
        ("not-on-main", 409),
        ("checks-not-passed", 409),
        ("conflict", 409),
        ("dirty", 409),
        ("check-failed", 422),
        ("paused", 423),
        ("unavailable", 503),
    ],
)
def test_each_deploy_outcome_has_the_contract_status(harness, outcome, status):
    harness.recorder.outcome = Outcome(outcome)
    port = harness.serve()

    assert harness.post(port, "/api/deploy", *signed(DEPLOY, DEPLOY_KEY)) == (
        status,
        {"outcome": outcome, "message": "Done."},
    )


def test_healthz_answers_without_a_signature(harness):
    port = harness.serve()
    connection = harness.connect(port)
    connection.request("GET", "/healthz")
    response = connection.getresponse()

    assert (response.status, json.loads(response.read())["outcome"]) == (200, "ok")


def test_a_renewed_certificate_is_served_without_a_restart(harness):
    port = harness.serve()

    def served() -> bytes:
        connection = harness.connect(port)
        connection.request("GET", "/healthz")
        certificate = connection.sock.getpeercert(binary_form=True)
        connection.getresponse().read()
        connection.close()
        return certificate

    assert served() == ssl.PEM_cert_to_DER_cert((harness.tls / "a.pem").read_text())
    shutil.copy(harness.tls / "b.pem", harness.cert)
    shutil.copy(harness.tls / "b.key", harness.key)
    later = time.time() + 5
    for path in (harness.cert, harness.key):
        os.utime(path, (later, later))

    assert served() == ssl.PEM_cert_to_DER_cert((harness.tls / "b.pem").read_text())


def test_ipv4_mapped_callers_are_compared_as_ipv4():
    auth = Auth((ipaddress.ip_network("192.0.2.10/32"),))

    assert auth.source_allowed("::ffff:192.0.2.10")
    assert not auth.source_allowed("::ffff:192.0.2.11")
