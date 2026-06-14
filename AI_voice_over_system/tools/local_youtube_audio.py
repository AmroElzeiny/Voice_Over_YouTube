from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import DownloadError, YoutubeDL


def valid_youtube_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower().split(":", 1)[0]
    return parsed.scheme in {"http", "https"} and (
        host == "youtu.be"
        or host in {"youtube.com", "youtube-nocookie.com"}
        or host.endswith((".youtube.com", ".youtube-nocookie.com"))
    )


def build_ydl_options(output_dir: Path, browser: str | None = None) -> dict:
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "youtube_audio_%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 2,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
    }
    if browser:
        options["cookiesfrombrowser"] = (browser,)
    return options


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download audio from a YouTube video on this computer."
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--browser",
        choices=("chrome", "edge", "firefox", "brave"),
        help="Read YouTube cookies from a browser on this computer.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Folder for the resulting MP3 file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not valid_youtube_url(args.url):
        raise SystemExit("The URL is not a valid YouTube URL.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required and was not found in PATH.")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with YoutubeDL(build_ydl_options(output_dir, args.browser)) as downloader:
            info = downloader.extract_info(args.url, download=True)
    except DownloadError as exc:
        raise SystemExit(
            "yt-dlp could not download this video. Try --browser chrome, edge, firefox, or brave "
            "if YouTube asks you to sign in."
        ) from exc

    result = output_dir / f"youtube_audio_{info['id']}.mp3"
    if not result.exists() or result.stat().st_size == 0:
        raise SystemExit("The audio download did not produce a valid MP3 file.")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
