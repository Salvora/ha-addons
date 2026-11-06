import shutil
import subprocess
import time

from ha_gitops_sync.capture import Capture
from ha_gitops_sync.state import Outcome, Switch
from ha_gitops_sync.worktree import LARGE, changes


def retrying(system) -> Capture:
    """A capture whose failed pushes are tried again after 0.05 s, then every 0.1 s."""
    return Capture(system.git, system.gitea, system.supervisor, system.state, system.worker, delays=(0.05, 0.1))


def wait_for(condition) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, "it did not happen in time"
        time.sleep(0.02)


def test_with_main_where_live_was_an_edit_goes_to_main_and_live_together(system):
    system.state.set(Switch.CAPTURE, True)
    system.write("automations.yaml", "- id: 'edited in the UI'\n")

    result = system.capture.run()

    assert result.outcome is Outcome.OK
    assert result.files == ("automations.yaml",)
    assert result.message == "Edited in Home Assistant: automations.yaml; pushed to main and live."
    assert system.remote_rev("main") == system.remote_rev("live") == result.live
    assert system.remote_show("main", "automations.yaml") == "- id: 'edited in the UI'\n"
    identity = "Home Assistant <home-assistant@ha-gitops-sync.invalid>"
    assert system.remote_log("live", "%an <%ae>|%cn <%ce>") == f"{identity}|{identity}"
    assert system.remote_log("live", "%s") == "Edited in Home Assistant: automations.yaml"
    assert system.supervisor.events == [
        ("gitops_captured", {"commit": result.live, "files": ["automations.yaml"], "by": "Home Assistant"})
    ]
    assert not system.gitea.pulls


def test_with_main_ahead_an_edit_goes_to_live_and_the_live_to_main_pull_request(system):
    system.state.set(Switch.CAPTURE, True)
    main = system.dev.commit({"scripts.yaml": "from: main\n"})
    system.write("automations.yaml", "- id: 'edited in the UI'\n")

    result = system.capture.run()

    assert system.remote_rev("main") == main
    assert system.remote_rev("live") == result.live
    assert result.pull_request == system.gitea.pulls[0]["html_url"]


def test_the_message_names_every_file(system):
    system.state.set(Switch.CAPTURE, True)
    names = [f"automations/room_{n}.yaml" for n in range(4)]
    for name in names:
        system.write(name, "- id: 'x'\n")

    system.capture.run()

    assert system.remote_log("live", "%s") == "Edited in Home Assistant: 4 files"
    assert system.remote_log("live", "%b").splitlines() == names


def test_a_deleted_file_is_captured_as_a_deletion(system):
    system.state.set(Switch.CAPTURE, True)
    (system.root / "scripts.yaml").unlink()

    system.capture.run()

    assert "scripts.yaml" not in system.git.run("ls-tree", "--name-only", "origin/live")


def test_capture_paused_commits_nothing(system):
    head = system.head()
    system.write("automations.yaml", "- id: 'edited'\n")

    assert system.capture.run() is None
    assert system.head() == head


def test_a_resave_that_only_changes_line_endings_is_not_an_edit(system):
    system.state.set(Switch.CAPTURE, True)
    head = system.head()
    resaved = system.root / "automations.yaml"
    resaved.write_bytes(resaved.read_bytes().replace(b"\n", b"\r\n"))

    assert system.capture.run() is None
    assert system.head() == head


def test_a_tracked_file_grown_past_20_mib_is_captured(system):
    system.state.set(Switch.CAPTURE, True)
    with (system.root / "scripts.yaml").open("ab") as file:
        file.truncate(LARGE + 1)

    result = system.capture.run()

    assert result.files == ("scripts.yaml",)
    assert system.git.run("cat-file", "-s", "origin/live:scripts.yaml") == f"{LARGE + 1}\n"


def test_new_large_files_repository_only_paths_and_nested_repositories_are_left_out(system):
    system.state.set(Switch.CAPTURE, True)
    big = system.root / "www" / "camera.mp4"
    big.parent.mkdir()
    with big.open("wb") as file:
        file.truncate(LARGE + 1)
    system.write("ci/made_on_home_assistant.py", "print('no')\n")
    nested = system.root / "custom_components" / "thing"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(nested)], check=True)
    (nested / "__init__.py").write_text("")
    system.write("automations.yaml", "- id: 'edited'\n")

    result = system.capture.run()

    assert result.files == ("automations.yaml",)
    assert changes(system.git).skipped == ("ci/made_on_home_assistant.py", "custom_components/thing/", "www/camera.mp4")
    tree = system.git.run("ls-tree", "-r", "--name-only", "origin/live").splitlines()
    assert not {"www/camera.mp4", "ci/made_on_home_assistant.py"} & set(tree)
    assert not any(name.startswith("custom_components/") for name in tree)
    assert system.capture.run() is None


