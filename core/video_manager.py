from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
EXT_PRIORITY = {".mp4": 0, ".mov": 1, ".webm": 2, ".mkv": 3}


@dataclass
class VideoInfo:
    name: str
    path: str


def natural_key(text: str):
    return [(1, int(p)) if p.isdigit() else (0, p.lower()) for p in re.split(r"(\d+)", text)]


def discover_videos(folder: Path) -> list[VideoInfo]:
    folder = Path(folder)
    if not folder.is_dir():
        return []

    best_by_stem: dict[str, tuple[int, Path]] = {}
    for p in folder.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        stem = p.stem.lower()
        rank = EXT_PRIORITY[ext]
        current = best_by_stem.get(stem)
        if current is None or rank < current[0]:
            best_by_stem[stem] = (rank, p)

    ordered = sorted(best_by_stem.values(), key=lambda t: (natural_key(t[1].stem), t[1].name.lower()))
    return [VideoInfo(name=p.name, path=str(p.resolve())) for _, p in ordered]
