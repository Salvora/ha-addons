import pytest

SECTION_101 = "# Changelog\n\n## 1.0.1\n\n- Fixed the API.\n\n## 1.0.0\n\n- First release.\n"


def at_fault(errors: list[str]) -> list[str]:
    """The file each error names first."""
    return [error.split(": ", 1)[0] for error in errors]


def raise_to(repo, version: str, changelog: str | None = SECTION_101) -> None:
    repo.edit("example/config.yaml", 'version: "1.0.0"', f'version: "{version}"')
    if changelog is not None:
        repo.write("example/CHANGELOG.md", changelog)


def test_an_unchanged_valid_addon_passes(repo, run):
    assert run("check", "--base", "main") == (0, [])


def test_a_change_outside_the_addons_needs_no_new_version(repo, run):
    repo.write("README.md", "# Add-ons\n")
    repo.commit()

    assert run("check", "--base", "main") == (0, [])


def test_touching_an_addon_without_raising_its_version_fails(repo, run):
    repo.write("example/run.sh", "#!/bin/sh\nexec python3 -m example --verbose\n")
    repo.commit()

    status, errors = run("check", "--base", "main")

    assert status == 1
    assert at_fault(errors) == ["example/config.yaml"]
    assert "1.0.0" in errors[0]


def test_a_raised_version_with_its_changelog_section_passes(repo, run):
    raise_to(repo, "1.0.1")
    repo.commit()

    assert run("check", "--base", "main") == (0, [])


@pytest.mark.parametrize("heading", ["## [1.0.1] - 2026-11-06", "## 1.0.1 (2026-11-06)"])
def test_a_changelog_heading_may_bracket_the_version_and_carry_a_date(repo, run, heading):
    raise_to(repo, "1.0.1", SECTION_101.replace("## 1.0.1", heading))
    repo.commit()

    assert run("check", "--base", "main") == (0, [])


@pytest.mark.parametrize(
    "changelog",
    [
        "# Changelog\n\n## 1.0.0\n\n- First release.\n",
        "# Changelog\n\n## 1.0.1\n\n## 1.0.0\n\n- First release.\n",
        "# Changelog\n\n### 1.0.1\n\n- Fixed the API.\n",
    ],
    ids=["no section", "empty section", "not a second-level heading"],
)
def test_a_raised_version_needs_a_changelog_section_with_text(repo, run, changelog):
    raise_to(repo, "1.0.1", changelog)
    repo.commit()

    status, errors = run("check", "--base", "main")

    assert status == 1
    assert at_fault(errors) == ["example/CHANGELOG.md"]


def test_a_lower_version_is_not_a_raise(repo, run):
    raise_to(repo, "0.9.9", "# Changelog\n\n## 0.9.9\n\n- Went back.\n")
    repo.commit()

    status, errors = run("check", "--base", "main")

    assert status == 1
    assert at_fault(errors) == ["example/config.yaml"]


def test_the_version_must_be_above_the_one_the_base_branch_has_now(repo, run):
    raise_to(repo, "1.0.1")
    repo.commit()
    repo.git("checkout", "-q", "main")
    raise_to(repo, "1.0.1", SECTION_101.replace("Fixed the API", "Something else"))
    repo.commit("another change, merged first")
    repo.git("checkout", "-q", "change")

    status, errors = run("check", "--base", "main")

    assert status == 1
    assert at_fault(errors) == ["example/config.yaml"]


def test_an_addon_changed_only_on_the_base_branch_is_not_this_changes_release(repo, run):
    repo.write("README.md", "# Add-ons\n")
    repo.commit()
    repo.git("checkout", "-q", "main")
    raise_to(repo, "1.0.1")
    repo.commit("another change, merged first")
    repo.git("checkout", "-q", "change")

    assert run("check", "--base", "main") == (0, [])


