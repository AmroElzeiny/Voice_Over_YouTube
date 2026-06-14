from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.build_info import APP_BUILD_ID


FORBIDDEN_MARKERS = {
    "app.py": ["st.context.headers", "browser_user_agent", "probe_youtube_duration"],
    "src/worker.py": ["request_user_agent"],
    "src/youtube.py": ["web_safari", "web_embedded", "youtubepot-wpc"],
}


def main() -> int:
    failed = False
    print(f"build_id={APP_BUILD_ID}")
    for relative_path, markers in FORBIDDEN_MARKERS.items():
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for marker in markers:
            if marker in source:
                print(f"STALE_MARKER_FOUND {relative_path}: {marker}")
                failed = True
    if failed:
        print("Deployment sanity check failed. The app still contains the old YouTube downloader path.")
        return 1
    print("Deployment sanity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
