"""What the Supervisor, Core and Gitea receive from the add-on, and how their answers are read."""

import json
import logging
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ha_gitops_sync import supervisor as supervisor_module
from ha_gitops_sync.api import ApiError
from ha_gitops_sync.gitea import Gitea
from ha_gitops_sync.supervisor import ESPHOME, CheckFailed, Supervisor, Unavailable

SHA = "b" * 40


class Peer:
    """A local HTTP server answering from a table of (method, path) and recording every request."""

    def __init__(self):
        self.answers: dict[tuple[str, str], tuple] = {}
        self.requests: list[tuple[str, str, str, object]] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                path = urllib.parse.urlsplit(self.path).path
                peer.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
                status, answer, *headers = peer.answers.get((self.command, path), (404, {"message": "not found"}))
                data = json.dumps(answer).encode()
                self.send_response(status)
                for name, value in (headers[0] if headers else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def peer():
    peer = Peer()
    yield peer
    peer.close()


def test_a_passing_check_asks_the_supervisor_with_the_add_ons_token(peer, tmp_path):
    peer.answers[("POST", "/core/check")] = (200, {"result": "ok", "data": {}})

    Supervisor("supervisor-token", tmp_path, peer.url).check()

    assert peer.requests == [("POST", "/core/check", "Bearer supervisor-token", None)]


def test_a_failing_check_carries_the_supervisors_output(peer, tmp_path):
    output = "Invalid config for 'light' at configuration.yaml, line 12"
    peer.answers[("POST", "/core/check")] = (400, {"result": "error", "message": output})

    with pytest.raises(CheckFailed) as failed:
        Supervisor("t", tmp_path, peer.url).check()

    assert failed.value.output == output


def test_the_check_output_loses_the_values_of_both_secrets_files(peer, tmp_path):
    (tmp_path / "secrets.yaml").write_text("wifi: hunter2-long\nport: 80\nnotify: true\n  nested: not-top-level\n")
    (tmp_path / "esphome").mkdir()
    (tmp_path / "esphome" / "secrets.yaml").write_text('# the OTA one\nota: "s3cret-ota"\n')
    output = "wifi 'hunter2-long' at line 3, ota s3cret-ota, port 80, notify true, nested not-top-level"
    peer.answers[("POST", "/core/check")] = (400, {"result": "error", "message": output})

    with pytest.raises(CheckFailed) as failed:
        Supervisor("t", tmp_path, peer.url).check()

    assert failed.value.output == "wifi '***' at line 3, ota ***, port 80, notify true, nested not-top-level"


def test_a_supervisor_error_or_silence_is_unavailable(peer, tmp_path):
    peer.answers[("POST", "/core/check")] = (502, {"message": "Bad Gateway"})

    with pytest.raises(Unavailable) as failed:
        Supervisor("t", tmp_path, peer.url).check()
    assert (failed.value.status, failed.value.message) == (502, "Bad Gateway")
    with pytest.raises(Unavailable):
        Supervisor("t", tmp_path, "http://127.0.0.1:9").restart()


def test_a_restart_not_back_in_time_may_still_be_in_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor_module, "RESTART_TIMEOUT", 0.5)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        with pytest.raises(Unavailable) as unanswered:
            Supervisor("t", tmp_path, f"http://127.0.0.1:{silent.getsockname()[1]}").restart()
    finally:
        silent.close()

    assert str(unanswered.value) == (
        "Home Assistant did not come back within 0.5 s; the restart may still be in progress"
    )


def test_reloads_and_events_go_through_the_core_proxy(peer, tmp_path):
    peer.answers[("GET", "/core/api/services")] = (
        200,
        [{"domain": "automation", "services": {"reload": {}, "trigger": {}}}, {"domain": "light", "services": {}}],
    )
    peer.answers[("POST", "/core/api/services/automation/reload")] = (200, [])
    peer.answers[("POST", "/core/api/events/gitops_deployed")] = (200, {"message": "Event gitops_deployed fired."})
    supervisor = Supervisor("t", tmp_path, peer.url)

    assert supervisor.services() == {"automation.reload", "automation.trigger"}
    supervisor.call_service("automation", "reload")
    supervisor.fire_event("gitops_deployed", {"commit": SHA, "by": "Actions"})

    assert [(method, path, body) for method, path, _, body in peer.requests[1:]] == [
        ("POST", "/core/api/services/automation/reload", {}),
        ("POST", "/core/api/events/gitops_deployed", {"commit": SHA, "by": "Actions"}),
    ]