def test_a_new_addon_counts_as_raised_but_needs_its_changelog_section(repo, run):
    for path in ("config.yaml", "translations/en.yaml", "CHANGELOG.md"):
        repo.write(f"other/{path}", (repo.root / "example" / path).read_text(encoding="utf-8"))
    repo.edit("other/config.yaml", "salvora/example", "salvora/other")
    repo.commit()
    assert run("check", "--base", "main") == (0, [])

    repo.write("other/CHANGELOG.md", "# Changelog\n")
    repo.commit()
    status, errors = run("check", "--base", "main")
    assert status == 1
    assert at_fault(errors) == ["other/CHANGELOG.md"]


@pytest.mark.parametrize(
    ("line", "wrong"),
    [
        ('version: "1.0.0"', "version: 1.0"),
        ('version: "1.0.0"', 'version: "1.0"'),
        ('version: "1.0.0"', 'version: "v1.0.0"'),
        ('version: "1.0.0"', 'version: "1.00.0"'),
        ("image: ghcr.io/salvora/example", "image: ghcr.io/Salvora/example"),
        ("image: ghcr.io/salvora/example", "image: ghcr.io/salvora/example:1.0.0"),
        ("image: ghcr.io/salvora/example", "image: ghcr.io/salvora/{arch}-example"),
        ("image: ghcr.io/salvora/example", "image: ghcr.io/example"),
        ("image: ghcr.io/salvora/example", "image: docker.io/salvora/example"),
        ("image: ghcr.io/salvora/example\n", ""),
    ],
)
def test_config_yaml_names_a_ghcr_image_without_tag_and_a_three_part_version(repo, run, line, wrong):
    repo.edit("example/config.yaml", line, wrong)

    status, errors = run("check", "--base", "HEAD")

    assert status == 1
    assert at_fault(errors) == ["example/config.yaml"]


@pytest.mark.parametrize(
    ("entry", "replacement"),
    [
        ("  token:\n    name: Token\n    description: How to sign in.\n", ""),
        ("    description: How to sign in.\n", ""),
        ("    name: Token\n", ""),
        ("  token:\n", "  tokens:\n"),
        ("  8765/tcp: The API.\n", ""),
        ("  8765/tcp: The API.\n", "  8765/tcp: The API.\n  8099/tcp: The panel.\n"),
    ],
    ids=["option missing", "no description", "no name", "not an option", "port missing", "not a port"],
)
def test_translations_describe_every_option_and_port_and_nothing_else(repo, run, entry, replacement):
    repo.edit("example/translations/en.yaml", entry, replacement)

    status, errors = run("check", "--base", "HEAD")

    assert status == 1
    assert set(at_fault(errors)) == {"example/translations/en.yaml"}


def test_an_addon_without_translations_has_every_option_and_port_undescribed(repo, run):
    (repo.root / "example/translations/en.yaml").unlink()

    status, errors = run("check", "--base", "HEAD")

    assert status == 1
    assert len(errors) == 7  # name and description of three options, and the port
    assert set(at_fault(errors)) == {"example/translations/en.yaml"}


def test_a_config_file_anywhere_the_store_looks_is_checked_as_an_addon(repo, run):
    repo.write("example/tests/fixtures/config.yaml", "name: not an add-on\n")

    status, errors = run("check", "--base", "HEAD")

    assert status == 1
    assert set(at_fault(errors)) == {"example/tests/fixtures/config.yaml"}


@pytest.mark.parametrize("config", ["example/rootfs/etc/config.yaml", ".github/config.yaml"])
def test_config_files_where_the_store_does_not_look_are_not_addons(repo, run, config):
    repo.write(config, "name: not an add-on\n")

    assert run("check", "--base", "HEAD") == (0, [])


def test_without_a_base_the_check_cannot_run(repo, run, monkeypatch):
    monkeypatch.delenv("BASE", raising=False)

    assert run("check")[0] == 2


def test_a_base_the_checkout_does_not_have_stops_the_check(repo, run):
    assert run("check", "--base", "origin/main")[0] == 2


def test_the_base_can_come_from_the_environment(repo, run, monkeypatch):
    repo.write("example/run.sh", "#!/bin/sh\n")
    repo.commit()
    monkeypatch.setenv("BASE", "main")

    assert run("check")[0] == 1
