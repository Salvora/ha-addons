"""inotify on /config: an edit to a tracked path restarts a quiet timer; when it runs out, a capture is queued."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from inotify_simple import INotify, flags

from ha_gitops_sync.git import Git
from ha_gitops_sync.worktree import directories, ignored

log = logging.getLogger(__name__)

QUIET = 60
MASK = flags.CLOSE_WRITE | flags.MOVED_TO | flags.MOVED_FROM | flags.CREATE | flags.DELETE
ARRIVED = flags.CREATE | flags.MOVED_TO


class Watcher:
    def __init__(self, git: Git, on_quiet: Callable[[], object], quiet: float = QUIET):
        self.git = git
        self.on_quiet = on_quiet
        self.quiet = quiet
        self._stop = threading.Event()
        self._timer: threading.Timer | None = None
        self._folders: dict[int, Path] = {}

    def run(self) -> None:
        with INotify() as inotify:
            self._watch(inotify, self.git.root)
            while not self._stop.is_set():
                if self._edited(inotify, inotify.read(timeout=1000, read_delay=100)):
                    self._restart_timer()
        if self._timer is not None:
            self._timer.cancel()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self, inotify: INotify, top: Path) -> None:
        """A vanished folder or the kernel's watch limit ends the walk; the daily check still finds those edits."""
        try:
            for folder in directories(self.git, top):
                self._folders[inotify.add_watch(folder, MASK)] = folder
        except OSError as e:
            log.warning("not watching everything under %s: %s", top, e)

    def _edited(self, inotify: INotify, events) -> bool:
        edited, files = False, []
        for event in events:
            if event.mask & flags.Q_OVERFLOW:
                edited = True
                continue
            if event.mask & flags.IGNORED:
                self._folders.pop(event.wd, None)
                continue
            folder = self._folders.get(event.wd)
            if folder is None or event.name in ("", ".git"):
                continue
            path = folder / event.name
            relative = path.relative_to(self.git.root).as_posix()
            if not event.mask & flags.ISDIR:
                files.append(relative)
            elif not ignored(self.git, [relative + "/"]):
                edited = True
                if event.mask & ARRIVED:
                    self._watch(inotify, path)
        return edited or bool(set(files) - ignored(self.git, files))

    def _restart_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.quiet, self.on_quiet)
        self._timer.daemon = True
        self._timer.start()
