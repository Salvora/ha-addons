"""What of /config the repository holds: repository-only paths, ignored folders, and edits waiting to be committed."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ha_gitops_sync.git import Git

REPOSITORY_ONLY = ".gitops/repository-only"
LARGE = 20 * 1024 * 1024


@dataclass(frozen=True)
class Changes:
    files: tuple[str, ...]
    skipped: tuple[str, ...]


def repository_only(git: Git) -> tuple[str, ...]:
    """The paths HEAD's .gitops/repository-only keeps off Home Assistant."""
    blob = git.output("rev-parse", "--verify", "--quiet", f"HEAD:{REPOSITORY_ONLY}").strip()
    lines = git.run("cat-file", "blob", blob).splitlines() if blob else []
    return tuple(line.strip().lstrip("/") for line in lines if line.strip() and not line.lstrip().startswith("#"))


def apply_sparse(git: Git) -> None:
    patterns = ["/*", *(f"!/{path}" for path in repository_only(git))]
    git.run("sparse-checkout", "set", "--no-cone", "--stdin", stdin="\n".join(patterns) + "\n")


def listed(path: str, entries: tuple[str, ...]) -> bool:
    return any(path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/") for entry in entries)


def _large(path: Path) -> bool:
    try:
        return path.lstat().st_size > LARGE
    except FileNotFoundError:
        return False


def _paths(git: Git, *args: str) -> set[str]:
    return set(filter(None, git.run(*args).split("\0")))


def changes(git: Git) -> Changes:
    """Edits not yet committed, compared by content after .gitattributes, so a resave that only changes line endings
    is none. Skipped: repository-only paths, new files over 20 MB, and nested repositories, which ls-files shows as a
    folder and git would record as a gitlink instead of their files."""
    tracked = _paths(git, "diff", "--name-only", "-z", "--no-renames", "HEAD")
    untracked = _paths(git, "ls-files", "--others", "--exclude-standard", "-z")
    entries = repository_only(git)
    files, skipped = [], []
    for path in sorted(tracked | untracked):
        new = path in untracked
        left_out = listed(path, entries) or (new and (path.endswith("/") or _large(git.root / path)))
        (skipped if left_out else files).append(path)
    return Changes(tuple(files), tuple(skipped))


def refresh(git: Git) -> None:
    """merge counts a size change as a local change, so files equal to HEAD after .gitattributes get a fresh stat."""
    stale = _paths(git, "diff-files", "--name-only", "-z") - _paths(
        git, "diff", "--name-only", "-z", "--no-renames", "HEAD"
    )
    if stale:
        git.run(
            "--literal-pathspecs",
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin="\0".join(sorted(stale)),
        )


def commit(git: Git, files: tuple[str, ...], message: str) -> str:
    git.run(
        "--literal-pathspecs", "add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul", stdin="\0".join(files)
    )
    git.run("commit", "--quiet", "--message", message)
    return git.run("rev-parse", "HEAD").strip()


def ignored(git: Git, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    return set(filter(None, git.output("check-ignore", "--stdin", "-z", stdin="\0".join(paths) + "\0").split("\0")))


def directories(git: Git, top: Path | None = None) -> Iterator[Path]:
    """top (the root by default) and every folder under it that .gitignore keeps, not entering nested repositories."""
    pending = [top or git.root]
    while pending:
        folder = pending.pop()
        yield folder
        if folder != git.root and (folder / ".git").exists():
            continue
        subfolders = {
            f"{p.relative_to(git.root).as_posix()}/": p
            for p in folder.iterdir()
            if p.is_dir() and not p.is_symlink() and p.name != ".git"
        }
        skip = ignored(git, list(subfolders))
        pending.extend(path for name, path in subfolders.items() if name not in skip)


def nested_repositories(git: Git) -> list[str]:
    return [d.relative_to(git.root).as_posix() for d in directories(git) if d != git.root and (d / ".git").exists()]
