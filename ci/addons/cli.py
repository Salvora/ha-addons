"""The add-on repository's checks and releases. Exit status: 0 all well, 1 a check found a problem, 2 the command
cannot run.

  check      every add-on against the repository's rules: config.yaml names a lower-case ghcr.io image without a tag
             and a MAJOR.MINOR.PATCH version; translations/en.yaml names and describes every option and port, and
             nothing else; an add-on this change touches raises its version above the one at BASE and has a
             CHANGELOG.md section for it. Add-ons are found as the store finds them: every config.yaml, at any
             depth, outside dot folders and rootfs/
  preflight  before image.yml builds an add-on: CHANGELOG.md has its version's section, <image>:<version> is not on
             ghcr.io yet, and GHCR_PUSH_USER / GHCR_PUSH_TOKEN may push it; writes kaniko's login to
             $GITHUB_WORKSPACE/.docker/config.json, and IMAGE and VERSION to GITHUB_ENV for the steps after it
  release    the Gitea release <folder>-v<version> on GITHUB_SHA, with the version's CHANGELOG.md section as notes

Run from the repository root. Environment: BASE (check, e.g. origin/main); GHCR_PUSH_USER, GHCR_PUSH_TOKEN,
GITHUB_WORKSPACE, GITHUB_ENV (preflight); TOKEN, GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_SHA (release).
"""

import argparse
import os
import traceback
from pathlib import Path

from addons import release
from addons.addon import Addon, AddonError
from addons.api import Unreachable
from addons.registry import RegistryError, Repository, write_login
from addons.rules import GitError, check


class CannotRun(Exception):
    """What the command needs is missing."""


def env(name: str) -> str:
    if value := os.environ.get(name):
        return value
    raise CannotRun(f"{name} is not set")


def load(folder: str) -> Addon:
    config = Path(folder, "config.yaml")
    if not config.is_file():
        raise CannotRun(f"{config.as_posix()} does not exist: name an add-on's folder, e.g. ha-gitops-sync")
    return Addon.load(config)


def error(message: str) -> None:
    print(f"::error::{message}", flush=True)


def cmd_check(args: argparse.Namespace) -> int:
    base = args.base or env("BASE")
    problems = check(base)
    for problem in problems:
        error(problem)
    if not problems:
        print(f"every add-on follows the rules, compared with {base}")
    return 1 if problems else 0


def cmd_preflight(args: argparse.Namespace) -> int:
    addon = load(args.folder)
    addon.release_notes()
    user, password = env("GHCR_PUSH_USER"), env("GHCR_PUSH_TOKEN")
    login, outputs = Path(env("GITHUB_WORKSPACE"), ".docker", "config.json"), Path(env("GITHUB_ENV"))
    host, _, name = addon.image.partition("/")
    reference = f"{addon.image}:{addon.version}"
    repository = Repository.login(host, name, user, password)
    if repository is None:
        error(f"{host} refused GHCR_PUSH_USER's login with GHCR_PUSH_TOKEN, a GitHub classic token")
        return 1
    if repository.has_tag(str(addon.version)):
        error(
            f"{reference} exists already, and a version is built once: raise the version in "
            f"{addon.config.as_posix()} and add its section to {addon.changelog.as_posix()}"
        )
        return 1
    if not repository.can_push():
        error(
            f"GHCR_PUSH_TOKEN may not push to {addon.image}: it must be a GitHub classic token with write:packages, "
            "of an account that may write that package"
        )
        return 1
    write_login(login, host, user, password)
    with outputs.open("a", encoding="utf-8") as out:
        out.write(f"IMAGE={addon.image}\nVERSION={addon.version}\n")
    print(f"{reference} is new, and GHCR_PUSH_USER may push it")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    addon = load(args.folder)
    notes = addon.release_notes()
    server, repository, sha, token = map(env, ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_SHA", "TOKEN"))
    release.create(addon, notes, server, repository, token, sha)
    print(f"released {release.tag(addon)}")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="python3 -m addons", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = top.add_subparsers(dest="command", required=True)
    checked = sub.add_parser("check", help="every add-on against the repository's rules")
    checked.add_argument("--base", help="the commit or branch the change is compared with (env BASE)")
    checked.set_defaults(run=cmd_check)
    for name, run, summary in (
        ("preflight", cmd_preflight, "what image.yml checks and prepares before it builds an add-on"),
        ("release", cmd_release, "the Gitea release of an add-on's version, once its image is pushed"),
    ):
        command = sub.add_parser(name, help=summary)
        command.add_argument("folder", help="the add-on's folder, e.g. ha-gitops-sync")
        command.set_defaults(run=run)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except AddonError as exc:
        for problem in exc.problems:
            error(problem)
        return 1
    except release.ReleaseError as exc:
        error(str(exc))
        return 1
    except (GitError, RegistryError, Unreachable, CannotRun) as exc:
        error(str(exc))
        return 2
    except Exception:
        traceback.print_exc()
        error("unexpected error, see above")
        return 2
