from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

PENDING = "pending"
UPLOADING = "uploading"
PROCESSING = "processing"
DESCRIBING = "describing"
PUBLISHING = "publishing"
PUBLISHED = "published"
ERROR = "error"
SKIPPED = "skipped"
RETRYING = "retrying"
PREPARING_RETRY = "preparing_retry"

ACTIVE_STATUSES = {UPLOADING, PROCESSING, DESCRIBING, PUBLISHING, RETRYING, PREPARING_RETRY}

STATUS_LABELS = {
    PENDING: "○ Aguardando",
    UPLOADING: "⏳ Enviando vídeo",
    PROCESSING: "⏳ Processando",
    DESCRIBING: "⏳ Inserindo descrição",
    PUBLISHING: "⏳ Publicando",
    PUBLISHED: "✓ Publicado",
    ERROR: "✗ Erro",
    SKIPPED: "◌ Ignorado",
    RETRYING: "↻ Tentando novamente",
    PREPARING_RETRY: "↻ Preparando retry",
}


@dataclass
class VideoState:
    name: str
    path: str
    status: str = PENDING
    attempts: int = 0
    error: Optional[str] = None
    published_at: Optional[str] = None
    custom_description: Optional[str] = None
    custom_hashtags: Optional[str] = None


@dataclass
class SessionState:
    version: int = 1
    folder: str = ""
    description: str = ""
    allow_no_description: bool = False
    created_at: str = ""
    updated_at: str = ""
    videos: list[VideoState] = field(default_factory=list)

    def published_count(self) -> int:
        return sum(1 for v in self.videos if v.status == PUBLISHED)

    def failed_count(self) -> int:
        return sum(1 for v in self.videos if v.status == ERROR)

    def pending_count(self) -> int:
        return sum(1 for v in self.videos if v.status not in (PUBLISHED, SKIPPED))


class StateManager:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state: Optional[SessionState] = None

    def new_session(self, folder: str, description: str, allow_no_description: bool,
                    videos: list, custom_descriptions: dict = None,
                    custom_hashtags: dict = None) -> SessionState:
        now = datetime.now().isoformat(timespec="seconds")
        descs = custom_descriptions or {}
        tags = custom_hashtags or {}
        self.state = SessionState(
            folder=folder,
            description=description,
            allow_no_description=allow_no_description,
            created_at=now,
            updated_at=now,
            videos=[
                VideoState(
                    name=v.name, path=v.path,
                    custom_description=descs.get(v.name),
                    custom_hashtags=tags.get(v.name),
                )
                for v in videos
            ],
        )
        self.save()
        return self.state

    def load(self) -> Optional[SessionState]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        try:
            st = SessionState(
                version=int(raw.get("version", 1)),
                folder=str(raw.get("folder", "")),
                description=str(raw.get("description", "")),
                allow_no_description=bool(raw.get("allow_no_description", False)),
                created_at=str(raw.get("created_at", "")),
                updated_at=str(raw.get("updated_at", "")),
                videos=[
                    VideoState(
                        name=str(v.get("name", "")),
                        path=str(v.get("path", "")),
                        status=str(v.get("status", PENDING)),
                        attempts=int(v.get("attempts", 0)),
                        error=v.get("error"),
                        published_at=v.get("published_at"),
                        custom_description=v.get("custom_description"),
                        custom_hashtags=v.get("custom_hashtags"),
                    )
                    for v in raw.get("videos", [])
                    if isinstance(v, dict)
                ],
            )
        except Exception:
            return None
        if not st.videos:
            return None
        for v in st.videos:
            if v.status in ACTIVE_STATUSES:
                v.status = PENDING
        self.state = st
        return st

    def is_resumable(self) -> bool:
        st = self.state
        return bool(
            st
            and st.folder
            and st.videos
            and st.pending_count() > 0
        )

    def save(self) -> None:
        if not self.state:
            return
        self.state.updated_at = datetime.now().isoformat(timespec="seconds")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self.state), indent=2, ensure_ascii=False)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self.state_file.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, self.state_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def discard(self) -> None:
        self.state = None
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def mark_errors_skipped(self) -> None:
        if not self.state:
            return
        for v in self.state.videos:
            if v.status == ERROR:
                v.status = SKIPPED
        self.save()
