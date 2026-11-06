"""The add-on repository's checks and releases. Exit status: 0 all well, 1 a check found a problem, 2 the command
cannot run.

  check      every add-on against the repository's rules: config.yaml names a lower-case ghcr.io image without a tag
             and a MAJOR.MINOR.PATCH version; translations/en.yaml names and describes every option and port, and
             nothing else; an add-on this change touches raises its version above the one at BASE and has a
             CHANGELOG.md section for it. Add-ons are found as the store finds them: every config.yaml, at any
             depth, outside dot folders and rootfs/
  preflight  before image.yml builds an add-on: CHANGELOG.md has its version's section, and <image>:<version> is
             on neither registry yet: not on ghcr.io, where GHCR_PUSH_USER / GHCR_PUSH_TOKEN must be able to push
             it, and not on this Gitea's registry as <owner>/<image's last part>, where IMAGE_PUSH_USER /
             IMAGE_PUSH_TOKEN must be able to push it; writes kaniko's login for both to
             $GITHUB_WORKSPACE/.docker/config.json, and IMAGE, GITEA_IMAGE and VERSION to GITHUB_ENV
  release    the Gitea release <folder>-v<version> on GITHUB_SHA, with the version's CHANGELOG.md section as notes

Run from the repository root. Environment: BASE (check, e.g. origin/main); GHCR_PUSH_USER, GHCR_PUSH_TOKEN,
IMAGE_PUSH_USER, IMAGE_PUSH_TOKEN, GITHUB_WORKSPACE, GITHUB_ENV (preflight); TOKEN (release); GITHUB_SERVER_URL,
GITHUB_REPOSITORY (preflight, release); GITHUB_SHA (release).
"""

import argparse
import os
import traceback
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Destination:
    """A registry image.yml pushes to, the login it pushes with (environment names), and what that token must be."""

    image: str
    user: str
    token: str
    needs: str


def destinations(addon: Addon) -> tuple[Destination, ...]:
    server = env("GITHUB_SERVER_URL").removeprefix("https://").rstrip("/")
    owner = env("GITHUB_REPOSITORY").partition("/")[0].lower()
    return (
        Destination(
            addon.image,
            "GHCR_PUSH_USER",
            "GHCR_PUSH_TOKEN",
            "a GitHub classic token with write:packages, of an account that may write that package",
        ),
        Destination(
            f"{server}/{owner}/{addon.image.rpartition('/')[2]}",
            "IMAGE_PUSH_USER",
            "IMAGE_PUSH_TOKEN",
            "a Gitea token with write:package, of an account that may write the organisation's packages",
        ),
    )


def cmd_preflight(args: argparse.Namespace) -> int:
    addon = load(args.folder)
    addon.release_notes()
    login, outputs = Path(env("GITHUB_WORKSPACE"), ".docker", "config.json"), Path(env("GITHUB_ENV"))
    targets = [(target, env(target.user), env(target.token)) for target in destinations(addon)]
    logins = {}
    for target, user, password in targets:
        host, _, name = target.image.partition("/")
        repository = Repository.login(host, name, user, password)
        if repository is None:
            error(f"{host} refused {target.user}'s login with {target.token}, {target.needs}")
            return 1
        if repository.has_tag(str(addon.version)):
            error(
                f"{target.image}:{addon.version} exists already, and a version is built once: raise the version in "
                f"{addon.config.as_posix()} and add its section to {addon.changelog.as_posix()}"
            )
            return 1
        if not repository.can_push():
            error(f"{target.token} may not push to {target.image}: it must be {target.needs}")
            return 1
        logins[host] = (user, password)
    write_login(login, logins)
    ghcr, gitea = (target.image for target, _, _ in targets)
    with outputs.open("a", encoding="utf-8") as out:
        out.write(f"IMAGE={ghcr}\nGITEA_IMAGE={gitea}\nVERSION={addon.version}\n")
    print(f"version {addon.version} is new on {ghcr} and {gitea}, and both logins may push it")
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