def test_switching_capture_on_captures_the_esphome_version(system):
    system.supervisor.esphome_version = "2026.10.2"
    system.state.set(Switch.CAPTURE, True)

    result = system.capture.run()

    assert result.files == (".gitops/esphome-version",)
    assert system.remote_show("main", ".gitops/esphome-version") == "2026.10.2\n"


def test_a_capture_made_while_gitea_is_unreachable_reaches_live_by_itself_once_it_answers(system):
    capture = retrying(system)
    system.state.set(Switch.CAPTURE, True)
    away = system.remote.with_name("away.git")
    system.remote.rename(away)
    system.write("automations.yaml", "- id: 'edited offline'\n")

    result = capture.run()

    assert result.message.endswith("; Gitea did not answer; it is tried again later.")
    assert system.git.run("show", "HEAD:automations.yaml") == "- id: 'edited offline'\n"
    away.rename(system.remote)
    wait_for(lambda: system.remote_rev("live") == result.live)
    assert system.remote_rev("main") == result.live


def test_a_pull_request_that_failed_to_open_after_the_push_is_opened_by_the_retry(system):
    capture = retrying(system)
    system.state.set(Switch.CAPTURE, True)
    system.dev.commit({"scripts.yaml": "from: main\n"})
    system.gitea.refused_pulls = 1
    system.write("automations.yaml", "- id: 'edited in the UI'\n")

    result = capture.run()

    assert system.remote_rev("live") == result.live
    assert result.pull_request == ""
    wait_for(lambda: system.gitea.pulls)
    assert len(system.gitea.pull_attempts) == 2


def test_each_failed_retry_waits_the_next_delay(system):
    capture = retrying(system)
    system.state.set(Switch.CAPTURE, True)
    system.dev.commit({"scripts.yaml": "from: main\n"})
    system.gitea.refused_pulls = 2
    system.write("automations.yaml", "- id: 'edited in the UI'\n")

    capture.run()

    wait_for(lambda: system.gitea.pulls)
    first, second, third = system.gitea.pull_attempts
    assert second - first >= 0.05
    assert third - second >= 0.1


def test_a_backup_restored_while_the_add_on_runs_is_committed_on_top_of_live_by_the_next_capture(system, tmp_path):
    backup = tmp_path / "backup"
    shutil.copytree(system.root, backup)
    restored = system.head()
    system.state.set(Switch.CAPTURE, True)
    system.write("scripts.yaml", "edited: after the backup\n")
    before = system.capture.run().live
    system.root.rename(tmp_path / "replaced")
    shutil.copytree(backup, system.root)
    system.write("automations.yaml", "- id: 'edited after the restore'\n")

    result = system.capture.run()

    assert restored[:7] in result.message
    assert result.live == system.remote_rev("live")
    live = system.remote_rev("live")
    assert system.remote_log(live, "%s") == f"Restored from backup: back to {restored[:7]}"
    assert system.remote_parents(live) == [before]
    assert system.remote_show(live, "scripts.yaml") == "{}\n"
    assert system.remote_show(live, "automations.yaml") == "- id: 'edited after the restore'\n"
    assert system.remote_rev("main") == live


def test_a_backup_restored_with_no_edit_after_it_is_committed_on_top_of_live_by_the_next_capture(system, tmp_path):
    backup = tmp_path / "backup"
    shutil.copytree(system.root, backup)
    restored = system.head()
    system.state.set(Switch.CAPTURE, True)
    system.write("scripts.yaml", "edited: after the backup\n")
    system.capture.run()
    system.root.rename(tmp_path / "replaced")
    shutil.copytree(backup, system.root)

    result = system.capture.run()

    assert restored[:7] in result.message
    assert result.live == system.remote_rev("live")
    entry = system.state.history()[0]
    assert entry.action == "restore"
    assert restored[:7] in entry.result.message
    assert entry.result.live == system.remote_rev("live")
    assert system.remote_show("live", "scripts.yaml") == "{}\n"
