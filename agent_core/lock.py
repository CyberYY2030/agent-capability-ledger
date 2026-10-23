from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError


def _make_lock_authority():
    class Token:
        __slots__ = ()

    active_roots: dict[int, tuple[object, Path]] = {}

    def validate(token: object, control_root: Path) -> None:
        resolved_root = control_root.resolve()
        active = active_roots.get(id(token))
        if active is None or active[0] is not token or active[1] != resolved_root:
            raise ConfigError("FAIL_LOCK_TOKEN", str(resolved_root))

    @contextmanager
    def lock(control_root: Path):
        resolved_root = control_root.resolve()
        path = resolved_root / "locks" / "promote.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                raise ConfigError("FAIL_LOCKED", str(path)) from exc
            token = Token()
            active_roots[id(token)] = (token, resolved_root)
            try:
                yield token
            finally:
                active_roots.pop(id(token), None)
        finally:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            stream.close()

    return lock, validate


operation_lock, _validate_lock_token = _make_lock_authority()
del _make_lock_authority
