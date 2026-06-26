from __future__ import annotations
from pathlib import Path
from typing import Any
import errno, json, os, threading, time, uuid


def is_transient_write_error(exc: BaseException) -> bool:
    if isinstance(exc, (PermissionError, TimeoutError, BlockingIOError, InterruptedError)):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, 'errno', None) in {errno.EACCES, errno.EPERM, errno.EBUSY, errno.ETXTBSY, errno.EAGAIN, errno.EINTR}
    return False


def durable_write_text(path: Path, text: str, *, encoding: str='utf-8', attempts: int=8, initial_delay_seconds: float=0.025) -> None:
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    delay=initial_delay_seconds; last: BaseException|None=None
    for i in range(max(1, attempts)):
        tmp=path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp')
        try:
            with tmp.open('w', encoding=encoding, newline='\n') as f:
                f.write(text); f.flush()
                try: os.fsync(f.fileno())
                except OSError: pass
            os.replace(tmp, path)
            try:
                dfd=os.open(str(path.parent), os.O_RDONLY)
                try: os.fsync(dfd)
                finally: os.close(dfd)
            except OSError: pass
            return
        except BaseException as exc:
            last=exc
            try: tmp.unlink(missing_ok=True)
            except Exception: pass
            if i >= attempts-1 or not is_transient_write_error(exc):
                raise
            time.sleep(delay); delay=min(delay*2, 1.0)
    if last: raise last


def durable_write_json(path: Path, data: Any, *, indent: int=2, attempts: int=8, initial_delay_seconds: float=0.025) -> None:
    durable_write_text(Path(path), json.dumps(data, indent=indent, ensure_ascii=False, default=str), attempts=attempts, initial_delay_seconds=initial_delay_seconds)
