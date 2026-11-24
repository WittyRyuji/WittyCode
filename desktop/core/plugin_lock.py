"""Cross-process locks for small file transactions (never hold one in the UI thread)."""

from contextlib import contextmanager
from pathlib import Path
import sys
import time

from desktop.core.plugin_http import PluginError


@contextmanager
def lock_file(file: Path, *, busy_message: str = "插件配置正在使用中，请稍后重试。"):
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.with_suffix(".lock").open("a+b") as stream:
        # Windows can lock a byte past EOF; do not write a starter byte before acquiring the lock.
        deadline = time.monotonic() + 65
        while True:
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PluginError(busy_message)
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
