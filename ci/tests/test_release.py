import pytest

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def workflow(monkeypatch):
    """What image.yml gives the release step."""
    for name, value in {
        "GITHUB_SERVER_URL": "https://git.example",
        "GITHUB_REPOSITORY": "owner/ha-addons",
        "GITHUB_SHA": SHA,
        "TOKEN": "job-token",
    }.items():
        monkeypatch.setenv(name, value)


def test_the_release_is_the_version_on_the_built_commit_with_its_changelog_section(example, gitea, workflow, run):
    assert run("release", "example") == (0, [])

    assert gitea.releases == [
        {"tag_name": "example-v1.0.0", "target_commitish": SHA, "name": "example 1.0.0", "body": "- First release."}
    ]


def test_the_notes_are_only_the_versions_own_section(example, gitea, workflow, run):
    example.edit("example/config.yaml", 'version: "1.0.0"', 'version: "1.1.0"')
    example.write(
        "example/CHANGELOG.md",
        "# Changelog\n\n## [1.1.0] - 2026-11-06\n\n- Added a panel.\n- Fixed the API.\n\n"
        "## 1.0.0\n\n- First release.\n",
    )

    assert run("release", "example")[0] == 0

    assert gitea.releases[0]["body"] == "- Added a panel.\n- Fixed the API."


def test_a_release_gitea_refuses_fails_the_step(example, gitea, workflow, run):
    gitea.tags.add("example-v1.0.0")

    status, errors = run("release", "example")

    assert status == 1
    assert "example-v1.0.0" in errors[0]


def test_a_folder_without_config_yaml_cannot_be_released(example, gitea, workflow, run):
    status, errors = run("release", "missing")

    assert status == 2
    assert errors[0].startswith("missing/config.yaml does not exist")
    assert gitea.releases == []


def test_a_version_without_its_changelog_section_is_not_released(example, gitea, workflow, run):
    example.write("example/CHANGELOG.md", "# Changelog\n\n## 0.9.0\n\n- Before.\n")

    assert run("release", "example")[0] == 1
    assert gitea.releases == []
