"""Project-wide work lock shared by every Studio backend that opens a project.

Studio's threading locks only serialize work inside one backend. Two backends
on the same project (two accounts on one SSH host, a local Studio and a remote
one over a shared directory, an orphaned backend) also exclude each other
through ``.seeknal/studio.lock``.

``flock`` is used deliberately rather than POSIX ``fcntl``/``lockf`` record
locks: a POSIX lock belongs to the process and is released as soon as the
process closes *any* descriptor for the file, so merely reading the holder
record, or a second thread opening the file, would silently drop the lock.
``flock`` belongs to one open file description, so it is unaffected by other
descriptors and works across threads. The OS releases it when the last
process sharing that description exits, however it exits; a job process that
inherits it keeps the project locked after its backend is killed. Within one backend a reference count shares
the single lock between overlapping operations (a queued job, a save).

NFS caveat: Linux emulates ``flock`` on NFS with whole-file byte-range locks
served by the NFS lock manager (NLM for NFSv3, built into NFSv4), so the lock
works across clients only when that service is available; some mounts
(``nolock``, ``local_lock=all``) keep locks local to one machine. Where the
filesystem refuses locks, Studio refuses work with an explanation rather than
running unprotected.
"""

from __future__ import annotations

import errno
import fcntl
import getpass
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
from typing import Optional

LOCK_PARTS = (".seeknal", "studio.lock")
RECORD_LIMIT = 4096
FIELD_LIMIT = 64
MESSAGE_LIMIT = 400
NO_RECORD = (
    "This project's Studio lock is held by a process that left no record, possibly a background "
    "process started by an earlier run or another account's Studio; it is released when that "
    "process exits."
)


class ProjectBusyError(RuntimeError):
    """Another process holds the project lock (or it cannot be taken safely)."""

    def __init__(self, message: str, holder: Optional[dict] = None) -> None:
        super().__init__(message)
        self.holder = holder or {}


def _text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable())[:FIELD_LIMIT].strip()
    return cleaned or None


def sanitize(record: object) -> dict:
    """Keep only well-typed, printable, bounded fields of a holder record.

    Anyone who can write the project can write the record, so nothing from it
    is trusted: no control characters, no unbounded text, no out-of-range time."""
    if not isinstance(record, dict):
        return {}
    holder: dict = {}
    for key in ("user", "host", "action"):
        text = _text(record.get(key))
        if text:
            holder[key] = text
    pid = record.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**31:
        holder["pid"] = pid
    started = record.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        try:
            holder["since"] = time.strftime("%H:%M", time.localtime(started))
        except (OverflowError, ValueError, OSError, TypeError):
            pass
    return holder


def describe(holder: dict) -> str:
    """A message built only from sanitize()d fields."""
    if not holder:
        return NO_RECORD
    who = []
    if "user" in holder and "host" in holder:
        who.append(f"{holder['user']} on {holder['host']}")
    if "pid" in holder:
        who.append(f"pid {holder['pid']}")
    if "since" in holder:
        who.append(f"since {holder['since']}")
    doing = f"running '{holder['action']}'" if "action" in holder else "working"
    detail = f" ({', '.join(who)})" if who else ""
    return f"Another Studio is {doing} in this project{detail}. Try again when it finishes."[:MESSAGE_LIMIT]


def _open_lock(project: Path) -> int:
    """Open .seeknal/studio.lock without following a symlink at any level.

    Anyone who can open the file can hold the lock (flock works on a read-only
    descriptor), so it is never readable by "other": the directory is created
    0770 and the file 0660, both still narrowed by the umask, so a group-shared
    project (umask 002) stays usable across its members. Waiters open without
    O_TRUNC and never write, so they cannot wipe the holder's record."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(project, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.mkdir(LOCK_PARTS[0], 0o770, dir_fd=directory)
        except FileExistsError:
            pass
        internal = os.open(LOCK_PARTS[0], os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        flags = os.O_CREAT | nofollow | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(LOCK_PARTS[1], os.O_RDWR | flags, 0o660, dir_fd=internal)
        except PermissionError:
            # Another account created it without group write: locking still
            # works read-only; only the holder record cannot be written.
            descriptor = os.open(LOCK_PARTS[1], os.O_RDONLY | flags, 0o660, dir_fd=internal)
    finally:
        os.close(internal)
    info = os.fstat(descriptor)
    # A FIFO, device or a hard link to someone's file must never be locked or
    # rewritten in place of the lock file.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ProjectBusyError("Studio refused the project lock: .seeknal/studio.lock is not a regular file")
    if info.st_uid == os.geteuid() and info.st_mode & 0o007:
        # An older or hand-made lock file: stop other accounts from opening it.
        try:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & ~0o007)
        except OSError:
            pass
    return descriptor


def read_holder(descriptor: int) -> dict:
    """The holder's record, read from at most RECORD_LIMIT bytes and sanitized."""
    try:
        raw = os.pread(descriptor, RECORD_LIMIT, 0)
        value = json.loads(raw.split(b"\n", 1)[0] or b"null")
    except (OSError, ValueError, RecursionError):
        return {}
    return sanitize(value)


class Lease:
    def __init__(self, owner: "ProjectLock") -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._owner._release()

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class ProjectLock:
    def __init__(self, project: Path) -> None:
        self.project = project
        self._guard = threading.Lock()
        self._count = 0
        self._descriptor: Optional[int] = None

    def acquire(self, action: str) -> Lease:
        """Take the project lock without waiting, or raise ProjectBusyError naming the holder."""
        with self._guard:
            if self._count == 0:
                try:
                    descriptor = _open_lock(self.project)
                except ProjectBusyError:
                    raise
                except OSError as error:
                    raise ProjectBusyError(f"Studio could not open the project lock: {error.strerror or error}") from error
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    holder = read_holder(descriptor)
                    os.close(descriptor)
                    if error.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                        raise ProjectBusyError(describe(holder), holder) from error
                    raise ProjectBusyError(
                        f"Studio cannot lock this project ({error.strerror}); the filesystem may not support locks",
                    ) from error
                self._record(descriptor, action)
                self._descriptor = descriptor
            self._count += 1
            return Lease(self)

    def _record(self, descriptor: int, action: str) -> None:
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = str(os.getuid())
        line = json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "user": user,
            "started_at": time.time(), "action": action,
        }).encode() + b"\n"
        try:
            # Overwrite then trim: a waiter reads up to the first newline and so
            # never sees an empty or half-old record.
            os.pwrite(descriptor, line, 0)
            os.ftruncate(descriptor, len(line))
        except OSError:
            pass

    def _release(self) -> None:
        with self._guard:
            self._count -= 1
            if self._count == 0 and self._descriptor is not None:
                descriptor, self._descriptor = self._descriptor, None
                # Clear the record so a later holder that cannot write it is not
                # reported as this, finished, operation. Closing (not LOCK_UN,
                # which would also unlock a job process sharing the descriptor)
                # releases the lock once no process holds it any more.
                try:
                    os.ftruncate(descriptor, 0)
                except OSError:
                    pass
                os.close(descriptor)

    def inherited_fds(self) -> tuple[int, ...]:
        """The lock descriptor, for a work process that must keep the project
        locked if this backend is killed while it still runs."""
        with self._guard:
            return (self._descriptor,) if self._descriptor is not None else ()

    def held(self) -> bool:
        with self._guard:
            return self._count > 0
