import shutil
import subprocess

import pytest

from ha_gitops_sync.deploy import Request
from ha_gitops_sync.state import Outcome, Switch


def checked(system, files, message="Change"):
    """A commit pushed to main whose checks passed."""
    sha = system.dev.commit(files, message)
    system.gitea.checks[sha] = "success"
    return sha


def contents(root) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_a_fast_forward_deploy_places_the_files_deletes_removed_ones_and_pushes_live(system):
    sha = checked(system, {"automations.yaml": "- id: '2'\n", "scripts.yaml": None}, "Swap the automation")

    result = system.deploy.run(Request(sha, "manual", run_url="https://gitea.example/run/5"), "Actions")

    assert result.outcome is Outcome.DEPLOYED
    assert system.read("automations.yaml") == "- id: '2'\n"
    assert not (system.root / "scripts.yaml").exists()
    assert system.remote_rev("live") == sha
    assert system.remote_rev("main") == sha
    assert system.supervisor.checks == 1
    assert system.gitea.deploy_statuses == [(sha, "success")]
    assert system.supervisor.events == [
        ("gitops_deployed", {"commit": sha, "files": ["automations.yaml", "scripts.yaml"], "by": "Actions"})
    ]
    assert not system.supervisor.called and system.supervisor.restarts == 0


def test_a_deploy_names_what_the_commit_needs(system):
    sha = checked(system, {"automations.yaml": "- id: '2'\n"})

    result = system.deploy.run(Request(sha, "manual", impact="reload automation"), "Actions")

    assert result.outcome is Outcome.DEPLOYED
    assert "needs: reload automation" in result.message


def test_a_deploy_merges_edits_made_in_home_assistant_and_moves_main_along(system):
    system.state.set(Switch.CAPTURE, True)
    sha = checked(system, {"automations.yaml": "- id: 'from main'\n"})
    system.write("scripts.yaml", "edited: in home assistant\n")
    captured = system.capture.run()
    assert system.remote_rev("main") == sha
    assert system.remote_rev("live") == captured.live

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.DEPLOYED
    assert system.read("automations.yaml") == "- id: 'from main'\n"
    assert system.read("scripts.yaml") == "edited: in home assistant\n"
    live = system.remote_rev("live")
    assert system.remote_parents(live) == [captured.live, sha]
    assert system.remote_rev("main") == live


def test_a_deploy_captures_pending_edits_first_when_capture_is_on(system):
    system.state.set(Switch.CAPTURE, True)
    sha = checked(system, {"automations.yaml": "- id: 'from main'\n"})
    system.write("scripts.yaml", "edited: just now\n")

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.DEPLOYED
    live = system.remote_rev("live")
    assert system.remote_show(live, "scripts.yaml") == "edited: just now\n"
    assert system.remote_show(live, "automations.yaml") == "- id: 'from main'\n"
    assert system.git.run("status", "--porcelain") == ""


def test_a_conflict_is_refused_and_config_is_untouched(system):
    system.state.set(Switch.CAPTURE, True)
    sha = checked(system, {"automations.yaml": "- id: 'from main'\n"})
    system.write("automations.yaml", "- id: 'from home assistant'\n")
    system.capture.run()
    live = system.remote_rev("live")

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.CONFLICT
    assert result.pull_request == system.gitea.pulls[0]["html_url"]
    assert system.read("automations.yaml") == "- id: 'from home assistant'\n"
    assert system.head() == live == system.remote_rev("live")
    assert system.git.run("status", "--porcelain") == ""
    assert system.supervisor.checks == 0
    assert system.gitea.deploy_statuses == [(sha, "failure")]
    assert [event for event, _ in system.supervisor.events][-1] == "gitops_conflict"


@pytest.mark.parametrize(
    ("failing", "down", "outcome"),
    [("Invalid config for [automation]", False, Outcome.CHECK_FAILED), (None, True, Outcome.UNAVAILABLE)],
)
def test_without_a_passing_check_the_files_are_put_back(system, failing, down, outcome):
    before = system.read("automations.yaml")
    live = system.remote_rev("live")
    sha = checked(system, {"automations.yaml": "broken: [\n"})
    seen = []
    system.supervisor.on_check = lambda: seen.append(system.read("automations.yaml"))
    system.supervisor.failing_check, system.supervisor.down = failing, down

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is outcome
    assert system.read("automations.yaml") == before
    assert system.head() == live == system.remote_rev("live")
    assert system.git.run("status", "--porcelain") == ""
    if failing:
        assert seen == ["broken: [\n"]
        assert result.output == failing
        assert system.gitea.deploy_statuses == [(sha, "failure")]
        assert system.supervisor.events[-1][0] == "gitops_deploy_failed"