def test_an_event_core_does_not_take_never_stops_the_action(peer, tmp_path):
    peer.answers[("POST", "/core/api/events/gitops_captured")] = (500, {"message": "boom"})

    Supervisor("t", tmp_path, peer.url).fire_event("gitops_captured", {"commit": SHA})


def test_the_esphome_version_comes_from_its_add_on_info(peer, tmp_path, caplog):
    peer.answers[("GET", f"/addons/{ESPHOME}/info")] = (200, {"result": "ok", "data": {"version": "2026.10.2"}})
    supervisor = Supervisor("t", tmp_path, peer.url)

    with caplog.at_level(logging.WARNING):
        assert supervisor.addon_version(ESPHOME) == "2026.10.2"
        assert supervisor.addon_version("not_installed") is None

    assert caplog.records == []


def test_an_add_on_version_the_supervisor_will_not_show_is_logged(peer, tmp_path, caplog):
    peer.answers[("GET", f"/addons/{ESPHOME}/info")] = (403, {"result": "error", "message": "Forbidden"})

    with caplog.at_level(logging.WARNING):
        assert Supervisor("t", tmp_path, peer.url).addon_version(ESPHOME) is None

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "status 403" in record.getMessage()
    assert "role may not be allowed to read another add-on's info" in record.getMessage()


def test_the_checks_gate_reads_the_latest_status_of_each_context(peer):
    statuses = [
        {"context": "ha/checks", "status": "success", "description": "all passed"},
        {"context": "ha/impact", "status": "success", "description": "reload automation"},
    ]
    peer.answers[("GET", f"/api/v1/repos/home/config/commits/{SHA}/status")] = (200, {"statuses": statuses})

    found = Gitea(peer.url, "home/config", "gitea-token").statuses(SHA)

    assert found["ha/checks"]["status"] == "success"
    assert found["ha/impact"]["description"] == "reload automation"
    assert peer.requests[0][2] == "token gitea-token"


def test_a_redirect_is_not_followed_so_the_token_never_reaches_another_host(peer):
    elsewhere = Peer()
    try:
        status = f"/api/v1/repos/home/config/commits/{SHA}/status"
        peer.answers[("GET", status)] = (302, {}, {"Location": f"{elsewhere.url}{status}"})

        with pytest.raises(ApiError) as refused:
            Gitea(peer.url, "home/config", "gitea-token").statuses(SHA)

        assert refused.value.status == 302
        assert refused.value.message == f"redirected to {elsewhere.url}{status}; not followed"
        assert elsewhere.requests == []
    finally:
        elsewhere.close()


def test_the_deploy_status_is_set_on_the_commit(peer):
    peer.answers[("POST", f"/api/v1/repos/home/config/statuses/{SHA}")] = (201, {})

    Gitea(peer.url, "home/config", "t").set_status(SHA, "success", "Deployed.", "https://gitea.example/run/2")

    assert peer.requests[0][3] == {
        "state": "success",
        "context": "ha/deploy",
        "description": "Deployed.",
        "target_url": "https://gitea.example/run/2",
    }


def test_the_live_to_main_pull_request_is_opened_once(peer):
    other = {"number": 3, "head": {"ref": "feature"}, "base": {"ref": "main"}, "html_url": "u3"}
    created = {"number": 4, "head": {"ref": "live"}, "base": {"ref": "main"}, "html_url": "u4"}
    peer.answers[("GET", "/api/v1/repos/home/config/pulls")] = (200, [other])
    peer.answers[("POST", "/api/v1/repos/home/config/pulls")] = (201, created)
    gitea = Gitea(peer.url, "home/config", "t")

    assert gitea.open_pull_request() == created
    method, _, _, sent = peer.requests[-1]
    assert (method, sent["head"], sent["base"]) == ("POST", "live", "main")
    assert sent["title"] == "Changes made in Home Assistant"

    peer.answers[("GET", "/api/v1/repos/home/config/pulls")] = (200, [other, created])
    peer.requests.clear()
    assert gitea.open_pull_request() == created
    assert [method for method, *_ in peer.requests] == ["GET"]
