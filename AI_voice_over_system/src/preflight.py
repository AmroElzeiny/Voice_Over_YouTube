from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import media


def probe_uploaded_duration(file_obj: BinaryIO, filename: str) -> float:
    suffix = Path(filename).suffix or ".bin"
    current_position = file_obj.tell()
    try:
        file_obj.seek(0)
        with tempfile.TemporaryDirectory(prefix="voiceover-check-") as temp_dir:
            path = Path(temp_dir) / f"source{suffix}"
            with path.open("wb") as output:
                shutil.copyfileobj(file_obj, output, length=1024 * 1024)
            duration = media.probe_duration(path)
    finally:
        file_obj.seek(current_position)
    if duration <= 0:
        raise media.MediaError("لم أتمكن من معرفة مدة الملف.")
    return duration
