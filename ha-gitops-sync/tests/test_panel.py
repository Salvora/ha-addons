import html
import http.client
import json
import re
import threading
import time
import urllib.parse

import pytest

from ha_gitops_sync import panel as panel_module
from ha_gitops_sync.panel import Panel, PanelServer
from ha_gitops_sync.state import Outcome, Switch

INGRESS_PATH = "/api/hassio_ingress/Xs3V2kfa"


class Browser:
    """What Home Assistant's ingress forwards: the session's user and the path the panel is served under."""

    def __init__(self, port: int, user: str | None = "alex"):
        self.port = port
        self.headers = {"X-Ingress-Path": INGRESS_PATH} | ({"X-Remote-User-Name": user} if user else {})

    def get(self, path: str = "/") -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("GET", path.removeprefix(INGRESS_PATH), headers=self.headers)
        response = connection.getresponse()
        return response.status, response.read().decode()

    def post(self, path: str, form: dict) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = urllib.parse.urlencode(form)
        headers = self.headers | {"Content-Type": "application/x-www-form-urlencoded"}
        connection.request("POST", path, body, headers)
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader("Location") or ""

    def press(self, path: str, **fields) -> tuple[int, str]:
        """A button on the page, with the form's own token."""
        token = re.search(r'name="csrf" value="([^"]+)"', self.get()[1]).group(1)
        return self.post(path, {"csrf": token, **fields})


def serve(system, ingress="127.0.0.1"):
    panel = Panel(
        system.git,
        system.gitea,
        system.supervisor,
        system.state,
        system.worker,
        system.capture,
        system.deploy,
        system.control,
    )
    server = PanelServer(0, panel, ingress)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def browser(system):
    server = serve(system)
    yield Browser(server.server_address[1])
    server.shutdown()
    server.server_close()


@pytest.fixture
def busy(system):
    """A job the worker runs until the test lets it finish."""
    release = threading.Event()
    system.worker.submit(release.wait, "a capture")
    yield
    release.set()
    settle(system)


def settle(system):
    deadline = time.monotonic() + 60
    while not system.worker.idle:
        assert time.monotonic() < deadline, "the worker did not finish"
        time.sleep(0.05)


def deploy_button(page: str) -> str:
    return re.search(r"<button[^>]*>Deploy [0-9a-f]{7}</button>", page).group(0)


