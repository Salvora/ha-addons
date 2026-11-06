"""An add-on folder as Home Assistant's store reads it: config.yaml, translations/en.yaml and CHANGELOG.md."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Self

import yaml

REGISTRY = "ghcr.io"  # where image.yml pushes, with the GHCR_PUSH_* login
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
IMAGE = re.compile(re.escape(REGISTRY) + r"(/[a-z0-9]+([._-][a-z0-9]+)*){2,}")
CONFIG_SUFFIXES = (".yaml", ".yml", ".json")
HEADING = re.compile(r"^## +\[?(?P<version>[^\]\s]+)\]?.*$", re.MULTILINE)


class AddonError(Exception):
    """What makes an add-on folder unusable, as one sentence per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class Version(NamedTuple):
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: object) -> Self:
        """MAJOR.MINOR.PATCH without leading zeros, so that str() gives back the exact tag Home Assistant pulls."""
        if not isinstance(value, str) or not VERSION.fullmatch(value):
            raise ValueError(f"version {value!r} is not MAJOR.MINOR.PATCH, as a string")
        return cls(*map(int, value.split(".")))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AddonError([f"{path.as_posix()}: no such file"]) from exc
    except yaml.YAMLError as exc:
        raise AddonError([f"{path.as_posix()}: not valid YAML: {exc}"]) from exc


def store_configs() -> list[Path]:
    """Every config file the store takes for an add-on: config.yaml (.yml, .json) at any depth below the working
    directory, outside folders whose name starts with a dot or is rootfs."""
    found = (path for path in Path().glob("**/config.*") if path.suffix in CONFIG_SUFFIXES)
    return sorted(path for path in found if not any(part.startswith(".") or part == "rootfs" for part in path.parts))


@dataclass(frozen=True)
class Addon:
    config: Path  # relative to the repository root, like every path in a message
    version: Version
    image: str
    options: frozenset[str]  # the Configuration tab's fields: config.yaml's options and schema
    ports: frozenset[str]

    @classmethod
    def load(cls, config: Path) -> Self:
        manifest = read_yaml(config)
        if not isinstance(manifest, dict):
            raise AddonError([f"{config.as_posix()}: not a mapping"])
        problems = []
        try:
            version = Version.parse(manifest.get("version"))
        except ValueError as exc:
            problems.append(f"{config.as_posix()}: {exc}")
        image = manifest.get("image")
        if not isinstance(image, str) or not IMAGE.fullmatch(image):
            problems.append(
                f"{config.as_posix()}: image {image!r} is not a lower-case {REGISTRY} repository without a tag; "
                "Home Assistant pulls <image>:<version> and image.yml pushes it there"
            )
        if problems:
            raise AddonError(problems)
        options = (manifest.get("options") or {}).keys() | (manifest.get("schema") or {}).keys()
        return cls(config, version, image, frozenset(options), frozenset(manifest.get("ports") or {}))

    @property
    def folder(self) -> Path:
        return self.config.parent

    @property
    def translations(self) -> Path:
        return self.folder / "translations" / "en.yaml"

    @property
    def changelog(self) -> Path:
        return self.folder / "CHANGELOG.md"

    def release_notes(self) -> str:
        """The text of CHANGELOG.md's section for this version, under '## <version>' (or '## [<version>]', with
        anything after it) up to the next '## ' heading."""
        text = self.changelog.read_text(encoding="utf-8") if self.changelog.is_file() else ""
        for heading in HEADING.finditer(text):
            following = HEADING.search(text, heading.end())
            notes = text[heading.end() : following.start() if following else len(text)].strip()
            if heading["version"] == str(self.version) and notes:
                return notes
        raise AddonError([f"{self.changelog.as_posix()}: no '## {self.version}' section saying what changed"])
