"""The repository's rules for its add-on folders, each problem as one sentence that starts with the file at fault.
Paths are relative to the repository root, the working directory."""

import subprocess
from pathlib import Path

import yaml

from addons.addon import Addon, AddonError, Version, read_yaml, store_configs


class GitError(Exception):
    """A git command failed; the message holds git's own."""


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise GitError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def translation_problems(addon: Addon) -> list[str]:
    """translations/en.yaml names and describes every option and port, and nothing else."""
    path = addon.translations.as_posix()
    translations = (read_yaml(addon.translations) if addon.translations.is_file() else None) or {}
    options = translations.get("configuration") or {}
    ports = translations.get("network") or {}
    problems = [
        f"{path}: option {name} has no {field}"
        for name in sorted(addon.options)
        for field in ("name", "description")
        if not (isinstance(options.get(name), dict) and options[name].get(field))
    ]
    problems += [f"{path}: port {name} has no description" for name in sorted(addon.ports) if not ports.get(name)]
    problems += [f"{path}: {name} is not an option in config.yaml" for name in sorted(options.keys() - addon.options)]
    problems += [f"{path}: {name} is not a port in config.yaml" for name in sorted(ports.keys() - addon.ports)]
    return problems


def release_problems(addon: Addon, before: Version | None, base: str) -> list[str]:
    """A changed add-on is a new release: its version is above the one at base (None: the add-on is new), and
    CHANGELOG.md has a section for it."""
    problems = []
    if before is not None and addon.version <= before:
        problems.append(
            f"{addon.config.as_posix()}: version {addon.version} must be raised above {before}, the version on "
            f"{base}, because this change touches {addon.folder.as_posix()}/"
        )
    try:
        addon.release_notes()
    except AddonError as exc:
        problems += exc.problems
    return problems


def version_at(base: str, config: Path) -> Version | None:
    """The add-on's version at base, None when its config file is not there."""
    if not git("ls-tree", "--name-only", base, "--", config.as_posix()).strip():
        return None
    spec = f"{base}:{config.as_posix()}"
    try:
        return Version.parse((yaml.safe_load(git("show", spec)) or {}).get("version"))
    except ValueError as exc:
        raise AddonError([f"{spec}: {exc}"]) from exc


def check(base: str) -> list[str]:
    """Every problem with the add-ons; one that this change touches since it left base is held to the release rule
    as well."""
    changed = git("diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD").split("\0")
    problems = []
    for config in store_configs():
        try:
            addon = Addon.load(config)
            problems += translation_problems(addon)
            if any(path.startswith(f"{addon.folder.as_posix()}/") for path in changed):
                problems += release_problems(addon, version_at(base, config), base)
        except AddonError as exc:
            problems += exc.problems
    return problems