def test_an_edit_made_while_the_check_ran_survives_the_put_back(system):
    before = system.read("automations.yaml")
    sha = checked(system, {"automations.yaml": "broken: [\n"})
    system.supervisor.on_check = lambda: system.write("scripts.yaml", "edited: during the check\n")
    system.supervisor.failing_check = "Invalid config"

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.CHECK_FAILED
    assert system.read("automations.yaml") == before
    assert system.read("scripts.yaml") == "edited: during the check\n"


def test_a_placed_file_edited_while_the_check_ran_stays_as_edited_and_never_reaches_live(system):
    live = system.remote_rev("live")
    before = contents(system.root)
    sha = checked(
        system,
        {
            "automations.yaml": "broken: [\n",
            "scripts.yaml": "from: main\n",
            "packages/heating.yaml": "climate: []\n",
        },
    )
    edit = "- id: 'edited during the check'\n"
    system.supervisor.on_check = lambda: system.write("automations.yaml", edit)
    system.supervisor.failing_check = "Invalid config"

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.CHECK_FAILED
    assert result.files == ("automations.yaml",)
    assert system.head() == live
    assert contents(system.root) == before | {"automations.yaml": edit}
    assert not (system.root / "packages").exists()
    assert system.gitea.deploy_statuses == [(sha, "failure")]

    system.state.set(Switch.CAPTURE, True)
    system.capture.run()

    assert system.remote_show("live", "automations.yaml") == edit
    on_live = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "live"], cwd=system.remote).returncode
    assert on_live == 1


def test_uncommitted_edits_refuse_the_deploy_while_capture_is_paused(system):
    before = system.read("automations.yaml")
    head = system.head()
    sha = checked(system, {"automations.yaml": "- id: 'from main'\n"})
    system.write("scripts.yaml", "edited: not captured\n")

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.DIRTY
    assert result.files == ("scripts.yaml",)
    assert system.read("automations.yaml") == before
    assert system.read("scripts.yaml") == "edited: not captured\n"
    assert system.head() == head
    assert system.supervisor.checks == 0


def test_a_resave_that_only_changes_line_endings_does_not_hold_a_deploy_while_capture_is_paused(system):
    resaved = system.root / "automations.yaml"
    resaved.write_bytes(resaved.read_bytes().replace(b"\n", b"\r\n"))
    sha = checked(system, {"scripts.yaml": "from: main\n"})

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.DEPLOYED
    assert system.remote_rev("live") == sha


def test_a_deploy_replaces_a_file_whose_line_endings_alone_were_changed(system):
    system.state.set(Switch.CAPTURE, True)
    resaved = system.root / "configuration.yaml"
    resaved.write_bytes(resaved.read_bytes().replace(b"\n", b"\r\n"))
    assert system.capture.run() is None
    text = "homeassistant:\n  name: Home, from main\nautomation: !include automations.yaml\n"
    sha = checked(system, {"configuration.yaml": text})

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.DEPLOYED
    assert resaved.read_bytes() == text.encode()


def test_a_file_whose_line_endings_alone_were_changed_is_put_back_after_a_failed_check(system):
    system.state.set(Switch.CAPTURE, True)
    resaved = system.root / "configuration.yaml"
    previous = resaved.read_bytes()
    resaved.write_bytes(previous.replace(b"\n", b"\r\n"))
    assert system.capture.run() is None
    sha = checked(system, {"configuration.yaml": "broken: [\n"})
    system.supervisor.failing_check = "Invalid config"

    result = system.deploy.run(Request(sha, "manual"), "alex")

    assert result.outcome is Outcome.CHECK_FAILED
    assert resaved.read_bytes() == previous


def test_a_restored_backup_a_deploy_finds_reaches_live_though_the_deploy_is_refused(system, tmp_path):
    backup = tmp_path / "backup"
    shutil.copytree(system.root, backup)
    restored = system.head()
    system.state.set(Switch.CAPTURE, True)
    system.write("scripts.yaml", "edited: after the backup\n")
    before = system.capture.run().live
    system.root.rename(tmp_path / "replaced")
    shutil.copytree(backup, system.root)
    sha = system.dev.commit({"automations.yaml": "- id: 'unchecked'\n"})

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.CHECKS_NOT_PASSED
    live = system.remote_rev("live")
    assert system.remote_log(live, "%s") == f"Restored from backup: back to {restored[:7]}"
    assert system.remote_parents(live) == [before]
    assert system.remote_show(live, "scripts.yaml") == "{}\n"


def test_a_commit_already_live_is_not_placed_again(system):
    live = system.remote_rev("live")
    system.gitea.checks[live] = "success"

    result = system.deploy.run(Request(live, "manual"), "Actions")

    assert result.outcome is Outcome.ALREADY_LIVE
    assert result.live == live
    assert system.supervisor.checks == 0


