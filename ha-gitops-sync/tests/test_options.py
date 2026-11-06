import ipaddress
import json
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from ha_gitops_sync.auth import Auth
from ha_gitops_sync.options import Options, load

ADDON = Path(__file__).resolve().parent.parent
VALID = {
    "gitea_url": "https://git.example.org/",
    "repository": "home/config",
    "gitea_token": "gitea-token-value",
    "deploy_key": "d" * 64,
    "control_key": "c" * 64,
    "allowed_sources": ["192.0.2.10/32", "2001:db8::10", "198.51.100.0/24"],
    "log_level": "info",
}


def options_file(tmp_path, **changes) -> Path:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(VALID | changes))
    return path


def test_the_options_are_read(tmp_path):
    options = load(options_file(tmp_path))

    assert options.gitea_url == "https://git.example.org"
    assert options.remote_url == "https://git.example.org/home/config.git"
    assert options.deploy_key == b"d" * 64
    assert options.control_key == b"c" * 64
    assert options.allowed_sources == (
        ipaddress.ip_network("192.0.2.10/32"),
        ipaddress.ip_network("2001:db8::10/128"),
        ipaddress.ip_network("198.51.100.0/24"),
    )


def test_the_token_and_keys_never_show_in_the_options_text(tmp_path):
    text = repr(load(options_file(tmp_path)))

    assert "gitea-token-value" not in text
    assert "d" * 64 not in text
    assert "c" * 64 not in text


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"deploy_key": "short"}, "deploy_key"),
        ({"control_key": "c" * 31}, "control_key"),
        ({"control_key": "d" * 64}, "differ"),
        ({"allowed_sources": ["runner.example.org"]}, "allowed_sources"),
    ],
)
def test_unusable_options_stop_the_start_and_say_which(tmp_path, change, named):
    with pytest.raises(SystemExit, match=named):
        load(options_file(tmp_path, **change))


def test_no_allowed_sources_allows_no_caller(tmp_path):
    options = load(options_file(tmp_path, allowed_sources=[]))

    assert options.allowed_sources == ()
    assert not Auth(options.allowed_sources).source_allowed("192.0.2.10")


def test_the_manifest_offers_exactly_the_options_the_add_on_reads_and_ships_them_empty():
    manifest = yaml.safe_load((ADDON / "config.yaml").read_text())
    names = {field.name for field in fields(Options)}

    assert set(manifest["options"]) == set(manifest["schema"]) == names
    assert manifest["options"] == dict.fromkeys(names, None) | {"allowed_sources": [], "log_level": "info"}