def row(page: str, label: str) -> str:
    """The text of the status row with this label."""
    found = re.search(rf"<dt>{re.escape(label)}</dt>\s*<dd>(.*?)</dd>", page, re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", found.group(1))).split())


def damage_live(system) -> None:
    """The branch HEAD names points at an object git cannot read."""
    loose = system.root / ".git" / "objects" / "ab" / ("0" * 38)
    loose.parent.mkdir(exist_ok=True)
    loose.write_bytes(b"not a git object")
    (system.root / ".git" / "refs" / "heads" / "live").write_text("ab" + "0" * 38 + "\n")


def test_the_page_shows_live_main_and_both_switches_paused(system, browser):
    status, page = browser.get()

    assert status == 200
    live = system.head()
    assert f'href="https://gitea.example/home/config/commit/{live}"' in page
    assert page.count('aria-checked="false"') == 2
    assert "<script>" not in page
    assert f'action="{INGRESS_PATH}/check"' in page
    assert f'href="{INGRESS_PATH}/?confirm=restart"' in page
    assert row(page, "Main in Gitea").endswith("live")
    assert row(page, "live → main") == "no pull request open"


def test_only_home_assistants_ingress_may_open_the_panel(system):
    server = serve(system, ingress="172.30.32.2")
    stranger = Browser(server.server_address[1])
    try:
        assert stranger.get()[0] == 403
        assert stranger.get("/jobs")[0] == 403
        assert stranger.post("/check", {})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
    assert system.supervisor.checks == 0


@pytest.mark.parametrize("token", [None, "forged"])
def test_a_form_without_the_pages_token_does_nothing_and_says_the_page_was_out_of_date(system, browser, token):
    form = {"csrf": token} if token else {}

    status, location = browser.post("/check", form)

    assert (status, location) == (303, f"{INGRESS_PATH}/?error=stale")
    settle(system)
    assert system.supervisor.checks == 0
    assert "This page was out of date, so nothing was done. Press the button again." in browser.get(location)[1]


def test_a_form_the_panel_does_not_understand_does_nothing_and_says_so(system, browser):
    history = system.state.history()

    status, location = browser.press("/switch", switch="everything", on="1")

    assert (status, location) == (303, f"{INGRESS_PATH}/?error=bad-form")
    assert not system.state.is_on(Switch.CAPTURE)
    assert system.state.history() == history
    assert "The form was not understood, so nothing was done." in browser.get(location)[1]


def test_an_action_is_queued_shown_running_then_its_result(system, browser):
    release = threading.Event()
    system.worker.submit(release.wait, "a capture")
    try:
        status, location = browser.press("/check")
        assert (status, location) == (303, f"{INGRESS_PATH}/")
        page = browser.get()[1]
        assert "check by alex" in page
    finally:
        release.set()
    settle(system)

    page = browser.get()[1]
    assert "The config check passed." in page
    entry = system.state.history()[0]
    assert (entry.action, entry.by, entry.result.outcome) == ("check", "alex", Outcome.CHECKED)


def test_while_an_action_runs_the_page_asks_for_the_jobs_and_offers_no_action(system, browser, busy):
    page = browser.get()[1]

    assert 'http-equiv="refresh"' not in page
    assert f'fetch("{INGRESS_PATH}/jobs"' in page
    assert re.search(r'<button type="button" class="restart" disabled>Restart…</button>', page)
    assert f'href="{INGRESS_PATH}/?confirm=restart"' not in page
    assert re.search(r"<button type=\"submit\" disabled>Check config</button>", page)


def test_the_jobs_name_what_runs_and_nothing_once_idle(system, browser):
    release = threading.Event()
    system.worker.submit(release.wait, "a capture")
    try:
        status, text = browser.get("/jobs")
        assert (status, json.loads(text)) == (200, {"running": ["a capture"]})
    finally:
        release.set()
    settle(system)

    assert json.loads(browser.get("/jobs")[1]) == {"running": []}


def test_deploy_is_offered_only_once_the_checks_passed(system, browser):
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"}, "Add an automation")
    system.gitea.checks_url[sha] = "https://gitea.example/home/config/actions/runs/3"

    assert "disabled" in deploy_button(browser.get()[1])

    system.gitea.checks[sha] = "success"
    page = browser.get()[1]
    assert "disabled" not in deploy_button(page)
    assert re.search(r'<a href="https://gitea.example/home/config/actions/runs/3"[^>]*><span class="chip ok">', page)
    assert browser.press("/deploy", sha=sha)[0] == 303
    settle(system)

    assert system.remote_rev("live") == sha
    assert system.read("automations.yaml") == "- id: 'from main'\n"
    assert system.state.history()[0].by == "alex"


def test_a_deploy_from_the_panel_names_what_the_commit_needs(system, browser):
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"})
    system.gitea.checks[sha] = "success"
    system.gitea.impact[sha] = "reload automation"
    impact = re.search(r'name="impact" value="([^"]*)"', browser.get()[1]).group(1)

    assert browser.press("/deploy", sha=sha, impact=impact)[0] == 303
    settle(system)

    assert "needs: reload automation" in system.state.history()[0].result.message


def test_deploy_waits_for_capture_while_edits_are_not_committed(system, browser):
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"})
    system.gitea.checks[sha] = "success"
    system.write("scripts.yaml", "edited: not captured\n")

    page = browser.get()[1]

    assert "disabled" in deploy_button(page)
    assert "Switch Capture on first: 1 file not committed" in row(page, "Main in Gitea")


def test_deploy_waits_while_main_conflicts_with_live(system, browser):
    system.state.set(Switch.CAPTURE, True)
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"})
    system.gitea.checks[sha] = "success"
    system.write("automations.yaml", "- id: 'from home assistant'\n")
    system.capture.run()

    page = browser.get()[1]

    assert "disabled" in deploy_button(page)
    assert "Resolve the conflict first (see #7)" in row(page, "Main in Gitea")
    assert row(page, "live → main").endswith("conflict")


def test_a_pull_request_gitea_calls_unmergeable_does_not_hold_a_deploy_that_merges_cleanly(system, browser):
    system.state.set(Switch.CAPTURE, True)
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"})
    system.gitea.checks[sha] = "success"
    system.write("scripts.yaml", "edited: in home assistant\n")
    system.capture.run()
    system.gitea.pulls[0]["mergeable"] = False

    page = browser.get()[1]

    assert "disabled" not in deploy_button(page)
    assert "Resolve the conflict first" not in page
    assert "conflict" not in row(page, "live → main")


def test_when_giteas_api_does_not_answer_checks_and_pull_request_are_unknown(system, browser):
    sha = system.dev.commit({"automations.yaml": "- id: 'from main'\n"})
    system.gitea.checks[sha] = "success"
    system.gitea.down = True

    page = browser.get()[1]

    main = row(page, "Main in Gitea")
    assert main.count("Gitea's API is not answering: checks and pull request unknown") == 1
    assert "checks passed" not in main
    assert "disabled" in deploy_button(page)
    assert row(page, "live → main") == "pull request unknown"


def test_when_the_fetch_fails_main_is_shown_as_last_fetched_and_nothing_as_current(system, browser):
    system.remote.rename(system.remote.with_name("away.git"))

    page = browser.get()[1]

    main = row(page, "Main in Gitea")
    assert "Gitea is not answering; showing main as last fetched." in main
    assert not main.endswith("live")
    assert row(page, "live → main") == "pull request unknown"


def test_a_status_that_cannot_be_read_is_named_and_the_last_one_stays(system, browser):
    assert "Live on Home Assistant" in browser.get()[1]
    damage_live(system)

    status, page = browser.get()

    assert status == 200
    assert "The status could not be read: " in page
    assert "Open the panel again to retry. Showing the status read at " in page
    assert "Live on Home Assistant" in page

    other = serve(system)
    try:
        page = Browser(other.server_address[1]).get()[1]
    finally:
        other.shutdown()
        other.server_close()
    assert "The status could not be read: " in page
    assert "Open the panel again to retry." in page
    assert "Showing the status read at" not in page
    assert "Live on Home Assistant" not in page


def test_a_status_read_that_takes_too_long_shows_the_last_one_with_its_time_and_runs_on(system, browser, monkeypatch):
    assert "Live on Home Assistant" in browser.get()[1]
    monkeypatch.setattr(panel_module, "GATHER_TIMEOUT", 0.5)
    monkeypatch.setattr(panel_module, "CALL_TIMEOUT", 2)
    system.supervisor.services_unanswered = True

    page = browser.get()[1]

    assert "Gitea or Home Assistant did not answer within 0.5 s. Showing the status read at " in page
    assert "Live on Home Assistant" in page
    assert "Running: reading the status" in page
    assert f'fetch("{INGRESS_PATH}/jobs"' in page
    settle(system)


def test_a_core_that_does_not_answer_still_gives_a_page_offering_the_named_reloads(system, browser, monkeypatch):
    monkeypatch.setattr(panel_module, "CALL_TIMEOUT", 0.5)
    system.supervisor.services_unanswered = True
    started = time.monotonic()

    page = browser.get()[1]

    assert time.monotonic() - started < 5
    assert "Showing the status read at" not in page
    assert re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', page) == [
        ("all", "All YAML"),
        ("core", "Core (location, customize)"),
        ("themes", "Themes"),
    ]