@pytest.mark.parametrize(
    "sha",
    [lambda dev: dev.commit({"automations.yaml": "- id: 'branch'\n"}, branch="feature"), lambda dev: "0" * 40],
    ids=["feature branch", "unknown commit"],
)
def test_only_commits_on_main_deploy(system, sha):
    sha = sha(system.dev)
    system.gitea.checks[sha] = "success"
    before = system.read("automations.yaml")

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.NOT_ON_MAIN
    assert system.read("automations.yaml") == before


@pytest.mark.parametrize("checks", [None, "pending", "failure"])
def test_a_commit_whose_checks_have_not_passed_does_not_deploy(system, checks):
    sha = system.dev.commit({"automations.yaml": "- id: 'unchecked'\n"})
    if checks:
        system.gitea.checks[sha] = checks
    before = system.read("automations.yaml")

    result = system.deploy.run(Request(sha, "manual"), "Actions")

    assert result.outcome is Outcome.CHECKS_NOT_PASSED
    assert system.read("automations.yaml") == before
    assert system.supervisor.checks == 0


def test_paused_auto_deploy_holds_merges_but_not_manual_deploys(system):
    sha = checked(system, {"automations.yaml": "- id: 'merged'\n"})

    held = system.deploy.run(Request(sha, "merge"), "Actions")
    assert held.outcome is Outcome.PAUSED
    assert system.remote_rev("live") != sha
    assert system.gitea.deploy_statuses == []

    manual = system.deploy.run(Request(sha, "manual"), "alex")
    assert manual.outcome is Outcome.DEPLOYED
    assert system.remote_rev("live") == sha
    assert system.gitea.deploy_statuses == [(sha, "success")]


def test_a_merge_deploys_by_itself_once_auto_deploy_is_on(system):
    system.state.set(Switch.AUTO_DEPLOY, True)
    sha = checked(system, {"automations.yaml": "- id: 'merged'\n"})

    assert system.deploy.run(Request(sha, "merge"), "Actions").outcome is Outcome.DEPLOYED
    assert system.remote_rev("live") == sha


def test_repository_only_paths_never_reach_config_and_follow_the_list(system):
    sha = checked(
        system,
        {
            "ci/new_check.py": "print('new')\n",
            "README.md": "# config, updated\n",
            ".gitops/repository-only": "# kept off Home Assistant\n.gitea/\nci/\nREADME.md\nesphome/archive/\n",
        },
    )

    assert system.deploy.run(Request(sha, "manual"), "Actions").outcome is Outcome.DEPLOYED

    assert not (system.root / "ci").exists()
    assert not (system.root / "README.md").exists()
    assert not (system.root / ".gitea").exists()
    assert not (system.root / "esphome" / "archive" / "old.yaml").exists()
    assert system.git.run("status", "--porcelain") == ""
    assert system.remote_show("live", "esphome/archive/old.yaml") == "esphome:\n  name: old\n"


def test_a_repository_only_entry_matches_a_file_or_a_folder_with_or_without_slashes(system):
    sha = checked(
        system,
        {
            ".gitops/repository-only": "docs\n/ci/\nREADME.md\n# comment\n",
            "docs/notes.md": "# Notes\n",
            "ci/x.py": "print('x')\n",
            "README.md": "# config, updated\n",
        },
    )

    assert system.deploy.run(Request(sha, "manual"), "Actions").outcome is Outcome.DEPLOYED

    assert [name for name in ("docs/notes.md", "ci/x.py", "README.md") if (system.root / name).exists()] == []


def test_every_deploy_is_in_the_history_with_who_asked(system):
    sha = checked(system, {"automations.yaml": "- id: 'x'\n"})

    system.deploy.run(Request(sha, "manual", run_url="https://gitea.example/run/9"), "alex")

    entry = system.state.history()[0]
    assert (entry.action, entry.by, entry.link, entry.result.outcome) == (
        "deploy",
        "alex",
        "https://gitea.example/run/9",
        Outcome.DEPLOYED,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"trigger": "manual"},
        {"sha": "ABC", "trigger": "manual"},
        {"sha": "a" * 40, "trigger": "push"},
        {"sha": "a" * 40, "trigger": "manual", "impact": "x" * 201},
        {"sha": "a" * 40, "trigger": "manual", "run_url": 5},
    ],
)
def test_malformed_deploy_requests_are_refused(body):
    with pytest.raises(ValueError):
        Request.parse(body)


def test_a_well_formed_deploy_request_is_read_as_sent():
    body = {"sha": "a" * 40, "trigger": "merge", "run_url": "https://gitea.example/run/1", "impact": "nothing"}
    assert Request.parse(body) == Request("a" * 40, "merge", "https://gitea.example/run/1", "nothing")
