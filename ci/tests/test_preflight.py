import json

import pytest


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    """What image.yml gives the preflight step: the push login and the runner's workspace and GITHUB_ENV."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "github_env").write_text("")
    for name, value in {
        "GHCR_PUSH_USER": "pusher",
        "GHCR_PUSH_TOKEN": "ghp_push",
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_ENV": str(tmp_path / "github_env"),
    }.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def kaniko_login(workflow):
    path = workflow / "workspace" / ".docker" / "config.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_a_new_version_gets_kanikos_login_and_its_image_and_version(example, ghcr, workflow, run):
    assert run("preflight", "example") == (0, [])

    assert kaniko_login(workflow) == {"auths": {"ghcr.io": {"username": "pusher", "password": "ghp_push"}}}
    assert (workflow / "github_env").read_text().splitlines() == ["IMAGE=ghcr.io/salvora/example", "VERSION=1.0.0"]
    assert "POST /v2/salvora/example/blobs/uploads/" in ghcr.calls
    assert ghcr.uploads == set()


def test_a_version_on_the_registry_already_is_not_built_again(example, ghcr, workflow, run):
    ghcr.tags.add("salvora/example:1.0.0")

    status, errors = run("preflight", "example")

    assert status == 1
    assert "ghcr.io/salvora/example:1.0.0" in errors[0]
    assert kaniko_login(workflow) is None
    assert (workflow / "github_env").read_text() == ""


def test_an_older_version_on_the_registry_does_not_stop_a_new_one(example, ghcr, workflow, run):
    ghcr.tags.add("salvora/example:0.9.0")

    assert run("preflight", "example")[0] == 0


def test_a_refused_login_stops_the_build(example, ghcr, workflow, run, monkeypatch):
    monkeypatch.setenv("GHCR_PUSH_TOKEN", "ghp_revoked")

    assert run("preflight", "example")[0] == 1
    assert kaniko_login(workflow) is None


def test_a_login_that_may_not_push_stops_the_build(example, ghcr, workflow, run):
    ghcr.may_push = False

    assert run("preflight", "example")[0] == 1
    assert kaniko_login(workflow) is None


def test_a_version_without_its_changelog_section_is_not_built(example, ghcr, workflow, run):
    example.write("example/CHANGELOG.md", "# Changelog\n")

    status, errors = run("preflight", "example")

    assert status == 1
    assert errors[0].startswith("example/CHANGELOG.md: ")
    assert ghcr.calls == []


def test_a_folder_without_config_yaml_cannot_be_built(example, ghcr, workflow, run):
    status, errors = run("preflight", "missing")

    assert status == 2
    assert errors[0].startswith("missing/config.yaml does not exist")
    assert ghcr.calls == []


def test_an_unexpected_failure_is_the_command_failing_not_a_check(example, ghcr, workflow, run, monkeypatch):
    monkeypatch.setenv("GITHUB_ENV", str(workflow))

    assert run("preflight", "example") == (2, ["unexpected error, see above"])


def test_without_the_push_login_the_preflight_cannot_run(example, ghcr, workflow, run, monkeypatch):
    monkeypatch.delenv("GHCR_PUSH_TOKEN")

    assert run("preflight", "example")[0] == 2
    assert ghcr.calls == []


def test_an_unreachable_registry_stops_the_preflight(example, network, workflow, run):
    assert run("preflight", "example")[0] == 2
