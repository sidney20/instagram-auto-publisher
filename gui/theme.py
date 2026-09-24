from __future__ import annotations

import customtkinter as ctk

BG = "#12131b"
SURF_LOWEST = "#0c0e15"
SURF_LOW = "#1a1b23"
SURF = "#1e1f27"
SURF_HIGH = "#282932"
SURF_HIGHEST = "#33343d"
BORDER = "#444654"
OUTLINE = "#8e8fa0"
TEXT = "#e2e1ed"
MUTED = "#c5c5d7"
DIM = "#6b6c7a"

PRIMARY = "#405de6"
PRIMARY_BRIGHT = "#5570ee"
PRIMARY_SOFT = "#bac3ff"
ON_PRIMARY = "#ebebff"

ACCENT_PINK = "#b7004f"
ACCENT_TEXT = "#ffb1c0"
TERTIARY = "#ffb692"
WARN = "#ff9f43"
ERROR = "#ffb4ab"
ERROR_DEEP = "#93000a"

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def headline(size: int = 24) -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight="bold")


def body(size: int = 13) -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size)


def label_caps(size: int = 11) -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight="bold")


def code(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=MONO_FAMILY, size=size)


STATUS_ICONS = {
    "pending": "○",
    "uploading": "↻",
    "processing": "↻",
    "describing": "↻",
    "publishing": "↻",
    "retrying": "↻",
    "preparing_retry": "↻",
    "published": "✓",
    "error": "✗",
    "skipped": "◌",
}

STATUS_COLORS = {
    "pending": OUTLINE,
    "uploading": ACCENT_TEXT,
    "processing": ACCENT_TEXT,
    "describing": ACCENT_TEXT,
    "publishing": ACCENT_TEXT,
    "retrying": WARN,
    "preparing_retry": WARN,
    "published": PRIMARY_SOFT,
    "error": ERROR,
    "skipped": DIM,
}

ACTIVE_STATUSES = {"uploading", "processing", "describing", "publishing",
                   "retrying", "preparing_retry"}
