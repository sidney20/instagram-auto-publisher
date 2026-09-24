from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import requests

from config import APP_VERSION, BASE_DIR

REPO = "sidney20/instagram-auto-publisher"
ASSET_NAME = "instagram_auto_publisher.zip"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PROTECTED = {
    "data", "logs", "debug", "dist", ".venv", "__pycache__",
    "browser_profile", "browser_profile_perfil1", "browser_profile_perfil2",
}


def _ver_tuple(v: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", v or "")]
    return tuple(parts) if parts else (0,)


def check_for_update(current: str | None = None) -> dict | None:
    """Retorna {"version", "notes", "url"} se houver versao mais nova, senao None."""
    cur = current or APP_VERSION
    resp = requests.get(
        LATEST_URL,
        timeout=15,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "instagram-auto-publisher-updater"},
    )
    resp.raise_for_status()
    info = resp.json()
    tag = str(info.get("tag_name") or "").lstrip("vV")
    if not tag or _ver_tuple(tag) <= _ver_tuple(cur):
        return None
    asset = next(
        (a for a in info.get("assets") or [] if a.get("name") == ASSET_NAME),
        None,
    )
    if not asset or not asset.get("browser_download_url"):
        return None
    return {
        "version": tag,
        "notes": str(info.get("body") or ""),
        "url": str(asset["browser_download_url"]),
    }


def download_update(url: str, progress=None) -> Path:
    """Baixa o zip para uma pasta temporaria. progress(frac: float|None)."""
    tmp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    dest = tmp_dir / f"{ASSET_NAME}.new"
    with requests.get(url, stream=True, timeout=(15, 120),
                      headers={"User-Agent": "instagram-auto-publisher-updater"}) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done / total if total else None)
    return dest


def apply_update(zip_path: Path) -> None:
    """Extrai por cima do programa, nunca tocando em dados/sessoes do usuario."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            parts = PurePosixPath(member.filename.replace("\\", "/")).parts
            if not parts or ".." in parts:
                continue
            if parts[0] == "instagram_auto_publisher":
                parts = parts[1:]
            if not parts or parts[0] in PROTECTED:
                continue
            target = BASE_DIR.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    try:
        zip_path.unlink()
    except OSError:
        pass
    for sub in ("", "gui", "instagram", "core"):
        base = BASE_DIR / sub if sub else BASE_DIR
        for p in base.glob("__pycache__"):
            shutil.rmtree(p, ignore_errors=True)


def relaunch() -> None:
    """Reinicia o programa com o codigo novo e encerra o processo atual."""
    subprocess.Popen([sys.executable, "main.py"], cwd=str(BASE_DIR))
    os._exit(0)
