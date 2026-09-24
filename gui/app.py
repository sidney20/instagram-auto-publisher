from __future__ import annotations

import io
import os
import queue
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageOps, ImageDraw
import requests

from config import APP_VERSION, BASE_DIR, DATA_DIR, LOGS_DIR, PROFILE1_DIR, PROFILE2_DIR
from core.logger import get_logger
from core.state_manager import (
    ERROR,
    PENDING,
    PUBLISHED,
    STATUS_LABELS,
    StateManager,
)
from core.video_manager import VideoInfo, discover_videos
from core import window_lock as wlock
from core import updater as upd
from gui import theme as T
from instagram.publisher import Publisher

PROFILE_DIRS = {1: PROFILE1_DIR, 2: PROFILE2_DIR}

SIDEBAR_W = 250
RIGHT_COL_W = 340


def _normalize_label(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def _is_credit_label(token: str) -> bool:
    t = _normalize_label(token)
    if not t:
        return False
    if t.startswith("card"):
        return False
    if t == "foto":
        return True
    if not t.startswith("cr"):
        return False
    return SequenceMatcher(None, "creditos", t).ratio() >= 0.6


class MonitorPanel:
    def __init__(self, parent):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self.frame, width=700, height=300,
                            fg_color=T.SURF_LOW, border_width=1,
                            border_color=T.BORDER, corner_radius=12)
        card.pack(fill="x", padx=4, pady=(4, 8))
        card.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        left_head = ctk.CTkFrame(head, fg_color="transparent")
        left_head.pack(side="left", padx=16, pady=8)
        ctk.CTkLabel(left_head, text="MONITOR AO VIVO",
                     font=T.label_caps(), text_color=T.PRIMARY_SOFT).pack(side="left")
        self.status_lbl = ctk.CTkLabel(left_head, text="• Aguardando publicação",
                                       font=T.body(11), text_color=T.MUTED)
        self.status_lbl.pack(side="left", padx=(12, 0))

        self.back_btn = ctk.CTkButton(
            head, text="↩ Dashboard", width=100, height=26,
            font=T.body(11), fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            corner_radius=6, command=lambda: self.frame.winfo_toplevel()._show_dashboard(),
        )
        self.back_btn.pack(side="right", padx=12, pady=8)

        self.frozen = False
        freeze_row = ctk.CTkFrame(head, fg_color="transparent")
        freeze_row.pack(side="right", padx=(0, 4))
        self.freeze_btn = ctk.CTkButton(
            freeze_row, text="⏸ Congelar", width=80, height=26,
            font=T.body(11), fg_color=T.SURF_HIGH, hover_color=T.WARN,
            corner_radius=6, command=self._toggle_freeze,
        )
        self.freeze_btn.pack(side="left")

        speed_row = ctk.CTkFrame(head, fg_color="transparent")
        speed_row.pack(side="right", padx=(0, 8))
        ctk.CTkLabel(speed_row, text="Intervalo:",
                     font=T.body(10), text_color=T.MUTED).pack(side="left", padx=(0, 4))
        self.speed_var = ctk.StringVar(value="1s")
        ctk.CTkOptionMenu(
            speed_row, values=["1s", "2s", "5s"], variable=self.speed_var,
            width=50, height=26, font=T.body(11),
            fg_color=T.SURF_HIGH, button_color=T.BORDER,
        ).pack(side="left")

        img_card = ctk.CTkFrame(card, fg_color=T.BG, corner_radius=0,
                                border_width=1, border_color=T.BORDER)
        img_card.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        img_card.columnconfigure(0, weight=1)
        img_card.rowconfigure(0, weight=1)

        self.placeholder = ctk.CTkLabel(
            img_card, text="🖥  Inicie a publicação para ver\no navegador aqui ao vivo",
            font=T.headline(16), text_color=T.DIM, justify="center",
        )
        self.placeholder.grid(row=0, column=0, sticky="nsew")

        self.image_label = ctk.CTkLabel(img_card, text="", anchor="center")
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.image_label.grid_remove()

        self._ctk_image = None
        self._last_bytes = None
        self._last_update = 0.0
        self._resize_job = None
        img_card.bind("<Configure>", self._schedule_image_resize)

    def _toggle_freeze(self):
        self.frozen = not self.frozen
        if self.frozen:
            self.freeze_btn.configure(text="▶ Continuar", fg_color=T.PRIMARY,
                                      hover_color=T.PRIMARY_BRIGHT)
        else:
            self.freeze_btn.configure(text="⏸ Congelar", fg_color=T.SURF_HIGH,
                                      hover_color=T.WARN)
            if self._last_bytes:
                self._update_image(self._last_bytes)

    def _schedule_image_resize(self, _event=None):
        if not self._last_bytes or self._resize_job is not None:
            return
        self._resize_job = self.frame.after(100, self._resize_image)

    def _resize_image(self):
        self._resize_job = None
        if self._last_bytes:
            self._update_image(self._last_bytes, force=True)

    def _update_image(self, png_bytes: bytes, force: bool = False):
        if self.frozen:
            self._last_bytes = png_bytes
            return
        now = time.time()
        try:
            interval = int(self.speed_var.get().replace("s", ""))
        except Exception:
            interval = 1
        if not force and now - self._last_update < interval:
            self._last_bytes = png_bytes
            return
        self._last_update = now
        self._last_bytes = png_bytes
        try:
            img = Image.open(io.BytesIO(png_bytes))
            max_w = max(self.image_label.winfo_width(), 1)
            max_h = max(self.image_label.winfo_height(), 1)
            img = ImageOps.fit(
                img, (max_w, max_h), method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            new_w, new_h = img.size
            self._ctk_image = ctk.CTkImage(light_image=img, size=(new_w, new_h))
            self.image_label.configure(image=self._ctk_image, text="")
            self.placeholder.grid_remove()
            self.image_label.grid()
        except Exception:
            pass

    def show_placeholder(self):
        self.placeholder.grid()
        self.image_label.grid_remove()
        self.status_lbl.configure(text="• Aguardando publicação", text_color=T.MUTED)

    def show_active(self, profile_name: str):
        self.status_lbl.configure(
            text=f"• Em execução — {profile_name}", text_color=T.PRIMARY_SOFT)

    def show_done(self):
        self.status_lbl.configure(text="• Publicação finalizada", text_color=T.MUTED)


class ProfilePanel:
    def __init__(self, parent, index: int, cfg):
        self.index = index
        self.name = f"Perfil {index}"
        self.cfg = cfg
        self.folder_var = ctk.StringVar(value=cfg.get(f"profile{index}_folder", ""))
        self.current_videos: list[VideoInfo] = []
        self.video_rows: dict[int, ctk.CTkLabel] = {}
        self.row_frames: dict[int, ctk.CTkFrame] = {}
        self._custom_descriptions: dict[str, str] = {}
        self._custom_hashtags: dict[str, str] = {}
        self.resumable_loaded = False

        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=0)
        self.frame.rowconfigure(1, weight=0)
        self.frame.rowconfigure(2, weight=1)

        self._build_config_card()
        self._build_splitter()
        self._build_queue_card()

        self.desc_box.bind("<KeyRelease>", lambda _e: parent.winfo_toplevel()._validate())
        self.allow_empty_chk.configure(
            command=lambda: parent.winfo_toplevel()._validate()
        )

        last_desc = cfg.get(f"profile{index}_description", "")
        if last_desc:
            self.desc_box.insert("1.0", last_desc)

    def _build_config_card(self):
        card = ctk.CTkFrame(self.frame, fg_color=T.SURF_LOW, border_width=1,
                    border_color=T.BORDER, corner_radius=12)
        card.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 0))
        self.config_card = card

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(8, 0))
        self.config_header = head
        ctk.CTkLabel(head, text="CONFIGURAÇÃO", font=T.label_caps(),
                     text_color=T.PRIMARY_SOFT).pack(side="left")

        chip = ctk.CTkFrame(head, fg_color=T.SURF_HIGH, corner_radius=20,
                            border_width=1, border_color=T.BORDER)
        chip.pack(side="right")
        inner = ctk.CTkFrame(chip, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(inner, text="●", font=T.code(11),
                     text_color=T.PRIMARY_SOFT).pack(side="left", padx=(0, 6))
        self.count_lbl = ctk.CTkLabel(inner, text="Vídeos encontrados: 0",
                                      font=T.code(12), text_color=T.TEXT)
        self.count_lbl.pack(side="left")

        self.resume_slot = ctk.CTkFrame(card, fg_color="transparent", height=0)
        self.resume_slot.pack(fill="x", padx=16, pady=(2, 0))

        ctk.CTkLabel(card, text=f"Pasta dos vídeos — {self.name}",
                     font=T.body(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(4, 2))

        f_row = ctk.CTkFrame(card, fg_color="transparent")
        f_row.pack(fill="x", padx=16)
        f_row.columnconfigure(0, weight=1)
        self.folder_entry = ctk.CTkEntry(
            f_row, textvariable=self.folder_var, state="readonly",
            font=T.code(12), fg_color=T.SURF, border_color=T.OUTLINE,
            corner_radius=8,
        )
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            f_row, text="📂  Selecionar pasta", width=160, height=32,
            font=T.body(), fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=self._select_folder,
        ).grid(row=0, column=1)

        ctk.CTkLabel(card, text="Descrição padrão",
                     font=T.body(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(6, 2))

        self.desc_box = ctk.CTkTextbox(
            card, height=48, wrap="word", font=T.body(),
            fg_color=T.SURF, border_width=1, border_color=T.OUTLINE,
            corner_radius=8,
        )
        self.desc_box.pack(fill="x", padx=16, pady=(0, 6))

        self.allow_empty_chk = ctk.CTkCheckBox(
            card, text="Permitir publicar sem descrição", font=T.body(12),
            fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            border_color=T.OUTLINE,
        )
        self.allow_empty_chk.pack(anchor="w", padx=16, pady=(0, 4))

        self.republish_chk = ctk.CTkCheckBox(
            card, text="Republicar vídeos já publicados", font=T.body(12),
            fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            border_color=T.OUTLINE,
        )
        self.republish_chk.pack(anchor="w", padx=16, pady=(0, 8))
        if self.cfg.get(f"profile{self.index}_republish"):
            self.republish_chk.select()

    def _build_splitter(self):
        self.splitter = ctk.CTkFrame(self.frame, fg_color="transparent",
                                     cursor="sb_v_double_arrow", height=14)
        self.splitter.grid(row=1, column=0, sticky="ew", padx=8)
        self.splitter.rowconfigure(0, weight=1)
        self.splitter.columnconfigure(0, weight=1)

        mid = ctk.CTkFrame(self.splitter, fg_color=T.SURF_HIGH,
                           corner_radius=6, height=22, width=130)
        self.splitter_mid = mid
        mid.grid(row=0, column=0)
        mid.columnconfigure(0, weight=1)

        self.split_balance_lbl = ctk.CTkLabel(
            mid, text="⋮⋮", font=T.code(11), text_color=T.DIM, width=30)
        self.split_balance_lbl.grid(row=0, column=0)

        self.splitter.bind("<ButtonPress-1>", self._splitter_press)
        self.splitter.bind("<B1-Motion>", self._splitter_drag)
        self.splitter.bind("<ButtonRelease-1>", self._splitter_release)
        mid.bind("<ButtonPress-1>", self._splitter_press)
        mid.bind("<B1-Motion>", self._splitter_drag)
        mid.bind("<ButtonRelease-1>", self._splitter_release)

        saved = int(self.cfg.get(f"profile{self.index}_config_height", 0) or 0)
        self._config_override = saved if saved > 0 else None
        self._drag_start_y = None
        self._drag_start_h = 0

    def _current_config_height(self) -> int:
        w = self.config_card.winfo_height()
        return w if w and w > 1 else self._config_override or 0

    def _set_config_height(self, h: int, persist: bool = False):
        natural = self.config_card.winfo_reqheight() or 1
        frame_h = self.frame.winfo_height()
        q_natural = self.queue_card.winfo_reqheight() or 1
        h = max(natural, int(h))
        if frame_h and frame_h > 1 and q_natural and q_natural > 1:
            reserve = max(30, min(q_natural + 90, frame_h - natural - 40))
            h = min(h, frame_h - reserve)
        self._config_override = int(h)
        self.frame.rowconfigure(0, minsize=int(h))
        if persist:
            self.cfg.save()

    def _apply_saved_config_height(self):
        if self._config_override and self._config_override > 0:
            self._set_config_height(self._config_override)

    def _splitter_press(self, event):
        self._drag_start_y = event.y_root
        self._drag_start_h = self._current_config_height()

    def _splitter_drag(self, event):
        if self._drag_start_y is None:
            return
        delta = event.y_root - self._drag_start_y
        self._set_config_height(self._drag_start_h + delta)

    def _splitter_release(self, _event):
        if self._drag_start_y is not None:
            self.cfg.save()
        self._drag_start_y = None

    def _build_queue_card(self):
        card = ctk.CTkFrame(self.frame, fg_color=T.SURF_LOW, border_width=1,
                            border_color=T.BORDER, corner_radius=12)
        card.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.queue_card = card

        head = ctk.CTkFrame(card, fg_color=T.SURF, corner_radius=12)
        head.pack(fill="x", padx=1, pady=(1, 0))
        self.queue_header = head
        head_title = ctk.CTkFrame(head, fg_color="transparent")
        head_title.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(head_title, text="FILA DE VÍDEOS", font=T.label_caps(),
                     text_color=T.MUTED).pack(anchor="w", padx=16, pady=8)
        ctk.CTkButton(
            head, text="✏ Descrições em lote", width=160, height=30,
            font=T.body(12), fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=self._open_bulk_desc_dialog,
        ).pack(side="right", padx=8, pady=5)
        ctk.CTkButton(
            head, text="# Hashtags em lote", width=160, height=30,
            font=T.body(12), fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=self._open_bulk_hashtag_dialog,
        ).pack(side="right", padx=(0, 8), pady=5)

        self.videos_frame = ctk.CTkScrollableFrame(
            card, fg_color=T.BG, corner_radius=0,
        )
        self.videos_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    def show_resume_strip(self, message: str, on_resume, on_discard):
        for w in self.resume_slot.winfo_children():
            w.destroy()
        strip = ctk.CTkFrame(self.resume_slot, fg_color=T.SURF_HIGH,
                             border_width=1, border_color=T.WARN, corner_radius=8)
        strip.pack(fill="x", pady=2)
        ctk.CTkLabel(strip, text=message, font=T.body(11), text_color=T.WARN,
                     justify="left", anchor="w", wraplength=520).pack(
            side="left", padx=10, pady=6, fill="x", expand=True)
        btns = ctk.CTkFrame(strip, fg_color="transparent")
        btns.pack(side="right", padx=8)
        ctk.CTkButton(btns, text="Retomar", width=90, height=26, font=T.body(11),
                      fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
                      corner_radius=6,
                      command=lambda: on_resume()).pack(side="left", padx=3, pady=5)
        ctk.CTkButton(btns, text="Descartar", width=90, height=26, font=T.body(11),
                      fg_color=T.SURF_HIGHEST, hover_color=T.DIM,
                      corner_radius=6,
                      command=lambda: on_discard()).pack(side="left", padx=3, pady=5)

    def hide_resume_strip(self):
        for w in self.resume_slot.winfo_children():
            w.destroy()

    def _select_folder(self):
        log = get_logger()
        try:
            log.info(f"[{self.name}] Botao 'Selecionar pasta' clicado")
            initial = self.folder_var.get() or str(Path.home())
            win = self.frame.winfo_toplevel()
            win.attributes("-topmost", True)
            win.update_idletasks()
            try:
                chosen = filedialog.askdirectory(
                    title=f"Selecione a pasta dos vídeos — {self.name}",
                    initialdir=initial, parent=win,
                )
            finally:
                win.attributes("-topmost", False)
            log.info(f"[{self.name}] Seletor retornou: {chosen!r}")
            if not chosen:
                return
            self.folder_var.set(chosen)
            self.cfg[f"profile{self.index}_folder"] = chosen
            self.cfg.save()
            self.resumable_loaded = False
            self.load_videos_from_folder(chosen)
            win._hide_result_banner()
        except Exception:
            import traceback
            log.error(f"[{self.name}] Erro ao selecionar pasta: {traceback.format_exc()}")
            messagebox.showerror("Erro", "Não foi possível abrir o seletor de pastas.",
                                 parent=self.frame.winfo_toplevel())

    def load_videos_from_folder(self, folder: str):
        vids = discover_videos(Path(folder))
        self.current_videos = vids
        self.refresh_video_list(vids, None)
        self.count_lbl.configure(text=f"Vídeos encontrados: {len(vids)}")
        get_logger().info(f"[{self.name}] Pasta selecionada: {folder} ({len(vids)} vídeos)")
        self.frame.winfo_toplevel()._validate()

    def get_custom_descriptions(self) -> dict[str, str]:
        return dict(self._custom_descriptions)

    def get_custom_hashtags(self) -> dict[str, str]:
        return dict(self._custom_hashtags)

    def _open_desc_dialog(self, video_name: str):
        import tkinter as tk
        win = ctk.CTkToplevel(self.frame.winfo_toplevel())
        win.title(f"Descrição — {video_name}")
        win.geometry("480x600")
        win.resizable(False, False)
        win.configure(fg_color=T.BG)
        win.grab_set()

        ctk.CTkLabel(win, text=f"Descrição para: {video_name}",
                     font=T.label_caps(), text_color=T.TEXT).pack(
            anchor="w", padx=16, pady=(16, 4))

        ctk.CTkLabel(win, text="Descrição individual",
                     font=T.label_caps(), text_color=T.ACCENT_TEXT).pack(
            anchor="w", padx=16, pady=(4, 2))
        ctk.CTkLabel(win, text="Se vazio, a descrição padrão será usada",
                     font=T.body(11), text_color=T.DIM).pack(
            anchor="w", padx=16, pady=(0, 4))

        txt_frame = ctk.CTkFrame(win, fg_color=T.SURF, border_width=1,
                                 border_color=T.OUTLINE, corner_radius=8)
        txt_frame.pack(fill="x", padx=16, pady=(0, 12))

        txt = tk.Text(
            txt_frame, height=5, wrap="word",
            font=(T.FONT_FAMILY, 13),
            bg=T.SURF, fg=T.TEXT, insertbackground=T.TEXT,
            selectbackground=T.PRIMARY, selectforeground=T.ON_PRIMARY,
            relief="flat", bd=0, padx=10, pady=8,
            undo=True,
        )
        txt.pack(fill="x", padx=2, pady=2)

        current = self._custom_descriptions.get(video_name, "")
        if current:
            txt.insert("1.0", current)
            txt.see("1.0")
        txt.focus_set()

        ctk.CTkLabel(win, text="Hashtag deste vídeo (opcional)",
                     font=T.label_caps(), text_color=T.ACCENT_TEXT).pack(
            anchor="w", padx=16, pady=(4, 2))
        ctk.CTkLabel(win, text="Ex.: #kingpanda — vai logo após a descrição",
                     font=T.body(11), text_color=T.DIM).pack(
            anchor="w", padx=16, pady=(0, 4))

        hashline = ctk.CTkEntry(
            win, height=36, font=T.body(13),
            fg_color=T.SURF, text_color=T.TEXT, border_color=T.BORDER,
            corner_radius=8,
        )
        hashline.pack(fill="x", padx=16, pady=(0, 10))
        current_tag = self._custom_hashtags.get(video_name, "")
        if current_tag:
            hashline.insert(0, current_tag)

        ctk.CTkLabel(win, text="Descrição padrão do perfil",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(4, 2))
        ctk.CTkLabel(win, text="(somente leitura — editável na configuração do perfil)",
                     font=T.body(11), text_color=T.DIM).pack(
            anchor="w", padx=16, pady=(0, 4))

        default_desc = self.desc_box.get("1.0", "end-1c").strip()
        default_frame = ctk.CTkFrame(win, fg_color=T.SURF_LOW, border_width=1,
                                     border_color=T.BORDER, corner_radius=8)
        default_frame.pack(fill="x", padx=16, pady=(0, 12))

        default_txt = tk.Text(
            default_frame, height=4, wrap="word",
            font=(T.FONT_FAMILY, 12),
            bg=T.SURF_LOW, fg=T.MUTED, insertbackground=T.MUTED,
            relief="flat", bd=0, padx=10, pady=8,
            state="disabled", cursor="arrow",
        )
        default_txt.pack(fill="x", padx=2, pady=2)
        if default_desc:
            default_txt.configure(state="normal")
            default_txt.insert("1.0", default_desc)
            default_txt.configure(state="disabled")

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 12))

        def _save():
            val = txt.get("1.0", "end-1c").strip()
            if val:
                self._custom_descriptions[video_name] = val
            elif video_name in self._custom_descriptions:
                del self._custom_descriptions[video_name]
            tag = hashline.get().strip()
            if tag:
                self._custom_hashtags[video_name] = tag
            elif video_name in self._custom_hashtags:
                del self._custom_hashtags[video_name]
            win.destroy()
            self.refresh_video_list(self.current_videos)

        def _clear():
            if video_name in self._custom_descriptions:
                del self._custom_descriptions[video_name]
            if video_name in self._custom_hashtags:
                del self._custom_hashtags[video_name]
            win.destroy()
            self.refresh_video_list(self.current_videos)

        ctk.CTkButton(
            btn_row, text="Limpar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=_clear,
        ).pack(side="left")
        ctk.CTkButton(
            btn_row, text="Salvar", width=90, height=30, font=T.body(12),
            fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            corner_radius=8,
            command=_save,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row, text="Cancelar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=win.destroy,
        ).pack(side="right", padx=(0, 8))

    def _extract_titles_from_docs(self, link):
        match = re.search(r"/d/([a-zA-Z0-9_-]+)", link)
        if not match:
            raise ValueError(
                "Link inválido. Cole o link de um documento do Google Docs "
                "(contém /d/SEU_ID/).")
        doc_id = match.group(1)
        url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        resp = requests.get(url, timeout=25)
        if resp.status_code != 200:
            raise ValueError(
                f"Falha ao acessar o documento (HTTP {resp.status_code}). "
                "Confira se o link está correto ou se o documento é público.")
        text = resp.text
        text = re.sub(
            r">>?\s*A\s*PARTIR\s+DAQUI.*?(?=\n\s*\d+\s*[.\-)])",
            "", text, flags=re.IGNORECASE | re.DOTALL,
        )
        titles = []
        next_num = 1
        prev_line = ""

        def _is_sep_or_blank(ln: str) -> bool:
            s = ln.strip()
            if not s:
                return True
            return bool(re.match(r"^[\u2500-\u257F\u2010-\u2027_=\-\s]+$", s))

        for ln in text.splitlines():
            ln_clean = ln.replace("\ufeff", "").strip()
            m = re.match(r"^(\d+)\s*[.\-)]\s*(.+?)\s*$", ln_clean)
            if m and int(m.group(1)) == next_num and _is_sep_or_blank(prev_line):
                title = m.group(2).strip()
                if title:
                    titles.append(title)
                    next_num += 1
            prev_line = ln
        if not titles:
            raise ValueError("Nenhum título numerado encontrado no documento.")
        return titles

    def _extract_roteiros_from_docs(self, link):
        match = re.search(r"/d/([a-zA-Z0-9_-]+)", link)
        if not match:
            raise ValueError(
                "Link inválido. Cole o link de um documento do Google Docs.")
        doc_id = match.group(1)
        url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        resp = requests.get(url, timeout=25)
        if resp.status_code != 200:
            raise ValueError(
                f"Falha ao acessar o documento (HTTP {resp.status_code}).")
        raw = resp.text.replace("\ufeff", "")
        raw = re.sub(
            r">>?\s*A\s*PARTIR\s+DAQUI.*?(?=\n\s*\d+\s*[.\-)])",
            "", raw, flags=re.IGNORECASE | re.DOTALL,
        )

        title_re = re.compile(
            r"^(\d+)\s*[.\-)]\s*(.+?)\s*$"
        )
        blocks = []
        current_num = None
        current_title = None
        current_lines = []
        next_num = 1
        prev_line = ""

        def _is_sep_or_blank(ln: str) -> bool:
            s = ln.strip()
            if not s:
                return True
            return bool(re.match(r"^[\u2500-\u257F\u2010-\u2027_=\-\s]+$", s))

        for line in raw.splitlines():
            stripped = line.strip()
            m = title_re.match(stripped)
            if m and int(m.group(1)) == next_num and _is_sep_or_blank(prev_line):
                if current_num is not None:
                    blocks.append({
                        "number": int(current_num),
                        "title": current_title,
                        "body": "\n".join(current_lines),
                    })
                current_num = m.group(1)
                current_title = m.group(2).strip()
                current_lines = []
                next_num += 1
            elif current_num is not None:
                current_lines.append(line)
            prev_line = line

        if current_num is not None:
            blocks.append({
                "number": int(current_num),
                "title": current_title,
                "body": "\n".join(current_lines),
            })

        known_tags = {"#kingpanda", "#doppa", "#superbet"}
        tag_aliases = {
            "kingpanda": "#kingpanda",
            "king-panda": "#kingpanda",
            "king panda": "#kingpanda",
            "doppa": "#doppa",
            "superbet": "#superbet",
        }
        results = []

        for blk in blocks:
            body = blk["body"]
            low = body.lower()

            instructions = ""
            inst_match = re.search(
                r"Instru[çc][õo]es?\s*:\s*(.*?)(?=\n\s*\n|\nRoteiro|\nFoto|\nCr[ée]ditos)",
                body, re.IGNORECASE | re.DOTALL,
            )
            if inst_match:
                instructions = inst_match.group(1).strip()

            hashtag = None
            for tag in known_tags:
                if tag in instructions.lower() or tag in low:
                    hashtag = tag
                    break
            if not hashtag:
                m_tag = re.search(r"#(\w+)", instructions)
                if m_tag:
                    hashtag = f"#{m_tag.group(1)}"
            if not hashtag:
                for alias, tag in tag_aliases.items():
                    if alias in low:
                        hashtag = tag
                        break

            credits = None
            credit_lines = []
            for ln in body.splitlines():
                m = re.match(r"^\s*([^:]+?)\s*:\s*(.+?)\s*$", ln)
                if m and _is_credit_label(m.group(1)):
                    credit_lines.append(ln.strip())
            if credit_lines:
                credits = "\n".join(credit_lines)

            legend = None
            lg_match = re.search(
                r"Legenda\s+do\s+post[^:]*:\s*(.*?)(?=\n\s*\n|\Z)",
                body, re.IGNORECASE | re.DOTALL,
            )
            if lg_match:
                legend = lg_match.group(1).strip()

            script = None
            roteiro_match = re.search(
                r"Roteiro\s*:\s*\n(.*?)(?=\n\s*Pron[úu]ncia|\n\s*Foto|\n\s*Cr[ée]ditos|\Z)",
                body, re.IGNORECASE | re.DOTALL,
            )
            if roteiro_match:
                script = roteiro_match.group(1).strip()
            else:
                after_inst = body
                if inst_match:
                    after_inst = body[inst_match.end():]
                skip_re = re.compile(
                    r"^\s*(?:Foto\s*:.*|Cr[ée]ditos?\s*:.*|"
                    r"Não\s+precisa\s+da\s+mensagem.*|"
                    r"_{5,})$",
                    re.IGNORECASE | re.MULTILINE,
                )
                cleaned = skip_re.sub("", after_inst)
                parts = re.split(
                    r"\n\s*(?:Pron[úu]ncia|Legenda)",
                    cleaned, maxsplit=1, flags=re.IGNORECASE,
                )
                script = parts[0].strip()

            if script:
                script = re.sub(r"\n{3,}", "\n\n", script)
                script = re.sub(r"\n\s*_{5,}\s*\n", "\n", script)
                script = script.strip()

            desc = blk["title"]
            if credits:
                desc = f"{blk['title']}\n\n{credits}"

            results.append({
                "number": blk["number"],
                "title": blk["title"],
                "description": desc,
                "hashtag": hashtag,
                "credits": credits,
            })

        if not results:
            raise ValueError("Nenhum roteiro encontrado no documento.")
        return results

    def _open_bulk_desc_dialog(self):
        import tkinter as tk
        folder = (self.folder_var.get() or "").strip()
        if not self.current_videos and folder:
            self.load_videos_from_folder(folder)
        win = ctk.CTkToplevel(self.frame.winfo_toplevel())
        win.title(f"Descrições em lote — {self.name}")
        win.geometry("560x600")
        win.configure(fg_color=T.BG)
        win.grab_set()

        ctk.CTkLabel(win, text="DESCRIÇÕES EM LOTE",
                     font=T.label_caps(), text_color=T.ACCENT_TEXT).pack(
            anchor="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            win,
            text="Cada linha abaixo será a legenda individual de um vídeo.\n"
                 "O número do roteiro é extraído do nome do arquivo.\n"
                 f"Ex: roteiro-25.mp4 recebe o roteiro nº 25.\n"
                 f"{len(self.current_videos)} vídeo(s) encontrado(s) nesta pasta.",
            font=T.body(11), text_color=T.DIM, justify="left").pack(
            anchor="w", padx=16, pady=(0, 8))

        link_row = ctk.CTkFrame(win, fg_color="transparent")
        link_row.pack(fill="x", padx=16, pady=(0, 4))

        link_entry = ctk.CTkEntry(
            link_row, height=32, font=T.body(12),
            fg_color=T.SURF, text_color=T.TEXT, border_color=T.BORDER,
            placeholder_text="Cole o link do Google Docs dos roteiros",
        )
        link_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        extract_btn = ctk.CTkButton(
            link_row, text="Extrair Tudo", width=120, height=32,
            font=T.body(12), fg_color=T.PRIMARY,
            hover_color=T.PRIMARY_BRIGHT, corner_radius=8,
        )
        extract_btn.pack(side="right")

        box_frame = ctk.CTkFrame(win, fg_color=T.SURF, border_width=1,
                                 border_color=T.OUTLINE, corner_radius=8)
        box_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        txt = tk.Text(
            box_frame, wrap="word",
            font=(T.MONO_FAMILY, 12),
            bg=T.SURF, fg=T.TEXT, insertbackground=T.TEXT,
            selectbackground=T.PRIMARY, selectforeground=T.ON_PRIMARY,
            relief="flat", bd=0, padx=10, pady=8, undo=True,
        )
        txt.pack(fill="both", expand=True, padx=2, pady=2)
        txt.focus_set()

        info_lbl = ctk.CTkLabel(win, text="", font=T.body(11),
                                text_color=T.PRIMARY_SOFT)
        info_lbl.pack(anchor="w", padx=16, pady=(0, 2))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 12))

        roteiros_data = []

        def _extract():
            link = link_entry.get().strip()
            if not link:
                messagebox.showwarning(
                    "Link vazio", "Cole o link do Google Docs primeiro.",
                    parent=self.frame.winfo_toplevel())
                return
            extract_btn.configure(state="disabled", text="Extraindo...")
            info_lbl.configure(text="Extraindo títulos do documento...",
                               text_color=T.PRIMARY_SOFT)

            def _run():
                res = {"ok": False, "roteiros": [], "err": ""}
                try:
                    res["roteiros"] = self._extract_roteiros_from_docs(link)
                    res["ok"] = True
                except Exception as e:
                    res["err"] = str(e)
                win.after(0, lambda: _done(res))

            def _done(res):
                extract_btn.configure(state="normal", text="Extrair Tudo")
                if not res["ok"]:
                    info_lbl.configure(text=f"✗ {res['err']}", text_color=T.ERROR)
                    return
                roteiros = res["roteiros"]
                nonlocal roteiros_data
                roteiros_data = roteiros
                total = len(self.current_videos)
                txt.delete("1.0", "end")
                descriptions = [r["description"] for r in roteiros]
                txt.insert("1.0", "\n\n---\n\n".join(descriptions))

                tag_counts = {}
                credits_count = 0
                for r in roteiros:
                    t = r.get("hashtag")
                    if t:
                        tag_counts[t] = tag_counts.get(t, 0) + 1
                    if r.get("credits"):
                        credits_count += 1

                parts = [f"✓ {len(roteiros)} título(s) extraído(s)."]
                tag_str = " | ".join(f"{t}: {c}" for t, c in tag_counts.items())
                if tag_str:
                    parts.append(f"Hashtags: {tag_str}")
                if credits_count:
                    parts.append(f"{credits_count} com créditos")
                if len(roteiros) > total:
                    parts.append(
                        f"{len(roteiros) - total} excedem os vídeos e serão ignorados.")
                info_lbl.configure(text="  ".join(parts), text_color=T.PRIMARY_SOFT)

            threading.Thread(target=_run, daemon=True).start()

        extract_btn.configure(command=_extract)

        def _apply():
            total = len(self.current_videos)
            if total == 0:
                messagebox.showwarning(
                    "Sem vídeos", "Selecione a pasta de vídeos primeiro.",
                    parent=self.frame.winfo_toplevel())
                return

            if not roteiros_data:
                link = link_entry.get().strip()
                if link and not txt.get("1.0", "end-1c").strip():
                    try:
                        roteiros_data.extend(self._extract_roteiros_from_docs(link))
                    except Exception as e:
                        messagebox.showwarning(
                            "Erro ao extrair", str(e),
                            parent=self.frame.winfo_toplevel())
                        return

            if roteiros_data:
                self._custom_descriptions.clear()
                self._custom_hashtags.clear()
                video_by_num = {}
                for v in self.current_videos:
                    m = re.search(r"(\d+)", v.name)
                    if m:
                        video_by_num[int(m.group(1))] = v.name
                applied_desc = 0
                applied_tag = 0
                for r in roteiros_data:
                    n = r["number"]
                    name = video_by_num.get(n)
                    if not name:
                        continue
                    if r.get("description"):
                        self._custom_descriptions[name] = r["description"]
                        applied_desc += 1
                    if r.get("hashtag"):
                        self._custom_hashtags[name] = r["hashtag"]
                        applied_tag += 1
                self.refresh_video_list(self.current_videos)
                parts = [f"✓ {applied_desc} legenda(s) aplicada(s)."]
                if applied_tag:
                    parts.append(f"{applied_tag} hashtag(s) aplicada(s).")
                unfound = [r["number"] for r in roteiros_data
                           if not video_by_num.get(r["number"])]
                if unfound:
                    nums = ", ".join(str(x) for x in unfound[:10])
                    parts.append(f"Sem vídeo p/ roteiro(s): {nums}")
                info_lbl.configure(text="  ".join(parts), text_color=T.PRIMARY_SOFT)
                win.destroy()
                return

            raw = txt.get("1.0", "end-1c")
            blocks = re.split(r"\n\s*---\s*\n", raw)
            lines = [b.strip() for b in blocks if b.strip()]
            if not lines:
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                messagebox.showinfo(
                    "Vazio", "Digite ao menos uma descrição.",
                    parent=self.frame.winfo_toplevel())
                return
            used = min(len(lines), total)
            for i, v in enumerate(self.current_videos[:used]):
                self._custom_descriptions[v.name] = lines[i]
            self.refresh_video_list(self.current_videos)
            if len(lines) > total:
                info_lbl.configure(
                    text=f"✓ Aplicadas {used} descrições. "
                         f"{len(lines) - total} linha(s) extras ignoradas.",
                    text_color=T.WARN)
            else:
                info_lbl.configure(
                    text=f"✓ Aplicadas {used} descrição(ões).",
                    text_color=T.PRIMARY_SOFT)
            win.destroy()

        def _clear():
            self._custom_descriptions.clear()
            self.refresh_video_list(self.current_videos)
            txt.delete("1.0", "end")
            roteiros_data.clear()
            info_lbl.configure(text="✓ Descrições e hashtags limpas.",
                               text_color=T.WARN)

        ctk.CTkButton(
            btn_row, text="Limpar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=_clear,
        ).pack(side="left")
        ctk.CTkButton(
            btn_row, text="Aplicar", width=130, height=32, font=T.body(12),
            fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            corner_radius=8,
            command=_apply,
        ).pack(side="right", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Fechar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=win.destroy,
        ).pack(side="right")

    def _open_bulk_hashtag_dialog(self):
        import tkinter as tk
        folder = (self.folder_var.get() or "").strip()
        if not self.current_videos and folder:
            self.load_videos_from_folder(folder)
        total = len(self.current_videos)
        if total == 0:
            messagebox.showwarning(
                "Sem vídeos", "Selecione a pasta de vídeos primeiro.",
                parent=self.frame.winfo_toplevel())
            return

        names = [v.name for v in self.current_videos]

        win = ctk.CTkToplevel(self.frame.winfo_toplevel())
        win.title(f"Hashtags em lote — {self.name}")
        win.geometry("720x660")
        win.configure(fg_color=T.BG)
        win.grab_set()

        ctk.CTkLabel(win, text="HASHTAGS EM LOTE",
                     font=T.label_caps(), text_color=T.ACCENT_TEXT).pack(
            anchor="w", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            win,
            text="Digite uma hashtag por linha nos cards abaixo.\n"
                 "Eles são criados automaticamente ao digitar.\n"
                 "Para mandar um vídeo para a hashtag: arraste o card do número até a linha da hashtag.\n"
                 "Clique 2x rápido em um card já colocado para devolver à área disponível.",
            font=T.body(11), text_color=T.DIM, justify="left").pack(
            anchor="w", padx=16, pady=(0, 8))

        txt = tk.Text(
            win, height=1, wrap="word",
            font=(T.MONO_FAMILY, 12),
            bg=T.SURF, fg=T.TEXT, insertbackground=T.TEXT,
            selectbackground=T.PRIMARY, selectforeground=T.ON_PRIMARY,
            relief="flat", bd=0, padx=10, pady=8, undo=True,
        )
        txt.pack_forget()

        tag_entry = ctk.CTkEntry(
            win, height=36, font=T.body(13),
            fg_color=T.SURF, text_color=T.TEXT, border_color=T.BORDER,
            corner_radius=8,
            placeholder_text="Digite uma hashtag e clique em Confirmar",
        )
        tag_entry.pack(fill="x", padx=16, pady=(0, 8))
        tag_entry.focus_set()

        tag_btn_row = ctk.CTkFrame(win, fg_color="transparent")
        tag_btn_row.pack(fill="x", padx=16, pady=(0, 8))

        def _normalize_tag(raw: str) -> str:
            cleaned = (raw or "").strip()
            if not cleaned:
                return ""
            cleaned = cleaned.strip("#").strip()
            cleaned = re.sub(r"\s+", "", cleaned)
            if not cleaned:
                return ""
            return f"#{cleaned}"

        def _render_confirmed_tags():
            for w in zones_host.winfo_children():
                w.destroy()

            if not zone_order:
                ctk.CTkLabel(
                    zones_host,
                    text="Digite uma hashtag no campo acima e confirme para aparecer aqui.",
                    font=T.body(11), text_color=T.DIM,
                ).pack(anchor="w", padx=10, pady=8)
                return

            for tag in zone_order:
                row = ctk.CTkFrame(zones_host, fg_color=T.SURF_LOW,
                                   border_width=1, border_color=T.BORDER,
                                   corner_radius=8)
                row.zone_tag = tag
                row.pack(fill="x", padx=2, pady=(0, 4))

                head = ctk.CTkFrame(row, fg_color="transparent")
                head.pack(fill="x", padx=12, pady=(6, 0))
                ctk.CTkLabel(head, text=tag, font=T.body(13),
                             text_color=T.ACCENT_TEXT).pack(side="left")
                ctk.CTkLabel(head,
                             text=f"{sum(1 for p, t in assigned.items() if t == tag)} vídeo(s)",
                             font=T.body(11), text_color=T.DIM).pack(side="right")

                body = ctk.CTkFrame(row, fg_color="transparent")
                body.pack(fill="x", padx=10, pady=(4, 8))
                body.zone_tag = tag

                assigned_cards = [
                    pos for pos, t in sorted(
                        ((pos, value) for pos, value in assigned.items()
                         if isinstance(pos, int)),
                        key=lambda item: item[0],
                    )
                    if t == tag
                ]
                if not assigned_cards:
                    ctk.CTkLabel(body, text="Nenhum vídeo atribuído a esta hashtag.",
                                 font=T.body(11), text_color=T.DIM).pack(anchor="w", padx=4, pady=4)
                    continue

                for pos in assigned_cards:
                    _make_card(body, pos, clickback=_unassign)

        def _add_tag_from_input():
            tag = _normalize_tag(tag_entry.get())
            if not tag:
                info_lbl.configure(text="Digite uma hashtag antes de confirmar.",
                                   text_color=T.WARN)
                return
            if tag not in zone_order:
                zone_order.append(tag)
            tag_entry.delete(0, "end")
            info_lbl.configure(text=f"✓ Hashtag confirmada: {tag}",
                               text_color=T.PRIMARY_SOFT)
            _render_confirmed_tags()
            _rebuild()

        ctk.CTkButton(
            tag_btn_row, text="Confirmar hashtag", width=150, height=32,
            font=T.body(12), fg_color=T.PRIMARY,
            hover_color=T.PRIMARY_BRIGHT, corner_radius=8,
            command=_add_tag_from_input,
        ).pack(side="right")
        tag_entry.bind("<Return>", lambda _e: _add_tag_from_input())

        scroll = ctk.CTkScrollableFrame(win, fg_color=T.BG, corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        avail_card = ctk.CTkFrame(scroll, fg_color=T.SURF_LOW, border_width=1,
                                  border_color=T.BORDER, corner_radius=10)
        avail_card.pack(fill="x", padx=2, pady=(0, 6))
        ctk.CTkLabel(avail_card, text="CARDS DE VÍDEOS (disponíveis)",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=12, pady=(8, 0))
        avail_host = ctk.CTkFrame(avail_card, fg_color="transparent")
        avail_host.pack(fill="x", padx=10, pady=(4, 8))

        zones_card = ctk.CTkFrame(scroll, fg_color="transparent")
        zones_card.pack(fill="x", padx=2)
        ctk.CTkLabel(zones_card, text="HASHTAGS",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=12, pady=(6, 2))
        zones_host = ctk.CTkFrame(zones_card, fg_color="transparent")
        zones_host.pack(fill="x", padx=2)

        info_lbl = ctk.CTkLabel(win, text="", font=T.body(11),
                                text_color=T.PRIMARY_SOFT)
        info_lbl.pack(anchor="w", padx=16, pady=(0, 4))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 12))

        assigned = {}
        for i, name in enumerate(names):
            tag = self._custom_hashtags.get(name)
            if tag:
                assigned[i + 1] = tag
        zone_order = []
        for tag in list(dict.fromkeys(assigned.values())):
            if tag not in zone_order:
                zone_order.append(tag)

        ghost = {"win": None, "num": None}

        def _sync_zones():
            _rebuild()

        def _zone_under(x, y):
            node = win.winfo_containing(x, y)
            for _ in range(25):
                if node is None:
                    return None
                tg = getattr(node, "zone_tag", None)
                if tg:
                    return tg
                node = getattr(node, "master", None)
            return None

        def _drag_motion(event):
            g = ghost.get("win")
            if g is not None:
                g.wm_geometry(f"+{event.x_root - 20}+{event.y_root - 14}")

        def _drag_drop(event):
            g = ghost.get("win")
            if g is not None:
                g.destroy()
            ghost["win"] = None
            num = ghost.get("num")
            ghost["num"] = None
            if not isinstance(num, int):
                return
            tag = _zone_under(event.x_root, event.y_root)
            if tag is None:
                return
            assigned[num] = tag
            _rebuild()

        def _start_drag(num, event):
            g = tk.Toplevel(win)
            g.overrideredirect(True)
            g.attributes("-topmost", True)
            lbl = ctk.CTkLabel(g, text=str(num), width=40, height=28,
                               font=T.code(13), fg_color=T.PRIMARY,
                               text_color=T.ON_PRIMARY, corner_radius=6)
            lbl.pack()
            ghost["win"] = g
            ghost["num"] = num
            g.bind("<B1-Motion>", _drag_motion)
            g.bind("<ButtonRelease-1>", _drag_drop)
            _drag_motion(event)
            return "break"

        def _unassign(num):
            assigned.pop(num, None)
            _rebuild()

        def _make_card(parent, num, draggable=True, clickback=None):
            card = ctk.CTkLabel(
                parent, text=str(num), width=40, height=28, corner_radius=6,
                font=T.code(13), fg_color=T.SURF_HIGH, text_color=T.TEXT,
                border_width=1, border_color=T.BORDER, cursor="fleur",
            )
            card.pack(side="left", padx=3, pady=3)
            if draggable:
                card.bind("<ButtonPress-1>", lambda e, n=num: _start_drag(n, e))
            if clickback is not None:
                card.bind("<Double-Button-1>", lambda _e, n=num: clickback(n))
            return card

        def _rebuild():
            for w in avail_host.winfo_children():
                w.destroy()
            for w in zones_host.winfo_children():
                w.destroy()

            for pos in range(1, total + 1):
                if pos not in assigned:
                    _make_card(avail_host, pos)

            _render_confirmed_tags()

        txt.bind("<KeyRelease>", lambda _e: _sync_zones())

        def _apply():
            applied = 0
            for pos, tag in assigned.items():
                self._custom_hashtags[names[pos - 1]] = tag
                applied += 1
            for name in list(self._custom_hashtags):
                pos = next((p for p, n in enumerate(names, 1) if n == name), None)
                if pos is None or pos not in assigned:
                    del self._custom_hashtags[name]
            self.refresh_video_list(self.current_videos)
            info_lbl.configure(
                text=f"✓ Aplicadas {applied} hashtag(s) a {applied} vídeo(s).",
                text_color=T.PRIMARY_SOFT)
            win.destroy()

        def _clear():
            assigned.clear()
            self._custom_hashtags.clear()
            self.refresh_video_list(self.current_videos)
            txt.delete("1.0", "end")
            info_lbl.configure(text="✓ Hashtags individuais removidas.",
                               text_color=T.WARN)
            _rebuild()

        ctk.CTkButton(
            btn_row, text="Limpar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=_clear,
        ).pack(side="left")
        ctk.CTkButton(
            btn_row, text="Aplicar", width=130, height=32, font=T.body(12),
            fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            corner_radius=8,
            command=_apply,
        ).pack(side="right", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Fechar", width=90, height=30, font=T.body(12),
            fg_color=T.SURF_HIGH, hover_color=T.SURF_HIGHEST,
            border_width=1, border_color=T.BORDER, corner_radius=8,
            command=win.destroy,
        ).pack(side="right")

        _rebuild()

    def refresh_video_list(self, vids, statuses=None):
        for w in self.videos_frame.winfo_children():
            w.destroy()
        self.video_rows = {}
        self.row_frames = {}
        if not vids:
            return
        for i, v in enumerate(vids):
            status = (statuses or {}).get(i, PENDING)
            row = ctk.CTkFrame(
                self.videos_frame, fg_color=T.SURF, corner_radius=8,
                border_width=0,
            )
            row.pack(fill="x", padx=2, pady=3)

            left_wrap = ctk.CTkFrame(row, fg_color="transparent")
            left_wrap.pack(side="left", fill="x", expand=True, padx=(10, 0))

            idx_lbl = ctk.CTkLabel(left_wrap, text=f"{i + 1:02d}", width=30,
                                   anchor="e", font=T.code(12), text_color=T.DIM)
            idx_lbl.pack(side="left", padx=(0, 10))

            name_lbl = ctk.CTkLabel(left_wrap, text=v.name, anchor="w",
                                    font=T.body(), text_color=T.TEXT)
            name_lbl.pack(side="left", padx=(0, 8))

            desc_indicator = ""
            if v.name in self._custom_descriptions:
                desc_indicator = "📝"
            tag_indicator = ""
            if v.name in self._custom_hashtags:
                tag_indicator = "#"
            edit_btn = ctk.CTkButton(
                left_wrap, text=f"✏ {desc_indicator}", width=30, height=24,
                font=T.body(11), fg_color="transparent", hover_color=T.SURF_HIGH,
                corner_radius=4, text_color=T.DIM,
                command=lambda n=v.name: self._open_desc_dialog(n),
            )
            edit_btn.pack(side="left", padx=(0, 4))
            if tag_indicator:
                tag_lbl = ctk.CTkLabel(left_wrap, text=tag_indicator,
                                       font=T.code(12), text_color=T.WARN,
                                       width=16)
                tag_lbl.pack(side="left", padx=(0, 4))

            icon_char = T.STATUS_ICONS.get(status, "○")
            color = T.STATUS_COLORS.get(status, T.MUTED)
            icon_lbl = ctk.CTkLabel(row, text=icon_char, font=T.body(13),
                                    text_color=color, width=24)
            icon_lbl.pack(side="right", padx=(8, 4))
            st_lbl = ctk.CTkLabel(row, text=STATUS_LABELS.get(status, status),
                                  font=T.body(12), text_color=color, anchor="e",
                                  width=180)
            st_lbl.pack(side="right", padx=(0, 10))
            self.video_rows[i] = (icon_lbl, st_lbl)
            self.row_frames[i] = row
            self._style_row(i, status)

    def _style_row(self, index: int, status: str):
        row = self.row_frames.get(index)
        if row is None:
            return
        if status in T.ACTIVE_STATUSES:
            row.configure(fg_color=T.SURF_HIGH, border_width=2, border_color=T.PRIMARY)
        else:
            row.configure(fg_color=T.SURF, border_width=0)

    def set_row_status(self, index: int, status: str, error=None):
        pair = self.video_rows.get(index)
        if pair is None:
            return
        icon_lbl, st_lbl = pair
        text = STATUS_LABELS.get(status, status)
        if status == ERROR and error:
            text += "  (" + str(error)[:40] + ")"
        color = T.STATUS_COLORS.get(status, T.MUTED)
        icon = T.STATUS_ICONS.get(status, "○")
        icon_lbl.configure(text=icon, text_color=color)
        st_lbl.configure(text=text, text_color=color)
        self._style_row(index, status)

    def collect(self):
        folder = (self.folder_var.get() or "").strip()
        desc = self.desc_box.get("1.0", "end-1c")
        allow_empty = bool(self.allow_empty_chk.get())
        return folder, desc, allow_empty

    def get_republish(self) -> bool:
        return bool(self.republish_chk.get())

    def apply_state(self, st):
        self.folder_var.set(st.folder)
        self.desc_box.delete("1.0", "end")
        self.desc_box.insert("1.0", st.description or "")
        self.allow_empty_chk.deselect()
        if st.allow_no_description:
            self.allow_empty_chk.select()
        vids = [VideoInfo(name=v.name, path=v.path) for v in st.videos]
        self.current_videos = vids
        self.refresh_video_list(vids, {i: v.status for i, v in enumerate(st.videos)})
        self.count_lbl.configure(text=f"Vídeos encontrados: {len(vids)}")
        self.resumable_loaded = True


