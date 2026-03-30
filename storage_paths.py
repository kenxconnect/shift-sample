from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_ROOT / ".data"


def data_dir() -> Path:
    configured = os.getenv("SHIFT_APP_DATA_DIR", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_file(name: str) -> Path:
    return data_dir() / name


@contextmanager
def exclusive_lock(path: Path):
    """同一データファイルへの更新を直列化するための排他ロック。"""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, content: str) -> None:
    """一時ファイルに書き込んでから rename するアトミック書き込み。

    書き込み途中のクラッシュやディスク満杯でも既存ファイルを壊さない。
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        # Windows では上書き rename に replace を使う
        Path(tmp_path).replace(path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            logging.warning("一時ファイルの削除に失敗しました: %s", tmp_path)
        raise


def safe_migrate_file(src: Path, dst: Path) -> None:
    """レガシーファイルを新パスにコピー。元ファイルは残す（手動削除用）。"""
    if not src.exists() or dst.exists():
        return
    try:
        content = src.read_text(encoding="utf-8")
        atomic_write_text(dst, content)
    except OSError:
        logging.warning("レガシーファイルの移行に失敗しました: %s -> %s", src, dst, exc_info=True)
