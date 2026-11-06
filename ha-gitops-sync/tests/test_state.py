from ha_gitops_sync.state import Outcome, Result, State, Switch


def test_a_fresh_install_starts_with_both_switches_paused(tmp_path):
    state = State(tmp_path / "state.json")

    assert not state.is_on(Switch.AUTO_DEPLOY)
    assert not state.is_on(Switch.CAPTURE)


def test_switches_and_results_survive_a_restart(tmp_path):
    state = State(tmp_path / "state.json")
    state.set(Switch.CAPTURE, True)
    deployed = Result(Outcome.DEPLOYED, "Deployed.", live="a" * 40, files=("automations.yaml",))
    state.record("deploy", deployed, "alex", "https://gitea.example/run/3")

    again = State(tmp_path / "state.json")

    assert again.is_on(Switch.CAPTURE)
    assert not again.is_on(Switch.AUTO_DEPLOY)
    entry = again.history()[0]
    assert (entry.action, entry.by, entry.link) == ("deploy", "alex", "https://gitea.example/run/3")
    assert entry.result == deployed


def test_the_history_keeps_the_last_50_newest_first(tmp_path):
    state = State(tmp_path / "state.json")
    for n in range(55):
        state.record("check", Result(Outcome.CHECKED, f"check {n}"), "alex")

    history = State(tmp_path / "state.json").history()

    assert [entry.result.message for entry in history] == [f"check {n}" for n in range(54, 4, -1)]


def test_an_answer_carries_only_the_fields_that_apply():
    assert Result(Outcome.CHECK_FAILED, "The check failed.", output="line 3").answer() == {
        "outcome": "check-failed",
        "message": "The check failed.",
        "output": "line 3",
    }
    assert Result(Outcome.RESTARTED, "Back.", seconds=0).answer() == {
        "outcome": "restarted",
        "message": "Back.",
        "seconds": 0,
    }
