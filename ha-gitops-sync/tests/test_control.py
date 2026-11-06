import pytest

from ha_gitops_sync.state import Outcome


def test_a_check_answers_with_the_result(system):
    assert system.control.check("alex").outcome is Outcome.CHECKED

    system.supervisor.failing_check = "Invalid config for [light]: required key not provided"
    result = system.control.check("alex")

    assert result.outcome is Outcome.CHECK_FAILED
    assert result.output == "Invalid config for [light]: required key not provided"


@pytest.mark.parametrize(
    ("domain", "service"),
    [("homeassistant", "reload_all"), ("frontend", "reload_themes"), ("automation", "reload")],
)
def test_a_reload_calls_its_service_once_the_check_passed(system, domain, service):
    result = system.control.reload(domain, service, "alex")

    assert result.outcome is Outcome.RELOADED
    assert system.supervisor.checks == 1
    assert system.supervisor.called == [f"{domain}.{service}"]


def test_a_reload_home_assistant_does_not_offer_is_a_bad_request(system):
    result = system.control.reload("template", "reload", "Actions")

    assert result.outcome is Outcome.BAD_REQUEST
    assert system.supervisor.called == []


def test_a_failing_check_stops_reload_and_restart(system):
    system.supervisor.failing_check = "Invalid config"

    assert system.control.reload("automation", "reload", "alex").outcome is Outcome.CHECK_FAILED
    assert system.control.restart("alex").outcome is Outcome.CHECK_FAILED
    assert system.supervisor.called == []
    assert system.supervisor.restarts == 0


def test_a_restart_follows_a_passing_check_and_says_how_long_it_took(system):
    system.supervisor.restart_takes = 1.1

    result = system.control.restart("alex")

    assert result.outcome is Outcome.RESTARTED
    assert system.supervisor.restarts == 1
    assert result.seconds == 1
    assert "back after 1 s" in result.message


def test_home_assistant_not_answering_is_unavailable_and_nothing_happens(system):
    system.supervisor.down = True

    for result in (
        system.control.check("alex"),
        system.control.reload("homeassistant", "reload_all", "alex"),
        system.control.restart("alex"),
    ):
        assert result.outcome is Outcome.UNAVAILABLE
    assert system.supervisor.restarts == 0
    assert system.supervisor.called == []


def test_every_action_is_in_the_history_with_who_asked(system):
    system.control.check("alex")
    system.control.restart("Actions")

    assert [(e.action, e.by, e.result.outcome) for e in system.state.history()[:2]] == [
        ("restart", "Actions", Outcome.RESTARTED),
        ("check", "alex", Outcome.CHECKED),
    ]
