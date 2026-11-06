"""An add-on version's release on Gitea: the tag <folder>-v<version> on the commit its image was built from, with the
version's CHANGELOG.md section as the notes."""

import json

from addons.addon import Addon
from addons.api import request


class ReleaseError(Exception):
    """Gitea did not create the release."""


def tag(addon: Addon) -> str:
    return f"{addon.folder.name}-v{addon.version}"


def create(addon: Addon, notes: str, server: str, repository: str, token: str, sha: str) -> None:
    name = tag(addon)
    release = {"tag_name": name, "target_commitish": sha, "name": f"{addon.folder.name} {addon.version}", "body": notes}
    answer = request(
        "POST",
        f"{server.rstrip('/')}/api/v1/repos/{repository}/releases",
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
        body=json.dumps(release).encode(),
    )
    if answer.status != 201:
        raise ReleaseError(
            f"Gitea refused the release {name}: HTTP {answer.status} {answer.said()}. The image is pushed: create the "
            f"release by hand, tag {name} on {sha}, notes from {addon.changelog.as_posix()}"
        )