class App(ctk.CTk):
    def __init__(self, cfg, gui_log_queue: queue.Queue, state_managers: list):
        super().__init__()
        self.cfg = cfg
        self.gui_log_queue = gui_log_queue
        self.sms: list[StateManager] = state_managers

        self.ui_queue: queue.Queue = queue.Queue()
        self.publisher: Publisher | None = None
        self.worker_thread: threading.Thread | None = None
        self.running = False
        self.paused = False
        self._cancel_all = False
        self._session_stats: dict[str, dict] = {}
        self._active_profiles: list[int] = []
        self._running_profile: str | None = None
        self.panels: list[ProfilePanel] = []
        self.current_profile_index = 1

        self._lock_desired = False
        self._locked_hwnd: int | None = None
        self._manual_active = False
        self._closing = False

        ctk.set_appearance_mode("dark")
        self.title("Instagram Auto Publisher — Dashboard")
        scr_w = self.winfo_screenwidth()
        scr_h = self.winfo_screenheight()
        work_h = scr_h
        try:
            import ctypes

            class _RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]

            _rc = _RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(_rc), 0)  # SPI_GETWORKAREA
            work_h = _rc.b - _rc.t
        except Exception:
            pass
        win_w = min(1200, scr_w - 20)
        win_h = min(920, work_h - 40)
        self.geometry(f"{win_w}x{win_h}+{(scr_w - win_w) // 2}+0")
        self.minsize(min(1080, win_w), min(700, win_h))
        self.configure(fg_color=T.BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._logo_img = None
        logo_path = BASE_DIR / "assets" / "logo.png"
        if logo_path.exists():
            try:
                pil = Image.open(logo_path)
                pil = pil.convert("RGBA")
                pil = ImageOps.fit(pil, (104, 104), method=Image.Resampling.LANCZOS)
                self._logo_img = ctk.CTkImage(light_image=pil, dark_image=pil,
                                               size=(104, 104))
                icon_pil = ImageOps.fit(Image.open(logo_path).convert("RGBA"),
                                        (64, 64), method=Image.Resampling.LANCZOS)
                icon_pil.save(logo_path.parent / "icon.ico", format="ICO")
            except Exception:
                self._logo_img = None

        if self._logo_img:
            try:
                icon_path = str(logo_path.parent / "icon.ico")
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self._build_sidebar()
        self._build_main()

        self.show_profile(1)
        self.update_idletasks()
        self._check_previous_sessions()
        self._update_profile_dots()
        self._validate()
        self.after(90, self._poll_queues)
        self.after(6000, self._silent_update_check)

        msg = wlock.recover_orphans()
        if msg:
            get_logger().warning(msg)

    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, width=SIDEBAR_W, fg_color=T.SURF,
                           corner_radius=0, border_width=0)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)

        head = ctk.CTkFrame(bar, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(6, 4))
        if self._logo_img:
            ctk.CTkLabel(head, image=self._logo_img, text="").pack(fill="x")
            ctk.CTkLabel(head, text="IG AUTO PUBLISHER", font=T.label_caps(10),
                         text_color=T.MUTED, anchor="w").pack(fill="x", pady=(4, 0))
        else:
            ctk.CTkLabel(head, text="IG AUTO PUBLISHER", font=T.headline(17),
                         text_color=T.PRIMARY_SOFT, anchor="w").pack(fill="x")
        self.subtitle_lbl = ctk.CTkLabel(head, text="Dashboard • 2 Perfis",
                                         font=T.label_caps(10),
                                         text_color=T.MUTED, anchor="w")
        self.subtitle_lbl.pack(fill="x", pady=(2, 0))

        sep = ctk.CTkFrame(bar, fg_color=T.BORDER, height=1)
        sep.pack(fill="x", padx=14, pady=(4, 0))

        prof_label = ctk.CTkLabel(bar, text="PERFIS", font=T.label_caps(10),
                                  text_color=T.MUTED, anchor="w")
        prof_label.pack(fill="x", padx=22, pady=(4, 2))

        self.profile_btns: dict[int, ctk.CTkButton] = {}
        for i in (1, 2):
            btn = ctk.CTkButton(
                bar, text=f"○  Perfil {i}", anchor="w", height=36,
                font=T.body(13), fg_color="transparent", hover_color=T.SURF_HIGHEST,
                text_color=T.MUTED, corner_radius=8,
                command=lambda pi=i: self.show_profile(pi),
            )
            btn.pack(fill="x", padx=14, pady=1)
            self.profile_btns[i] = btn

        sep2 = ctk.CTkFrame(bar, fg_color=T.BORDER, height=1)
        sep2.pack(fill="x", padx=14, pady=(8, 0))

        self.start_btn = ctk.CTkButton(
            bar, text="▶   INICIAR PUBLICAÇÃO", height=46,
            font=T.headline(14), fg_color=T.PRIMARY,
            hover_color=T.PRIMARY_BRIGHT, text_color=T.ON_PRIMARY,
            corner_radius=10,
            command=self._start,
        )
        self.start_btn.pack(fill="x", padx=16, pady=(6, 2))

        self.hint_lbl = ctk.CTkLabel(bar, text="", justify="left", anchor="w",
                                     font=T.body(11), wraplength=SIDEBAR_W - 44,
                                     text_color=T.MUTED)
        self.hint_lbl.pack(fill="x", padx=20, pady=(0, 4))

        nav_items = [
            ("🏠   Dashboard", lambda: self.show_profile(self.current_profile_index)),
            ("📺   Monitor", self.show_monitor),
            ("📁   Biblioteca", self._open_library),
            ("📜   Logs", lambda: self._open_folder(LOGS_DIR)),
            ("⚙   Configurações", self._open_settings),
        ]
        for label, cmd in nav_items:
            b = ctk.CTkButton(
                bar, text=label, anchor="w", height=34, font=T.body(13),
                fg_color="transparent", hover_color=T.SURF_HIGHEST,
                text_color=T.MUTED, corner_radius=8,
                command=cmd,
            )
            b.pack(fill="x", padx=14, pady=1)

        self.update_btn = ctk.CTkButton(
            bar, text="⟳   Buscar atualização", anchor="w", height=34,
            font=T.body(13), fg_color="transparent", hover_color=T.SURF_HIGHEST,
            text_color=T.MUTED, corner_radius=8,
            command=self._check_update,
        )
        self.update_btn.pack(fill="x", padx=14, pady=1)

        footer_sep = ctk.CTkFrame(bar, fg_color=T.BORDER, height=1)
        footer_sep.pack(fill="x", padx=14, pady=(12, 6), side="bottom")
        exit_btn = ctk.CTkButton(
            bar, text="✕   Sair", anchor="w", height=34, font=T.body(13),
            fg_color="transparent", hover_color=T.ERROR_DEEP,
            text_color=T.MUTED, corner_radius=8,
            command=self._on_close,
        )
        exit_btn.pack(fill="x", padx=14, pady=6, side="bottom")

    def _check_update(self):
        if getattr(self, "_update_busy", False):
            return
        self._update_busy = True
        self.update_btn.configure(state="disabled", text="⟳   Verificando...")
        log = get_logger()
        log.info("Verificando atualizacao no GitHub...")

        def _done(res):
            self.update_btn.configure(state="normal",
                                       text="⟳   Buscar atualização")
            self._update_busy = False
            if res.get("err"):
                log.error(f"Falha ao verificar atualizacao: {res['err']}")
                messagebox.showerror(
                    "Atualização",
                    f"Não foi possível verificar agora:\n{res['err']}",
                    parent=self)
                return
            info = res.get("upd")
            if not info:
                log.info(f"Voce ja esta na ultima versao (v{APP_VERSION}).")
                messagebox.showinfo(
                    "Atualização",
                    f"Você já está na última versão (v{APP_VERSION}).",
                    parent=self)
                return
            notes = (info.get("notes") or "").strip()
            if len(notes) > 600:
                notes = notes[:600] + "..."
            ok = messagebox.askyesno(
                "Atualização disponível",
                f"Nova versão: v{info['version']}  (atual: v{APP_VERSION})\n\n"
                f"{notes}\n\n"
                "Baixar e instalar agora?\n"
                "O programa vai reiniciar ao final.",
                parent=self)
            if not ok:
                log.info("Atualizacao adiada pelo usuario.")
                return
            self._apply_update(info)

        def _run():
            res = {}
            try:
                res["upd"] = upd.check_for_update()
            except Exception as e:
                res["err"] = str(e)
            self.after(0, lambda: _done(res))

        threading.Thread(target=_run, daemon=True).start()

    def _apply_update(self, info: dict):
        log = get_logger()
        log.info(f"Baixando atualizacao v{info['version']}...")
        self.update_btn.configure(state="disabled", text="⟳   Baixando 0%")

        def _prog(frac):
            if frac is not None:
                pct = int(frac * 100)
                self.after(0, lambda p=pct: self.update_btn.configure(
                    text=f"⟳   Baixando {p}%"))

        def _done(res):
            self.update_btn.configure(state="normal",
                                       text="⟳   Buscar atualização")
            self._update_busy = False
            if res.get("err"):
                log.error(f"Falha na atualizacao: {res['err']}")
                messagebox.showerror(
                    "Atualização",
                    f"Falha ao instalar a atualização:\n{res['err']}",
                    parent=self)
                return
            log.info(f"Instalado! Reiniciando na versao v{info['version']}...")
            messagebox.showinfo(
                "Atualização",
                f"Atualizado para v{info['version']}!\n\n"
                "O programa vai reiniciar agora.",
                parent=self)
            upd.relaunch()

        def _run():
            res = {}
            try:
                zip_path = upd.download_update(info["url"], progress=_prog)
                upd.apply_update(zip_path)
            except Exception as e:
                res["err"] = str(e)
            self.after(0, lambda: _done(res))

        threading.Thread(target=_run, daemon=True).start()

    def _silent_update_check(self):
        if getattr(self, "_update_busy", False) or self._closing:
            return

        def _run():
            try:
                info = upd.check_for_update()
            except Exception:
                return
            if info:
                get_logger().info(
                    f"Nova versao disponivel: v{info['version']} — "
                    "clique em 'Buscar atualizacao' na barra lateral.")

        threading.Thread(target=_run, daemon=True).start()

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(side="right", fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(22, 10))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(title_box, text="Main Dashboard", font=T.headline(26),
                     text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(title_box, text="Configure e acompanhe sua fila de publicação.",
                     font=T.body(14), text_color=T.MUTED, anchor="w").pack(fill="x")

        body_row = ctk.CTkFrame(main, fg_color="transparent")
        body_row.pack(fill="both", expand=True, padx=(28, 0), pady=(0, 16))

        left_col = ctk.CTkFrame(body_row, fg_color="transparent")
        left_col.pack(side="left", fill="both", expand=True)
        left_col.columnconfigure(0, weight=1)

        self.panel_host = ctk.CTkFrame(left_col, fg_color="transparent")
        self.panel_host.pack(fill="both", expand=True)
        self.panel_host.columnconfigure(0, weight=1)
        self.panel_host.rowconfigure(0, weight=1)

        for i in (1, 2):
            panel = ProfilePanel(self.panel_host, i, self.cfg)
            panel.frame.grid(row=0, column=0, sticky="nsew")
            panel.frame.grid_remove()
            self.panels.append(panel)

        self.monitor = MonitorPanel(self.panel_host)
        self.monitor.frame.grid(row=0, column=0, sticky="nsew")
        self.monitor.frame.grid_remove()
        self._monitoring = False

        right_col = ctk.CTkFrame(body_row, fg_color="transparent", width=RIGHT_COL_W)
        right_col.pack(side="right", fill="y", padx=(0, 20))
        right_col.pack_propagate(False)
        right_col.columnconfigure(0, weight=1)

        self.result_frame = ctk.CTkFrame(right_col, fg_color=T.SURF_LOW,
                                         border_width=1, border_color=T.ERROR,
                                         corner_radius=12)
        res_msg_box = ctk.CTkFrame(self.result_frame, fg_color="transparent")
        res_msg_box.pack(fill="x", padx=14, pady=(10, 4))
        self.result_msg = ctk.CTkLabel(res_msg_box, text="", anchor="w",
                                       justify="left", font=T.body(12),
                                       text_color=T.ERROR)
        self.result_msg.pack(fill="x")
        res_btns = ctk.CTkFrame(self.result_frame, fg_color="transparent")
        res_btns.pack(fill="x", padx=10, pady=(2, 10))
        ctk.CTkButton(res_btns, text="↻ Tentar novamente falhas", height=30,
                      font=T.body(12), fg_color=T.PRIMARY,
                      hover_color=T.PRIMARY_BRIGHT, corner_radius=8,
                      command=self._retry_failed).pack(side="left", padx=4,
                                                       fill="x", expand=True)
        ctk.CTkButton(res_btns, text="Ignorar falhas", height=30, font=T.body(12),
                      fg_color=T.SURF_HIGHEST, hover_color=T.DIM,
                      corner_radius=8,
                      command=self._skip_failed).pack(side="left", padx=4,
                                                      fill="x", expand=True)

        actions_card = ctk.CTkFrame(right_col, fg_color=T.SURF, border_width=1,
                                    border_color=T.BORDER, corner_radius=12)
        self.actions_card = actions_card
        actions_card.pack(fill="x", pady=(0, 12))

        act_head = ctk.CTkFrame(actions_card, fg_color="transparent")
        act_head.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(act_head, text="CONTROLE", font=T.label_caps(),
                     text_color=T.PRIMARY_SOFT).pack(side="left")

        btn_row = ctk.CTkFrame(actions_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        self.pause_btn = ctk.CTkButton(
            btn_row, text="⏸  PAUSAR", height=38, font=T.headline(12),
            state="disabled", command=self._toggle_pause,
            fg_color=T.SURF_HIGHEST, hover_color=T.DIM, corner_radius=8,
        )
        self.pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.cancel_btn = ctk.CTkButton(
            btn_row, text="✕  CANCELAR", height=38, font=T.headline(12),
            state="disabled", command=self._cancel,
            fg_color="#7a1220", hover_color=T.ERROR_DEEP, corner_radius=8,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self.lock_btn = ctk.CTkButton(
            actions_card, text="🔓  Travamento do navegador", height=32,
            font=T.body(12), command=self._toggle_browser_lock,
            fg_color=T.SURF_HIGHEST, hover_color=T.DIM, corner_radius=8,
        )
        self.lock_btn.pack(fill="x", padx=12, pady=(0, 10))

        prog_wrap = ctk.CTkFrame(actions_card, fg_color=T.BG, corner_radius=8,
                                 border_width=1, border_color=T.BORDER)
        prog_wrap.pack(fill="x", padx=12, pady=(0, 12))
        p_top = ctk.CTkFrame(prog_wrap, fg_color="transparent")
        p_top.pack(fill="x", padx=12, pady=(10, 4))
        self.progress_lbl = ctk.CTkLabel(p_top, text="PROGRESSO",
                                         font=T.label_caps(10), text_color=T.MUTED)
        self.progress_lbl.pack(side="left")
        self.pct_lbl = ctk.CTkLabel(p_top, text="0 / 0 • 0%", font=T.code(12),
                                    text_color=T.TEXT)
        self.pct_lbl.pack(side="right")
        self.progress_bar = ctk.CTkProgressBar(
            prog_wrap, height=8, corner_radius=4,
            progress_color=T.PRIMARY, fg_color=T.SURF_HIGHEST,
        )
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=12, pady=(2, 8))

        info_lines = ctk.CTkFrame(prog_wrap, fg_color="transparent")
        info_lines.pack(fill="x", padx=12, pady=(0, 10))
        self.current_lbl = ctk.CTkLabel(info_lines, text="▶  Vídeo atual: -",
                                        font=T.code(11), text_color=T.TEXT,
                                        anchor="w")
        self.current_lbl.pack(fill="x")
        self.stage_lbl = ctk.CTkLabel(info_lines, text="↻  Status: -",
                                      font=T.code(11), text_color=T.ACCENT_TEXT,
                                      anchor="w")
        self.stage_lbl.pack(fill="x")
        self.manual_lbl = ctk.CTkLabel(info_lines, text="", text_color=T.WARN,
                                       font=T.body(11), wraplength=RIGHT_COL_W - 60,
                                       justify="left", anchor="w")
        self.manual_lbl.pack(fill="x", pady=(4, 0))

        console_card = ctk.CTkFrame(right_col, fg_color=T.SURF_LOWEST,
                                    border_width=1, border_color=T.BORDER,
                                    corner_radius=12)
        console_card.pack(fill="both", expand=True)
        console_card.rowconfigure(1, weight=1)
        console_card.columnconfigure(0, weight=1)

        cons_head = ctk.CTkFrame(console_card, fg_color=T.SURF, corner_radius=12)
        cons_head.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        ctk.CTkLabel(cons_head, text="CONSOLE", font=T.label_caps(),
                     text_color=T.MUTED).pack(side="left", padx=14, pady=8)
        ctk.CTkButton(cons_head, text="🧹 Limpar", width=70, height=22,
                      font=T.body(10), fg_color="transparent",
                      hover_color=T.SURF_HIGHEST, text_color=T.MUTED,
                      corner_radius=6, command=self._clear_console).pack(
            side="right", padx=10, pady=5)

        self.log_box = ctk.CTkTextbox(
            console_card, wrap="none", font=T.code(11),
            fg_color=T.SURF_LOWEST, border_width=0, corner_radius=0,
            text_color=T.MUTED,
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 8))
        self.log_box.configure(state="disabled")

    def _open_folder(self, path: Path):
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            get_logger().error(f"Falha ao abrir pasta {path}: {e}")

    def _open_library(self):
        win = ctk.CTkToplevel(self)
        win.title("Biblioteca — distribuir vídeos")
        win.geometry("620x440")
        win.configure(fg_color=T.BG)
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(
            win, text="DISTRIBUIR VÍDEOS POR NÚMERO",
            font=T.headline(19), text_color=T.PRIMARY_SOFT,
        ).pack(anchor="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            win,
            text="Escolha a pasta e informe os números dos vídeos para cada perfil.",
            font=T.body(12), text_color=T.MUTED,
        ).pack(anchor="w", padx=18, pady=(0, 14))

        folder_var = ctk.StringVar(
            value=self.panels[self.current_profile_index - 1].folder_var.get()
        )
        folder_row = ctk.CTkFrame(win, fg_color="transparent")
        folder_row.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkEntry(
            folder_row, textvariable=folder_var, height=36, font=T.code(12),
            fg_color=T.SURF, border_color=T.BORDER,
            placeholder_text="Pasta com os vídeos",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        def choose_folder():
            chosen = filedialog.askdirectory(
                title="Selecione a pasta com os vídeos", parent=win,
                initialdir=folder_var.get() or str(Path.home()),
            )
            if chosen:
                folder_var.set(chosen)
                refresh_preview()

        ctk.CTkButton(
            folder_row, text="📁 Escolher", width=110, height=36,
            font=T.body(12), fg_color=T.SURF_HIGH,
            hover_color=T.SURF_HIGHEST, command=choose_folder,
        ).pack(side="right")

        ctk.CTkLabel(
            win, text="Números destinados a cada perfil",
            font=T.label_caps(), text_color=T.ACCENT_TEXT,
        ).pack(anchor="w", padx=18, pady=(0, 5))
        fields = {}
        for index in (1, 2):
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=4)
            ctk.CTkLabel(
                row, text=f"Perfil {index}", width=90, anchor="w",
                font=T.body(13), text_color=T.TEXT,
            ).pack(side="left")
            entry = ctk.CTkEntry(
                row, height=34, font=T.code(12), fg_color=T.SURF,
                border_color=T.BORDER,
                placeholder_text="Ex.: 1, 2, 4, 10, 25",
            )
            entry.pack(side="left", fill="x", expand=True)
            fields[index] = entry

        preview = ctk.CTkLabel(
            win, text="", justify="left", anchor="w", font=T.body(11),
            text_color=T.DIM, wraplength=570,
        )
        preview.pack(fill="x", padx=18, pady=(12, 4))

        def parse_numbers(value: str) -> list[int]:
            tokens = re.split(r"[,;\s]+", value.strip())
            numbers = []
            for token in tokens:
                if not token:
                    continue
                if not token.isdigit():
                    raise ValueError(f"Número inválido: {token}")
                number = int(token)
                if number <= 0 or number in numbers:
                    raise ValueError(f"Número repetido ou inválido: {token}")
                numbers.append(number)
            return numbers

        def resolve_assignments():
            folder = Path(folder_var.get().strip())
            if not folder.is_dir():
                raise ValueError("Escolha uma pasta válida.")
            videos = discover_videos(folder)
            by_number: dict[int, list[VideoInfo]] = {}
            for video in videos:
                for match in re.findall(r"(?<!\d)(\d+)(?!\d)", Path(video.name).stem):
                    by_number.setdefault(int(match), []).append(video)
            result = {}
            used = set()
            for index in (1, 2):
                selected = []
                for number in parse_numbers(fields[index].get()):
                    matches = by_number.get(number, [])
                    if not matches:
                        raise ValueError(f"O vídeo com número {number} não foi encontrado.")
                    if len(matches) > 1:
                        raise ValueError(f"O número {number} aparece em mais de um vídeo.")
                    video = matches[0]
                    if video.name in used:
                        raise ValueError(f"O vídeo {video.name} foi atribuído aos dois perfis.")
                    used.add(video.name)
                    selected.append(video)
                result[index] = selected
            return folder, result

        def refresh_preview():
            folder = Path(folder_var.get().strip())
            total = len(discover_videos(folder)) if folder.is_dir() else 0
            preview.configure(text=f"{total} vídeo(s) encontrados na pasta.")

        def apply_distribution():
            try:
                folder, assignments = resolve_assignments()
            except ValueError as exc:
                preview.configure(text=f"⚠ {exc}", text_color=T.ERROR)
                return
            for index, videos in assignments.items():
                panel = self.panels[index - 1]
                assigned_folder = str(folder) if videos else ""
                panel.folder_var.set(assigned_folder)
                panel.current_videos = videos
                panel.resumable_loaded = False
                panel.refresh_video_list(videos, None)
                panel.count_lbl.configure(text=f"Vídeos selecionados: {len(videos)}")
                self.cfg[f"profile{index}_folder"] = assigned_folder
            self.cfg.save()
            get_logger().info(
                f"Biblioteca distribuída: Perfil 1 ({len(assignments[1])}), "
                f"Perfil 2 ({len(assignments[2])})"
            )
            win.destroy()
            self._validate()

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(12, 16))
        ctk.CTkButton(
            buttons, text="Aplicar distribuição", height=36,
            font=T.headline(13), fg_color=T.PRIMARY,
            hover_color=T.PRIMARY_BRIGHT, command=apply_distribution,
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="Cancelar", width=100, height=36,
            font=T.body(12), fg_color=T.SURF_HIGH,
            hover_color=T.SURF_HIGHEST, command=win.destroy,
        ).pack(side="right", padx=(0, 8))
        refresh_preview()

    def _open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Configurações")
        win.geometry("420x680")
        win.configure(fg_color=T.BG)
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        win.attributes("-topmost", True)
        win.after(10, lambda: win.attributes("-topmost", False))

        ctk.CTkLabel(win, text="CONFIGURAÇÕES", font=T.headline(20),
                     text_color=T.PRIMARY_SOFT).pack(pady=(18, 6))
        ctk.CTkLabel(win, text="Ajustes gerais do publicador",
                     font=T.body(13), text_color=T.MUTED).pack(pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                        scrollbar_fg_color=T.SURF,
                                        scrollbar_button_color=T.BORDER,
                                        scrollbar_button_hover_color=T.DIM)
        scroll.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        frame = ctk.CTkFrame(scroll, fg_color=T.SURF_LOW, border_width=1,
                             border_color=T.BORDER, corner_radius=12)
        frame.pack(fill="x", padx=16)

        ctk.CTkLabel(frame, text="Tentativas por vídeo (retry)",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(12, 2))
        ctk.CTkLabel(frame, text="Quantas vezes tentar postar novamente um vídeo antes de pular para o próximo.",
                     font=T.body(11), text_color=T.DIM, wraplength=330).pack(
            anchor="w", padx=16, pady=(0, 6))

        attempts_var = ctk.IntVar(value=int(self.cfg.max_attempts))

        slider_row = ctk.CTkFrame(frame, fg_color="transparent")
        slider_row.pack(fill="x", padx=16, pady=(0, 4))
        slider = ctk.CTkSlider(
            slider_row, from_=1, to=10, number_of_steps=9,
            variable=attempts_var, width=220,
            progress_color=T.PRIMARY, button_color=T.PRIMARY,
        )
        slider.pack(side="left", padx=(0, 12))
        val_lbl = ctk.CTkLabel(slider_row, textvariable=attempts_var,
                               font=T.code(16), text_color=T.TEXT, width=30)
        val_lbl.pack(side="left")

        ctk.CTkLabel(frame, text=" minutos entre vídeos (pausa entre posts)",
                     font=T.body(11), text_color=T.DIM).pack(
            anchor="w", padx=16, pady=(6, 2))
        delay_var = ctk.DoubleVar(value=float(self.cfg.delay_between_videos_s / 60))
        delay_row = ctk.CTkFrame(frame, fg_color="transparent")
        delay_row.pack(fill="x", padx=16, pady=(0, 12))
        delay_slider = ctk.CTkSlider(
            delay_row, from_=0, to=30, number_of_steps=30,
            variable=delay_var, width=220,
            progress_color=T.ACCENT_TEXT, button_color=T.ACCENT_TEXT,
        )
        delay_slider.pack(side="left", padx=(0, 12))
        delay_lbl = ctk.CTkLabel(delay_row, textvariable=delay_var,
                                 font=T.code(14), text_color=T.TEXT, width=40)
        delay_lbl.pack(side="left")

        sep2 = ctk.CTkFrame(frame, fg_color=T.BORDER, height=1)
        sep2.pack(fill="x", padx=16, pady=(8, 8))

        ctk.CTkLabel(frame, text="VELOCIDADE DA AUTOMAÇÃO",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(0, 2))
        ctk.CTkLabel(frame,
                     text="Quanto maior a velocidade, mais rápido o programa\n"
                          "age. 'Humano' insere pausas naturais para se\n"
                          "parecer com uma pessoa real.",
                     font=T.body(11), text_color=T.DIM, justify="left").pack(
            anchor="w", padx=16, pady=(0, 6))

        speed_var = ctk.StringVar(value=self.cfg.get("autopilot_speed", "normal"))
        speed_frame = ctk.CTkFrame(frame, fg_color="transparent")
        speed_frame.pack(fill="x", padx=16, pady=(0, 12))

        for val, label, desc in [
            ("rapido", "⚡ Rápido", "mínimas pausas"),
            ("normal", "◈ Normal", "pausas moderadas"),
            ("humano", "👤 Humano", "pausas realistas"),
        ]:
            row = ctk.CTkFrame(speed_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkRadioButton(
                row, text=label, variable=speed_var, value=val,
                font=T.body(12), fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            ).pack(side="left")
            ctk.CTkLabel(row, text=desc, font=T.body(11),
                         text_color=T.DIM).pack(side="left", padx=(10, 0))

        sep_speed = ctk.CTkFrame(frame, fg_color=T.BORDER, height=1)
        sep_speed.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(frame, text="TAMANHO DA JANELA DO CHROME",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(0, 2))
        ctk.CTkLabel(frame, text="Menor que a tela para caber tudo sem dar zoom.",
                     font=T.body(11), text_color=T.DIM, wraplength=330).pack(
            anchor="w", padx=16, pady=(0, 6))

        win_row = ctk.CTkFrame(frame, fg_color="transparent")
        win_row.pack(fill="x", padx=16, pady=(0, 12))
        width_var = ctk.IntVar(value=int(self.cfg.viewport_width))
        height_var = ctk.IntVar(value=int(self.cfg.viewport_height))
        for label, var, lo, hi in [("Largura", width_var, 800, 1920),
                                   ("Altura", height_var, 500, 1080)]:
            col = ctk.CTkFrame(win_row, fg_color=T.SURF, border_width=1,
                               border_color=T.BORDER, corner_radius=8)
            col.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ctk.CTkLabel(col, text=label, font=T.label_caps(10),
                         text_color=T.DIM).pack(padx=8, pady=(6, 0))
            ctk.CTkEntry(col, textvariable=var, width=80, height=32,
                         font=T.code(14), fg_color="transparent",
                         border_width=0, justify="center").pack(padx=8, pady=(0, 6))

        ctk.CTkLabel(frame, text="PERFIS DE PUBLICAÇÃO",
                     font=T.label_caps(), text_color=T.MUTED).pack(
            anchor="w", padx=16, pady=(0, 2))
        ctk.CTkLabel(frame, text="Escolha em quais perfis o programa vai postar.",
                     font=T.body(11), text_color=T.DIM, wraplength=330).pack(
            anchor="w", padx=16, pady=(0, 6))

        publish_var = ctk.StringVar(value=self.cfg.get("publish_mode", "both"))
        mode_frame = ctk.CTkFrame(frame, fg_color="transparent")
        mode_frame.pack(fill="x", padx=16, pady=(0, 12))

        for val, label in [("both", "Ambos os perfis"),
                           ("profile1", "Apenas Perfil 1"),
                           ("profile2", "Apenas Perfil 2")]:
            ctk.CTkRadioButton(
                mode_frame, text=label, variable=publish_var, value=val,
                font=T.body(12), fg_color=T.PRIMARY, hover_color=T.PRIMARY_BRIGHT,
            ).pack(anchor="w", pady=2)

        sep = ctk.CTkFrame(frame, fg_color=T.BORDER, height=1)
        sep.pack(fill="x", padx=16, pady=(8, 8))

        lock_head = ctk.CTkFrame(frame, fg_color="transparent")
        lock_head.pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkLabel(lock_head, text="TRAVAMENTO DO NAVEGADOR",
                     font=T.label_caps(), text_color=T.MUTED).pack(side="left")
        lock_state_lbl = ctk.CTkLabel(
            lock_head,
            text="ATIVO" if self._lock_desired else "INATIVO",
            font=T.label_caps(10),
            text_color=T.ERROR if self._lock_desired else T.PRIMARY_SOFT,
        )
        lock_state_lbl.pack(side="right")

        ctk.CTkLabel(
            frame,
            text="Bloqueia mouse/teclado no Chrome durante publicação.\n"
                 "Útil para evitar cliques acidentais.",
            font=T.body(11), text_color=T.DIM, wraplength=330, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        lock_btn_dialog = ctk.CTkButton(
            frame, text=("🔒  DESATIVAR trava" if self._lock_desired
                         else "🔓  ATIVAR trava"),
            height=34, font=T.body(12),
            fg_color=T.ERROR_DEEP if self._lock_desired else T.SURF_HIGH,
            hover_color=T.ERROR if self._lock_desired else T.SURF_HIGHEST,
            corner_radius=8,
        )
        lock_btn_dialog.pack(fill="x", padx=16, pady=(0, 14))

        def _toggle_lock_from_settings():
            self._toggle_browser_lock()
            lock_btn_dialog.configure(
                text=("🔒  DESATIVAR trava" if self._lock_desired
                      else "🔓  ATIVAR trava"),
                fg_color=T.ERROR_DEEP if self._lock_desired else T.SURF_HIGH,
                hover_color=T.ERROR if self._lock_desired else T.SURF_HIGHEST,
            )
            lock_state_lbl.configure(
                text="ATIVO" if self._lock_desired else "INATIVO",
                text_color=T.ERROR if self._lock_desired else T.PRIMARY_SOFT,
            )

        lock_btn_dialog.configure(command=_toggle_lock_from_settings)

        def _save():
            self.cfg["max_attempts"] = int(attempts_var.get())
            self.cfg["delay_between_videos_s"] = round(float(delay_var.get()) * 60, 1)
            self.cfg["publish_mode"] = publish_var.get()
            self.cfg["autopilot_speed"] = speed_var.get()
            self.cfg["viewport_width"] = max(800, min(1920, int(width_var.get())))
            self.cfg["viewport_height"] = max(500, min(1080, int(height_var.get())))
            self.cfg.save()
            get_logger().info(
                f"Configurações atualizadas: tentativas={self.cfg.max_attempts}, "
                f"pausa_entre_videos={self.cfg.delay_between_videos_s}s "
                f"({self.cfg.delay_between_videos_s / 60:.1f} min), "
                f"perfis={self.cfg.publish_mode}, "
                f"velocidade={self.cfg.autopilot_speed}, "
                f"janela={self.cfg.viewport_width}x{self.cfg.viewport_height}"
            )
            self._refresh_profile_button_styles()
            win.destroy()

        btn_row = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(12, 8))
        ctk.CTkButton(btn_row, text="💾  Salvar", height=38,
                      font=T.headline(13), fg_color=T.PRIMARY,
                      hover_color=T.PRIMARY_BRIGHT, corner_radius=10,
                      command=_save).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(btn_row, text="Cancelar", height=38,
                      font=T.headline(13), fg_color=T.SURF_HIGHEST,
                      hover_color=T.DIM, corner_radius=10,
                      command=win.destroy).pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _clear_console(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def show_profile(self, index: int):
        self._monitoring = False
        self.current_profile_index = index
        self.monitor.frame.grid_remove()
        for p in self.panels:
            p.frame.grid_remove()
        target = self.panels[index - 1]
        target.frame.grid()
        self.update_idletasks()
        target._apply_saved_config_height()
        self._refresh_profile_button_styles()

    def show_monitor(self):
        self._monitoring = True
        for p in self.panels:
            p.frame.grid_remove()
        self.monitor.frame.grid()
        self.monitor.show_active(self._running_profile or "Navegador")
        if getattr(self.monitor, "_last_bytes", None):
            self.monitor._update_image(self.monitor._last_bytes)
        self.update_idletasks()
        self._refresh_profile_button_styles()

    def _show_dashboard(self):
        self.show_profile(self.current_profile_index)

    def _refresh_profile_button_styles(self):
        mode = self.cfg.get("publish_mode", "both")
        mode_labels = {"both": "2 Perfis", "profile1": "Perfil 1", "profile2": "Perfil 2"}
        self.subtitle_lbl.configure(text=f"Dashboard • {mode_labels.get(mode, '2 Perfis')}")
        for i, btn in self.profile_btns.items():
            is_disabled = (mode == "profile1" and i == 2) or (mode == "profile2" and i == 1)
            if is_disabled:
                btn.configure(state="disabled", fg_color=T.SURF,
                              text_color="#555566", hover_color=T.SURF,
                              text=f"○  Perfil {i}  (inativo)")
                continue
            dot = "●" if (self.running and self._running_profile == f"Perfil {i}") else "○"
            base_text = self._profile_btn_caption(i, dot)
            if i == self.current_profile_index:
                btn.configure(state="normal", fg_color=T.PRIMARY, text_color=T.ON_PRIMARY,
                              hover_color=T.PRIMARY_BRIGHT, text=base_text)
            else:
                btn.configure(state="normal", fg_color="transparent", text_color=T.MUTED,
                              hover_color=T.SURF_HIGHEST, text=base_text)

    def _profile_btn_caption(self, index: int, dot: str) -> str:
        suffix = ""
        sm = self.sms[index - 1]
        st = sm.state
        if st is not None and not self.running:
            pub, tot = st.published_count(), len(st.videos)
            if tot > 0:
                suffix = f"   {pub}/{tot}"
        return f"{dot}  Perfil {index}{suffix}"

    def _update_profile_dots(self):
        self._refresh_profile_button_styles()

    def _panel_by_name(self, name: str) -> ProfilePanel | None:
        for p in self.panels:
            if p.name == name:
                return p
        return None

    def _check_previous_sessions(self):
        for i, sm in enumerate(self.sms, start=1):
            st = sm.load()
            if st and sm.is_resumable():
                panel = self.panels[i - 1]
                msg = (f"Sessão anterior: {st.published_count()}/{len(st.videos)} "
                       f"publicados • Pendentes: {st.pending_count()} • "
                       f"Pasta: {Path(st.folder).name}")
                panel.show_resume_strip(
                    msg,
                    on_resume=lambda pi=i: self._apply_resume(pi),
                    on_discard=lambda sm_=sm, pi=i: self._discard_resume(sm_, pi),
                )

    def _apply_resume(self, profile_index: int):
        panel = self.panels[profile_index - 1]
        sm = self.sms[profile_index - 1]
        st = sm.state
        if not st:
            return
        self.show_profile(profile_index)
        panel.apply_state(st)
        panel.hide_resume_strip()
        get_logger().info(f"[{panel.name}] Sessão anterior carregada para retomada")
        self._validate()

    def _discard_resume(self, sm: StateManager, profile_index: int):
        sm.discard()
        self.panels[profile_index - 1].hide_resume_strip()
        get_logger().info(f"[Perfil {profile_index}] Sessão anterior descartada")
        self._update_profile_dots()

    def _validation_error(self):
        errors = []
        active = 0
        publish_mode = self.cfg.get("publish_mode", "both")
        for i, panel in enumerate(self.panels):
            if publish_mode == "profile1" and i == 1:
                continue
            if publish_mode == "profile2" and i == 0:
                continue
            folder, desc, allow_empty = panel.collect()
            if not folder:
                continue
            if not panel.current_videos:
                errors.append(f"{panel.name}: nenhum vídeo encontrado na pasta.")
                continue
            active += 1
            if not desc.strip() and bool(self.cfg.require_description) and not allow_empty:
                errors.append(f"{panel.name}: digite uma descrição ou permita publicar sem descrição.")
            missing = [v.name for v in panel.current_videos if not Path(v.path).exists()]
            if missing:
                errors.append(f"{panel.name}: {len(missing)} arquivo(s) não existem mais.")
        if active == 0:
            errors.insert(0, "Selecione pelo menos uma pasta de vídeos no perfil ativo.")
        return "; ".join(errors) if errors else None

    def _validate(self):
        err = self._validation_error()
        if err:
            self.hint_lbl.configure(text="⚠ " + err, text_color=T.ERROR)
            self.start_btn.configure(state="disabled")
        elif self.running:
            self.hint_lbl.configure(text="Publicação em andamento…",
                                    text_color=T.ACCENT_TEXT)
            self.start_btn.configure(state="disabled")
        else:
            self.hint_lbl.configure(text="✔ Pronto para iniciar.",
                                    text_color=T.PRIMARY_SOFT)
            self.start_btn.configure(state="normal")

    def _start(self):
        err = self._validation_error()
        if err:
            messagebox.showwarning("Validação", err, parent=self)
            return

        self._active_profiles = []
        publish_mode = self.cfg.get("publish_mode", "both")
        for i, panel in enumerate(self.panels):
            if publish_mode == "profile1" and i == 1:
                continue
            if publish_mode == "profile2" and i == 0:
                continue
            sm = self.sms[i]
            folder, desc, allow_empty = panel.collect()
            if not folder or not panel.current_videos:
                continue
            self._active_profiles.append(i)

            st = sm.state
            matches = bool(
                st is not None
                and st.folder == folder
                and len(st.videos) == len(panel.current_videos)
                and all(
                    st.videos[j].name == v.name
                    for j, v in enumerate(panel.current_videos)
                )
            )
            if matches:
                st.description = desc
                st.allow_no_description = allow_empty
                descs = panel.get_custom_descriptions()
                tags = panel.get_custom_hashtags()
                if panel.get_republish():
                    for vs in st.videos:
                        vs.status = PENDING
                        vs.attempts = 0
                        vs.error = None
                        vs.published_at = None
                for j, vs in enumerate(st.videos):
                    if vs.status != PUBLISHED:
                        vs.path = panel.current_videos[j].path
                    if vs.name in descs:
                        vs.custom_description = descs[vs.name]
                    elif not vs.custom_description:
                        pass
                    if vs.name in tags:
                        vs.custom_hashtags = tags[vs.name]
                    elif not vs.custom_hashtags:
                        pass
                sm.save()
                get_logger().info(
                    f"[{panel.name}] Sessão existente compatível — retomando "
                    f"({st.published_count()} já publicado(s), {st.pending_count()} pendente(s))"
                )
                panel.refresh_video_list(
                    [VideoInfo(name=v.name, path=v.path) for v in st.videos],
                    {j: v.status for j, v in enumerate(st.videos)},
                )
                panel.hide_resume_strip()
            else:
                sm.new_session(folder, desc, allow_empty, panel.current_videos,
                               custom_descriptions=panel.get_custom_descriptions(),
                               custom_hashtags=panel.get_custom_hashtags())
                get_logger().info(
                    f"[{panel.name}] Nova sessão criada ({len(panel.current_videos)} vídeo(s))"
                )
            self.cfg[f"profile{i + 1}_description"] = desc
            self.cfg[f"profile{i + 1}_republish"] = panel.get_republish()
        self.cfg.save()

        self._begin_worker()

    def _begin_worker(self):
        self.running = True
        self.paused = False
        self._cancel_all = False
        self._session_stats = {}
        self._running_profile = None
        self._manual_active = False
        self.worker_thread = threading.Thread(
            target=self._run_all_profiles, daemon=True, name="publisher-worker"
        )
        self.worker_thread.start()

        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="⏸  PAUSAR")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self._set_progress_labels(0, 0)
        self._hide_result_banner()
        self._update_profile_dots()
        self._validate()
        self._poll_lock()

    def _run_all_profiles(self):
        log = get_logger()
        for i, panel in enumerate(self.panels):
            if self._cancel_all:
                break
            if i not in self._active_profiles:
                continue
            sm = self.sms[i]
            st = sm.state
            if st is None:
                log.info(f"[{panel.name}] Nenhuma sessão pendente; perfil ignorado")
                continue

            if st.pending_count() == 0:
                folder, desc, allow_empty = panel.collect()
                if not folder or not panel.current_videos:
                    log.info(f"[{panel.name}] Perfil sem pasta/vídeos ativos; ignorado")
                    continue
                log.warning(
                    f"[{panel.name}] Sessão marcada como concluída, mas a execução foi solicitada novamente. "
                    "Reinicializando vídeos para publicação."
                )
                for v in st.videos:
                    v.status = PENDING
                    v.attempts = 0
                    v.error = None
                    v.published_at = None
                st.description = desc
                st.allow_no_description = allow_empty
                sm.save()
                log.info(f"[{panel.name}] Vídeos do perfil reinicializados para nova execução")

            log.info(
                f"[{panel.name}] Iniciando execução ({st.pending_count()} vídeo(s) pendente(s))"
            )
            self.ui_queue.put({
                "kind": "profile_running", "profile": panel.name,
            })
            publisher = Publisher(
                self.cfg,
                sm,
                emit=lambda kind, **data: self.ui_queue.put({"kind": kind, **data}),
                profile_dir=PROFILE_DIRS[panel.index],
                profile_name=panel.name,
            )
            self.publisher = publisher
            publisher.run()
            if publisher.cancel_event.is_set():
                self._cancel_all = True
                break

        total_pub = sum(int(s.get("published", 0)) for s in self._session_stats.values())
        total_fail = sum(int(s.get("failed", 0)) for s in self._session_stats.values())
        any_cancel = any(bool(s.get("cancelled")) for s in self._session_stats.values())
        any_fatal = next((s.get("fatal_error") for s in self._session_stats.values()
                          if s.get("fatal_error")), None)
        self.ui_queue.put({
            "kind": "all_finished",
            "published": total_pub,
            "failed": total_fail,
            "cancelled": any_cancel,
            "fatal_error": any_fatal,
            "stats": {name: dict(s) for name, s in self._session_stats.items()},
        })

    def _toggle_pause(self):
        if not self.publisher or not self.running:
            return
        if self.paused:
            self.publisher.resume()
            self.paused = False
            self.pause_btn.configure(text="⏸  PAUSAR")
            self.stage_lbl.configure(text="↻  Status: Continuando...")
            get_logger().info("Continuar solicitado pelo usuário")
        else:
            self.publisher.request_pause()
            self.paused = True
            self.pause_btn.configure(text="▶  CONTINUAR")
            self.stage_lbl.configure(text="⏸  Status: Pausado (aguardando etapa terminar)")
            get_logger().info("Pausa solicitado pelo usuário")

    def _cancel(self):
        if not self.publisher or not self.running:
            return
        self._cancel_all = True
        self.cancel_btn.configure(state="disabled")
        self.stage_lbl.configure(text="✕  Status: Cancelando...")
        get_logger().info("Cancelamento solicitado pelo usuário")
        self.publisher.request_cancel()

    def _toggle_browser_lock(self):
        if self._lock_desired:
            if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
                wlock.set_enabled(self._locked_hwnd, True)
            wlock.clear_marker()
            self._locked_hwnd = None
            self._lock_desired = False
            self._update_lock_button()
            get_logger().info("Travamento do navegador DESATIVADO")
        else:
            idx = self._running_profile
            if idx:
                i = int(idx.replace("Perfil ", ""))
            else:
                i = self.current_profile_index
            profile_dir = PROFILE_DIRS.get(i)
            if not profile_dir:
                return
            hwnd = wlock.find_hwnd(str(profile_dir))
            if not hwnd:
                get_logger().warning("Navegador da automação não encontrado; abra-o primeiro")
                return
            wlock.set_enabled(hwnd, False)
            wlock.save_marker(hwnd)
            self._locked_hwnd = hwnd
            self._lock_desired = True
            self._update_lock_button()
            get_logger().info(f"Travamento do navegador ATIVADO")

    def _update_lock_button(self):
        if self._lock_desired:
            self.lock_btn.configure(
                text="🔒  Navegador travado (clique para destravar)",
                fg_color="#5a2030", hover_color="#7a1220",
            )
        else:
            self.lock_btn.configure(
                text="🔓  Travamento do navegador",
                fg_color=T.SURF_HIGHEST, hover_color=T.DIM,
            )

    def _try_apply_lock(self):
        if not self._lock_desired or not self._running_profile:
            return
        if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
            return
        i = int(self._running_profile.replace("Perfil ", ""))
        profile_dir = PROFILE_DIRS.get(i)
        if not profile_dir:
            return
        hwnd = wlock.find_hwnd(str(profile_dir))
        if hwnd:
            wlock.set_enabled(hwnd, False)
            wlock.save_marker(hwnd)
            self._locked_hwnd = hwnd
            self.after(0, self._update_lock_button)
            get_logger().info(f"Navegador ({self._running_profile}) bloqueado")

    def _unlock_for_login(self):
        if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
            wlock.set_enabled(self._locked_hwnd, True)
            get_logger().info("Navegador destravado temporariamente (login manual)")

    def _poll_lock(self):
        if not self.running:
            return
        if self._lock_desired and not self._manual_active:
            self._try_apply_lock()
        self.after(1500, self._poll_lock)

    def _set_progress_labels(self, done: int, total: int, profile: str | None = None):
        prefix = f"{profile} • " if profile else ""
        pct = int((done / total) * 100) if total else 0
        self.progress_lbl.configure(text=f"PROGRESSO {prefix}".strip())
        self.pct_lbl.configure(text=f"{done} / {total} • {pct}%")

    def _show_manual(self, active: bool, message: str = ""):
        if active:
            msg = message or "Resolva na janela do navegador."
            self.manual_lbl.configure(
                text=(
                    f"⚠ AÇÃO MANUAL NECESSÁRIA — {msg}\n"
                    "O programa continuará automaticamente assim que você resolver."
                ),
                text_color=T.WARN,
            )
        else:
            self.manual_lbl.configure(text="")

    def _hide_result_banner(self):
        self.result_frame.pack_forget()

    def _retry_failed(self):
        if self.running:
            return
        had_failures = any(sm.state and sm.state.failed_count() > 0 for sm in self.sms)
        if not had_failures:
            return
        self._hide_result_banner()
        for i, sm in enumerate(self.sms):
            panel = self.panels[i]
            if sm.state and sm.state.failed_count() > 0:
                folder, desc, allow = panel.collect()
                if folder and folder != sm.state.folder:
                    messagebox.showinfo(
                        "Tentar novamente",
                        f"[{panel.name}] A sessão gravada aponta para outra pasta:\n{sm.state.folder}",
                        parent=self,
                    )
                    return
        self._start()

    def _skip_failed(self):
        if self.running:
            return
        for sm in self.sms:
            if sm.state and sm.state.failed_count() > 0:
                sm.mark_errors_skipped()
        for i, sm in enumerate(self.sms):
            panel = self.panels[i]
            if sm.state:
                panel.refresh_video_list(
                    [VideoInfo(name=v.name, path=v.path) for v in sm.state.videos],
                    {j: v.status for j, v in enumerate(sm.state.videos)},
                )
        get_logger().info("Vídeos com erro marcados como ignorados")
        self._hide_result_banner()
        self._update_profile_dots()

    def _poll_queues(self):
        try:
            while True:
                ev = self.ui_queue.get_nowait()
                try:
                    self._handle_event(ev)
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            while True:
                line = self.gui_log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(90, self._poll_queues)

    def _handle_event(self, ev: dict):
        kind = ev.pop("kind")
        profile = ev.pop("profile", None)

        if kind == "video_status":
            if profile:
                panel = self._panel_by_name(profile)
                if panel:
                    panel.set_row_status(int(ev["index"]), ev["status"], ev.get("error"))
            if self._lock_desired and self._manual_active:
                self._manual_active = False
                if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
                    wlock.set_enabled(self._locked_hwnd, False)
        elif kind == "current_video":
            name = ev.get("name")
            stage = ev.get("stage") or "-"
            prefix = f"[{profile}] " if profile else ""
            self.current_lbl.configure(
                text=f"▶  {prefix}{name}" if name else "▶  Vídeo atual: -"
            )
            if not self.paused:
                self.stage_lbl.configure(text=f"↻  {stage}")
            if self._lock_desired and self._manual_active:
                self._manual_active = False
                if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
                    wlock.set_enabled(self._locked_hwnd, False)
        elif kind == "progress":
            done, total = int(ev.get("done", 0)), int(ev.get("total", 0))
            self.progress_bar.set((done / total) if total else 0)
            self._set_progress_labels(done, total, profile)
        elif kind == "manual_required":
            self._manual_active = bool(ev.get("active"))
            self._show_manual(self._manual_active, ev.get("message"))
            if self._lock_desired and self._manual_active:
                self._unlock_for_login()
        elif kind == "profile_running":
            self._running_profile = profile
            self._update_profile_dots()
            if self._lock_desired:
                self._locked_hwnd = None
                self.after(1500, self._try_apply_lock)
            if self._monitoring:
                self.monitor.show_active(profile or "")
        elif kind == "preview_image":
            img_data = ev.get("image_bytes")
            if img_data and self._monitoring:
                self.monitor._update_image(img_data)
        elif kind == "finished":
            if profile:
                self._session_stats[profile] = ev
        elif kind == "all_finished":
            self.monitor.show_done()
            self._finish_run(ev)

    def _finish_run(self, ev: dict):
        self.running = False
        self.paused = False
        published = int(ev.get("published", 0))
        failed = int(ev.get("failed", 0))
        cancelled = bool(ev.get("cancelled"))
        fatal = ev.get("fatal_error")
        stats = ev.get("stats") or {}

        self.pause_btn.configure(state="disabled", text="⏸  PAUSAR")
        self.cancel_btn.configure(state="disabled")
        self.publisher = None
        self._running_profile = None
        self._manual_active = False
        self._locked_hwnd = None
        self._lock_desired = False
        self._update_lock_button()
        self.current_lbl.configure(text="▶  Vídeo atual: -")

        if fatal:
            self.stage_lbl.configure(text="✗  Erro fatal", text_color=T.ERROR)
        elif cancelled:
            self.stage_lbl.configure(text="✕  Cancelado", text_color=T.WARN)
        else:
            self.stage_lbl.configure(text="✓  Finalizado (todos os perfis)",
                                     text_color=T.PRIMARY_SOFT)

        self._show_manual(False)
        self._update_profile_dots()

        fail_lines = []
        for pname, s in stats.items():
            if int(s.get("failed", 0)) > 0:
                fail_lines.append(f"{pname}: {s.get('failed')} falha(s)")
        if fail_lines:
            self.result_msg.configure(text="\n".join(fail_lines))
            self.result_frame.pack(fill="x", before=self.actions_card, pady=(0, 12))

        detail = "\n".join(
            f"{pname}: {int(s.get('published', 0))}/{int(s.get('total', 0))} publicados"
            for pname, s in stats.items()
        ) or "Nenhum vídeo pendente."
        get_logger().info(
            f"Publicação finalizada | {published} publicado(s), {failed} falha(s) | {detail}"
        )
        self.result_msg.configure(text=detail)
        self.result_frame.pack(fill="x", before=self.actions_card, pady=(0, 12))
        self._validate()

    def _append_log(self, line: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 800:
            self.log_box.delete("1.0", f"{lines - 600}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _finalize_close(self):
        try:
            for i, panel in enumerate(self.panels):
                _, desc, _allow = panel.collect()
                if desc.strip():
                    self.cfg[f"profile{i + 1}_description"] = desc
            self.cfg.save()
        except Exception:
            pass
        try:
            if self._locked_hwnd and wlock.is_window(self._locked_hwnd):
                wlock.set_enabled(self._locked_hwnd, True)
            wlock.clear_marker()
        except Exception:
            pass
        self._closing = False
        self.destroy()

    def _on_close(self):
        if self._closing:
            return
        self._closing = True

        if self.running:
            if not messagebox.askyesno(
                "Sair",
                "Há uma publicação em andamento. Deseja cancelar e sair?",
                parent=self,
            ):
                self._closing = False
                return
            get_logger().info("Fechando janela: cancelando execução")
            self._cancel_all = True
            if self.publisher:
                self.publisher.request_cancel()
            if self.worker_thread:
                self.worker_thread.join(timeout=6)
            self._finalize_close()
            return

        self._finalize_close()
