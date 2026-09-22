"""Replace generated text files without truncating an existing valid file."""

import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile


def atomic_write_text(path: Path, text: str) -> None:
    path = path.resolve()
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
