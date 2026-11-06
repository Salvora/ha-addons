"""The example add-on the tests start from, a git repository holding it, and fakes of ghcr.io and Gitea that answer
ci/addons' requests the way those services do. The add-on is written by the tests rather than kept as files: the
store would list a config.yaml anywhere in this repository as an add-on."""

import base64
import io
import json
import re
import subprocess
import uuid
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.response import addinfourl

import pytest

from addons import api, cli

EXAMPLE = {
    "config.yaml": """\
name: Example
version: "1.0.0"
slug: example
description: An add-on for the tests
arch: [amd64]
image: ghcr.io/salvora/example
ports:
  8765/tcp: 8765
options:
  url: ""
  log_level: info
schema:
  url: url
  token: password?
  log_level: list(debug|info|warning|error)
""",
    "translations/en.yaml": """\
configuration:
  url:
    name: URL
    description: Where to connect.
  token:
    name: Token
    description: How to sign in.
  log_level:
    name: Log level
    description: How much to log.
network:
  8765/tcp: The API.
""",
    "CHANGELOG.md": """\
# Changelog

## 1.0.0

- First release.
""",
    "run.sh": "#!/bin/sh\nexec python3 -m example\n",
}


class Repo:
    """A repository in the working directory: main holds example/, and the branch change is checked out."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, path: str, text: str) -> None:
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text, encoding="utf-8")

    def edit(self, path: str, old: str, new: str) -> None:
        text = (self.root / path).read_text(encoding="utf-8")
        assert old in text
        self.write(path, text.replace(old, new))

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self, message: str = "change") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)


@pytest.fixture
def example(tmp_path, monkeypatch) -> Repo:
    """The example add-on in example/ of the working directory, without git."""
    root = tmp_path / "repo"
    repo = Repo(root)
    for path, text in EXAMPLE.items():
        repo.write(f"example/{path}", text)
    repo.write("repository.yaml", "name: Test add-ons\n")
    monkeypatch.chdir(root)
    return repo


@pytest.fixture
def repo(example, tmp_path, monkeypatch) -> Repo:
    """example/ committed on main, with git kept from this machine's settings (signing, line endings)."""
    (tmp_path / "gitconfig").write_text("")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    example.git("init", "-q", "-b", "main")
    example.commit("base")
    example.git("checkout", "-q", "-b", "change")
    return example


@pytest.fixture
def run(capsys):
    """Runs python3 -m addons with these arguments: its exit status and its ::error:: lines."""

    def run(*argv: str) -> tuple[int, list[str]]:
        status = cli.main(list(argv))
        lines = capsys.readouterr().out.splitlines()
        return status, [line.removeprefix("::error::") for line in lines if line.startswith("::error::")]

    return run


class FakeRegistry:
    """ghcr.io for one account: the token endpoint, manifests by tag, and blob uploads."""

    def __init__(self, user: str, password: str) -> None:
        self.basic = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.bearer = "registry-token"
        self.may_push = True
        self.tags: set[str] = set()  # repository:tag
        self.uploads: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, method: str, path: str, headers: dict[str, str], body: bytes | None):
        self.calls.append(f"{method} {path}")
        if path == "/token":
            if headers.get("authorization") != self.basic:
                return 403, {}, b'{"errors":[{"code":"DENIED","message":"denied"}]}'
            return 200, {}, json.dumps({"token": self.bearer}).encode()
        if headers.get("authorization") != f"Bearer {self.bearer}":
            return 401, {}, b'{"errors":[{"code":"UNAUTHORIZED"}]}'
        if found := re.fullmatch(r"/v2/(.+)/manifests/([^/]+)", path):
            return (200 if ":".join(found.groups()) in self.tags else 404), {}, b""
        if (found := re.fullmatch(r"/v2/(.+)/blobs/uploads/", path)) and method == "POST":
            if not self.may_push:
                return 403, {}, b'{"errors":[{"code":"DENIED","message":"permission_denied"}]}'
            upload = f"/v2/{found[1]}/blobs/uploads/{uuid.uuid4()}"
            self.uploads.add(upload)
            return 202, {"Location": upload}, b""
        if path in self.uploads and method == "DELETE":
            self.uploads.remove(path)
            return 204, {}, b""
        return 404, {}, b""


class FakeGitea:
    """Gitea's release endpoint of one repository, for the run's token."""

    def __init__(self, repository: str, token: str) -> None:
        self.path = f"/api/v1/repos/{repository}/releases"
        self.token = token
        self.tags: set[str] = set()
        self.releases: list[dict] = []

    def __call__(self, method: str, path: str, headers: dict[str, str], body: bytes | None):
        if (method, path) != ("POST", self.path):
            return 404, {}, b""
        if headers.get("authorization") != f"token {self.token}":
            return 401, {}, b'{"message":"token is required"}'
        release = json.loads(body)
        if release["tag_name"] in self.tags:
            return 409, {}, b'{"message":"tag already exists"}'
        self.tags.add(release["tag_name"])
        self.releases.append(release)
        return 201, {}, b'{"id":1}'


class FakeNetwork:
    """Stands in for addons.api's opener: each request goes to the fake of its host, and an error status comes back
    as urllib raises it."""

    def __init__(self) -> None:
        self.hosts: dict = {}

    def open(self, request, timeout=None):
        url = urlsplit(request.full_url)
        if url.netloc not in self.hosts:
            raise URLError(f"no route to {url.netloc}")
        headers = {name.lower(): value for name, value in request.header_items()}
        status, answer_headers, body = self.hosts[url.netloc](request.get_method(), url.path, headers, request.data)
        message = Message()
        for name, value in answer_headers.items():
            message[name] = value
        if status >= 400:
            raise HTTPError(request.full_url, status, "", message, io.BytesIO(body))
        return addinfourl(io.BytesIO(body), message, request.full_url, status)


@pytest.fixture
def network(monkeypatch) -> FakeNetwork:
    fake = FakeNetwork()
    monkeypatch.setattr(api, "opener", fake)
    return fake


@pytest.fixture
def ghcr(network) -> FakeRegistry:
    network.hosts["ghcr.io"] = FakeRegistry("pusher", "ghp_push")
    return network.hosts["ghcr.io"]


@pytest.fixture
def gitea(network) -> FakeGitea:
    network.hosts["git.example"] = FakeGitea("owner/ha-addons", "job-token")
    return network.hosts["git.example"]
