import shutil
import subprocess

import pytest

from ha_gitops_sync.reconcile import StartError
from ha_gitops_sync.state import Outcome, Switch
from ha_gitops_sync.worktree import changes

CONTROL_FILES = (".gitattributes", ".gitignore", ".gitops/repository-only")


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_the_first_start_attaches_to_main_and_adds_only_the_control_files_config_lacks(fresh):
    fresh.write("automations.yaml", "- id: 'edited after the upload'\n")
    before = snapshot(fresh.root)

    fresh.reconcile.attach()

    after = snapshot(fresh.root)
    assert {name: after[name] for name in before} == before
    assert sorted(set(after) - set(before)) == list(CONTROL_FILES)
    assert {name: fresh.read(name) for name in CONTROL_FILES} == {
        name: fresh.remote_show("main", name) for name in CONTROL_FILES
    }
    assert fresh.head() == fresh.remote_rev("main")
    assert fresh.git.run("symbolic-ref", "HEAD") == "refs/heads/live\n"
    assert changes(fresh.git).files == ("automations.yaml",)
    entry = fresh.state.history()[0]
    assert (entry.action, entry.result.outcome) == ("attach", Outcome.OK)
    assert "added .gitattributes, .gitignore, .gitops/repository-only" in entry.result.message


def test_the_first_capture_after_the_first_start_takes_in_no_ignored_path_and_deletes_nothing(fresh):
    for name in (".storage/auth", "deps/x.py", "home-assistant.log"):
        fresh.write(name, "kept out\n")
    fresh.reconcile.attach()
    uploaded = fresh.remote_rev("main")
    fresh.state.set(Switch.CAPTURE, True)
    fresh.write("automations.yaml", "- id: 'edited'\n")

    fresh.capture.run()

    tree = fresh.remote_files("main")
    assert "automations.yaml" in tree
    assert [name for name in tree if name.startswith((".storage/", "deps/")) or name.endswith(".log")] == []
    deleted = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=D", uploaded, "main"],
        cwd=fresh.remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert deleted == ""


def test_edits_made_before_the_first_start_arrive_as_an_ordinary_capture(fresh):
    fresh.supervisor.esphome_version = "2026.10.2"
    fresh.write("automations.yaml", "- id: 'edited after the upload'\n")
    fresh.reconcile.attach()
    fresh.state.set(Switch.CAPTURE, True)

    fresh.reconcile.run()

    assert fresh.remote_rev("live") == fresh.remote_rev("main")
    assert fresh.remote_show("main", "automations.yaml") == "- id: 'edited after the upload'\n"
    assert fresh.remote_show("main", ".gitops/esphome-version") == "2026.10.2\n"
    assert fresh.remote_log("main", "%an") == "Home Assistant"


def test_the_first_start_leaves_the_repository_only_paths_off_home_assistant(fresh):
    fresh.reconcile.attach()
    fresh.reconcile.run()

    assert not (fresh.root / "ci").exists()
    assert not (fresh.root / ".gitea").exists()
    assert not (fresh.root / "README.md").exists()
    assert fresh.git.run("status", "--porcelain") == ""
    assert fresh.remote_rev("live") == fresh.remote_rev("main")


def test_a_reinstall_attaches_to_live_not_main(system, tmp_path):
    live = system.remote_rev("live")
    system.dev.commit({"automations.yaml": "- id: 'not deployed'\n"})
    before = snapshot(system.root)
    (system.root / ".git").rename(tmp_path / "removed.git")

    system.reconcile.attach()

    assert system.head() == live
    assert snapshot(system.root) == before
    assert system.git.run("status", "--porcelain") == ""


def test_a_nested_repository_stops_the_start(fresh):
    subprocess.run(["git", "init", "--quiet", str(fresh.root / "esphome")], check=True)

    with pytest.raises(StartError, match="esphome"):
        fresh.reconcile.attach()


def test_a_nested_repository_in_a_folder_gitignore_leaves_out_does_not_stop_the_first_start(fresh):
    subprocess.run(["git", "init", "--quiet", str(fresh.root / ".storage" / "clone")], check=True)

    fresh.reconcile.attach()

    assert fresh.head() == fresh.remote_rev("main")


def test_a_repository_another_setup_made_stops_the_start(fresh):
    subprocess.run(["git", "init", "--quiet", "--initial-branch=master", str(fresh.root)], check=True)

    with pytest.raises(StartError, match="refs/heads/master"):
        fresh.reconcile.attach()


def test_a_restored_backup_is_committed_on_top_of_live_and_main_follows(system, tmp_path):
    backup = tmp_path / "backup"
    shutil.copytree(system.root, backup)
    restored = system.head()
    system.state.set(Switch.CAPTURE, True)
    system.write("automations.yaml", "- id: 'after the backup'\n")
    captured = system.capture.run()
    system.state.set(Switch.CAPTURE, False)
    system.root.rename(tmp_path / "replaced")
    shutil.copytree(backup, system.root)

    system.reconcile.run()

    live = system.remote_rev("live")
    assert system.remote_parents(live) == [captured.live]
    assert system.remote_log(live, "%s") == f"Restored from backup: back to {restored[:7]}"
    assert system.remote_show(live, "automations.yaml") == (backup / "automations.yaml").read_text()
    assert system.read("automations.yaml") == (backup / "automations.yaml").read_text()
    assert system.remote_rev("main") == live
    entry = system.state.history()[0]
    assert (entry.action, entry.result.outcome) == ("restore", Outcome.OK)


def test_drift_since_the_last_run_is_captured_and_pushed(system):
    system.state.set(Switch.CAPTURE, True)
    system.write("scenes.yaml", "- name: Evening\n")

    system.reconcile.run()

    assert system.remote_show("live", "scenes.yaml") == "- name: Evening\n"
    assert system.git.run("status", "--porcelain") == ""
