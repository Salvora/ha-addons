import sys
import threading
import time

import pytest

if sys.platform != "linux":
    pytest.skip("inotify is Linux only; these run in CI", allow_module_level=True)

from ha_gitops_sync.watch import Watcher  # noqa: E402

QUIET = 0.3


@pytest.fixture
def watching(system):
    quiet = threading.Event()
    calls = []

    def on_quiet():
        calls.append(time.monotonic())
        quiet.set()

    watcher = Watcher(system.git, on_quiet, quiet=QUIET)
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    time.sleep(0.5)
    yield system, quiet, calls
    watcher.stop()
    thread.join(5)


def test_a_burst_of_edits_to_tracked_files_queues_one_capture_after_the_quiet_time(watching):
    system, quiet, calls = watching

    for n in range(5):
        system.write("automations.yaml", f"- id: '{n}'\n")
        time.sleep(0.05)

    assert quiet.wait(5)
    time.sleep(QUIET * 3)
    assert len(calls) == 1


def test_a_file_saved_by_renaming_a_draft_into_place_counts(watching):
    system, quiet, _ = watching
    draft = system.root.parent / "scripts.yaml.draft"
    draft.write_text("{}\n")

    draft.replace(system.root / "scripts.yaml")

    assert quiet.wait(5)


def test_what_gitignore_leaves_out_queues_nothing(watching):
    system, quiet, _ = watching

    system.write("home-assistant.log", "more lines\n")
    system.write(".storage/core.config", "{}\n")
    system.write(".storage/new_file", "{}\n")

    assert not quiet.wait(QUIET * 4)


def test_a_new_folder_is_watched_too(watching):
    system, quiet, _ = watching
    (system.root / "packages").mkdir()
    assert quiet.wait(5)
    quiet.clear()
    time.sleep(QUIET * 2)
    quiet.clear()

    system.write("packages/heating.yaml", "climate: []\n")

    assert quiet.wait(5)