def test_the_first_page_opened_while_an_action_runs_says_when_the_status_is_read(system, browser, busy):
    page = browser.get()[1]

    assert "The status is read once the running action has finished." in page
    assert "banner warn" not in page
    assert "Live on Home Assistant" not in page


def test_restart_asks_for_confirmation_first(system, browser):
    page = browser.get()[1]
    assert f'action="{INGRESS_PATH}/restart"' not in page

    page = browser.get("/?confirm=restart")[1]
    assert "Restart Home Assistant?" in page
    assert "The panel updates by itself once Home Assistant is back." in page
    assert f'action="{INGRESS_PATH}/restart"' in page

    assert browser.press("/restart")[0] == 303
    settle(system)
    assert system.supervisor.restarts == 1


def test_switching_capture_on_captures_what_is_waiting(system, browser):
    system.write("automations.yaml", "- id: 'edited while paused'\n")

    assert browser.press("/switch", switch="capture", on="1")[0] == 303
    settle(system)

    assert system.state.is_on(Switch.CAPTURE)
    assert system.remote_show("main", "automations.yaml") == "- id: 'edited while paused'\n"
    assert any(e.action == "switch" and e.by == "alex" for e in system.state.history())


def test_a_reload_offers_what_home_assistant_can_reload_and_keeps_the_last_choice(system, browser):
    page = browser.get()[1]
    options = re.findall(r'<option value="([^"]+)"( selected)?>([^<]+)</option>', page)

    assert options == [
        ("all", " selected", "All YAML"),
        ("core", "", "Core (location, customize)"),
        ("themes", "", "Themes"),
        ("automation", "", "automation"),
        ("script", "", "script"),
    ]

    assert browser.press("/reload", target="script")[0] == 303
    settle(system)
    assert system.supervisor.called == ["script.reload"]
    assert re.findall(r'<option value="([^"]+)" selected>', browser.get()[1]) == ["script"]


def test_edits_waiting_and_files_left_out_are_named(system, browser):
    system.write("automations.yaml", "- id: 'edited'\n")
    system.write("ci/on_home_assistant.py", "print()\n")

    edits = row(browser.get()[1], "Home Assistant edits")

    assert "1 file not committed yet" in edits
    assert "capture is paused" in edits
    assert "automations.yaml" in edits
    assert "left out: ci/on_home_assistant.py" in edits


def test_more_than_five_edits_are_listed_in_a_details(system, browser):
    names = [f"automations/room_{n}.yaml" for n in range(6)]
    for name in names:
        system.write(name, "- id: 'x'\n")

    page = browser.get()[1]

    listed = re.search(r"<details><summary>6 files</summary>(.*?)</details>", page, re.S).group(1)
    assert re.findall(r"<li>([^<]+)</li>", listed) == names


def test_each_recent_entry_names_its_files(system, browser):
    system.state.set(Switch.CAPTURE, True)
    system.write("automations.yaml", "- id: 'edited'\n")
    system.write("scripts.yaml", "edited: too\n")
    system.capture.run()

    page = browser.get()[1]

    listed = re.search(r"<details><summary>2 files</summary>(.*?)</details>", page, re.S).group(1)
    assert re.findall(r"<li>([^<]+)</li>", listed) == ["automations.yaml", "scripts.yaml"]


def test_without_a_user_header_the_action_is_by_unknown(system):
    server = serve(system)
    try:
        assert Browser(server.server_address[1], user=None).press("/check")[0] == 303
        settle(system)
    finally:
        server.shutdown()
        server.server_close()
    assert system.state.history()[0].by == "unknown"
