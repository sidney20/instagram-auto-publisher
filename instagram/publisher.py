from __future__ import annotations

import base64
import random
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from config import ERRORS_DIR
from core.logger import get_logger
from core.state_manager import (
    ACTIVE_STATUSES,
    DESCRIBING,
    ERROR,
    PENDING,
    PUBLISHED,
    UPLOADING,
    StateManager,
    VideoState,
)
from instagram import browser as browser_mod
from instagram import cover_detector
from instagram import description as description_mod
from instagram.login import (
    LoginTimeout,
    ManualActionRequired,
    assert_no_challenge,
    ensure_logged_in,
    has_session_cookie,
    login_form_visible,
)
from instagram.selectors import (
    CHALLENGE_URL_MARKERS,
    COVER_RANGE_INPUT_CSS,
    COVER_SLIDER_ROLE_TEXTS,
    COVER_STRIP_CSS,
    CREATE_BUTTON_LABELS,
    CREATE_CSS_CANDIDATES,
    CREATE_URL_FALLBACK,
    DIALOG_CSS,
    DISCARD_TEXTS,
    ERROR_TOAST_TEXTS,
    EXPAND_BUTTON_ARIA,
    EXPAND_CSS_CANDIDATES,
    EXPAND_ICON_CSS,
    FILE_INPUT_ACCEPT_VIDEO,
    FILE_INPUT_ANY,
    INSTAGRAM_HOME_URL,
    NEXT_BUTTON_TEXTS,
    SELECT_FROM_COMPUTER_TEXTS,
    SHARE_BUTTON_TEXTS,
    SUCCESS_TEXTS,
)


class CancelledRequest(Exception):
    pass


class PublishStepError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _is_target_closed_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "target page, context or browser has been closed",
            "targetclosederror",
            "browser has been closed",
            "page has been closed",
        )
    )


def _page_is_closed(page) -> bool:
    if page is None:
        return True
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _texts_regex(texts, anchored: bool = True) -> re.Pattern:
    body = "|".join(re.escape(t) for t in texts)
    if anchored:
        return re.compile(r"^\s*(?:" + body + r")\s*:?\s*$", re.IGNORECASE)
    return re.compile(body, re.IGNORECASE)


_SUCCESS_RX = _texts_regex(SUCCESS_TEXTS, anchored=False)
_ERROR_TOAST_RX = _texts_regex(ERROR_TOAST_TEXTS, anchored=False)


class Publisher:
    def __init__(self, cfg, sm: StateManager, emit, profile_dir=None,
                 profile_name: str = "Perfil 1"):
        self.cfg = cfg
        self.sm = sm

        def _tagged(kind, **data):
            emit(kind, profile=profile_name, **data)

        self.emit = _tagged
        self.profile_name = profile_name
        self.profile_dir = profile_dir
        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()
        self._page = None
        self._ctx = None
        self._screenshot_stop = threading.Event()
        self._screenshot_thread = None
        self._preview_failures = 0
        self._debug_dir = Path(__file__).resolve().parents[1] / "debug"

    def request_pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        self.resume()

    def _checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledRequest()
        while self.pause_event.is_set():
            if self.cancel_event.is_set():
                raise CancelledRequest()
            time.sleep(0.2)
        if self.cancel_event.is_set():
            raise CancelledRequest()

    def _emit_preview(self, page) -> None:
        """Captura no mesmo thread do Playwright e entrega o quadro à UI."""
        try:
            if page is None or page.is_closed():
                return
            cdp = page.context.new_cdp_session(page)
            result = cdp.send(
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 70, "fromSurface": True},
            )
            data = base64.b64decode(result["data"])
            if data:
                self._preview_failures = 0
                self.emit("preview_image", image_bytes=data)
        except Exception as exc:
            self._preview_failures += 1
            if self._preview_failures <= 3:
                get_logger().warning(
                    f"Preview ao vivo indisponível ({datetime.now():%H:%M:%S}): {exc}"
                )

    def _small_delay(self, page=None, extra_ms: int = 0) -> None:
        speed = str(self.cfg.get("autopilot_speed", "normal")).lower()
        presets = {
            "rapido": (150, 400),
            "normal": (400, 900),
            "humano": (900, 2000),
        }
        lo, hi = presets.get(
            speed,
            (int(self.cfg.action_delay_min_ms), int(self.cfg.action_delay_max_ms)),
        )
        ms = random.randint(lo, hi) + int(extra_ms)
        try:
            if page is not None:
                page.wait_for_timeout(ms)
            else:
                time.sleep(ms / 1000.0)
        except Exception as exc:
            if _is_target_closed_error(exc):
                raise PublishStepError(
                    "interferencia_usuario",
                    "O navegador foi fechado ou perdeu a página durante a publicação.",
                ) from exc
            time.sleep(ms / 1000.0)
        if page is not None:
            self._emit_preview(page)

    def _sleep_between_videos(self) -> None:
        end = time.monotonic() + float(self.cfg.delay_between_videos_s)
        while time.monotonic() < end:
            self._checkpoint()
            time.sleep(0.2)

    def _screenshot(self, page, video_name: str, stage: str) -> None:
        try:
            ERRORS_DIR.mkdir(parents=True, exist_ok=True)
            stem = Path(video_name).stem if video_name else "sessao"
            safe_stage = re.sub(r"[^A-Za-z0-9_-]+", "_", stage)[:40]
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = ERRORS_DIR / f"{stem}_{safe_stage}_{ts}.png"
            page.screenshot(path=str(path))
            get_logger().error(f"Screenshot do erro salvo em: {path}")
        except Exception:
            pass

    def _debug_cover_shot(self, page, label: str) -> None:
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:40]
            path = self._debug_dir / f"capa_{safe_label}_{ts}.png"
            page.screenshot(path=str(path), animations="disabled")
            get_logger().info(f"[CAPA] screenshot salvo em: {path}")
        except Exception:
            pass

    def run(self) -> None:
        log = get_logger()
        state = self.sm.state
        total = len(state.videos)

        for vs in state.videos:
            if vs.status in ACTIVE_STATUSES:
                vs.status = PENDING
        self.sm.save()
        log.info(
            f"Sessão iniciada: {total} vídeo(s) | "
            f"{state.published_count()} já publicado(s) | {state.pending_count()} pendente(s)"
        )

        pw = None
        ctx = None
        page = None
        cancelled = False
        fatal_error = None
        summary = {"published": 0, "failed": 0, "total": total}

        def reopen_browser() -> None:
            nonlocal pw, ctx, page
            log.warning(
                f"[{self.profile_name}] Navegador fechado durante a publicação; "
                "reabrindo para repetir o vídeo atual."
            )
            browser_mod.stop(pw, ctx)
            pw, ctx = browser_mod.launch(self.cfg, self.profile_dir)
            page = browser_mod.first_page(ctx)
            self._page = page
            self._ctx = ctx
            self._emit_preview(page)
            self.emit("current_video", stage="Navegador reaberto; repetindo vídeo atual")

        try:
            log.info(f"[{self.profile_name}] Abrindo navegador...")
            pw, ctx = browser_mod.launch(self.cfg, self.profile_dir)
            page = browser_mod.first_page(ctx)
            self._page = page
            self._ctx = ctx
            self._emit_preview(page)

            self.emit("current_video", name=None, stage="Verificando sessão do Instagram")
            if has_session_cookie(ctx):
                log.info("Cookie de sessão presente; iniciando publicação direto")
            else:
                try:
                    ensure_logged_in(
                        ctx,
                        page,
                        self.cfg,
                        notify=lambda active: self.emit(
                            "manual_required",
                            active=active,
                            message="Faça login no Instagram na janela do navegador.",
                        ),
                        gate=self._checkpoint,
                    )
                except ManualActionRequired as e:
                    self._wait_manual(page, e.user_message)

            self.emit("progress", done=state.published_count(), total=total)

            for idx, vs in enumerate(state.videos):
                if vs.status == PUBLISHED:
                    continue

                vs.attempts = 0
                published = False
                last_error = None
                max_retries = max(1, int(self.cfg.max_attempts))

                while vs.attempts < max_retries:
                    self._checkpoint()
                    if _page_is_closed(page):
                        reopen_browser()
                    vs.attempts += 1

                    if vs.attempts > 1:
                        log.info(f"RETRY: Tentando vídeo '{vs.name}' novamente (tentativa {vs.attempts}/{max_retries})")
                        self.emit("video_status", index=idx, status="retrying")
                        self.emit("current_video", name=vs.name, stage=f"Tentativa {vs.attempts}/{max_retries}")
                        time.sleep(3)

                    try:
                        self._publish_one(idx, vs)
                        published = True
                        break
                    except ManualActionRequired as e:
                        vs.attempts -= 1
                        self._wait_manual(page, e.user_message)
                    except CancelledRequest:
                        raise
                    except PublishStepError as e:
                        stage = getattr(e, "stage", "etapa_desconhecida")
                        last_error = f"{stage}: {e}"
                        if stage == "interferencia_usuario":
                            log.error(
                                f"INTERFERÊNCIA DETECTADA no vídeo '{vs.name}' "
                                f"durante '{stage}': {e}"
                            )
                            self.emit(
                                "current_video",
                                name=vs.name,
                                stage="Interferência detectada; preparando repetição",
                            )
                        else:
                            log.error(
                                f"Falha no vídeo '{vs.name}' "
                                f"(tentativa {vs.attempts}/{max_retries}) - {last_error}"
                            )
                        log.debug(traceback.format_exc())
                        self._screenshot(page, vs.name, stage)
                        self._recover(page)
                        if _page_is_closed(page):
                            reopen_browser()

                        if vs.attempts < max_retries:
                            log.warning(
                                f"Repetindo o mesmo vídeo '{vs.name}' "
                                f"(tentativa {vs.attempts + 1}/{max_retries})"
                            )
                            self.emit("video_status", index=idx, status="preparing_retry")
                        else:
                            log.error(f"Esgotadas todas as tentativas para '{vs.name}'")
                    except Exception as e:
                        stage = getattr(e, "stage", "etapa_desconhecida")
                        if _is_target_closed_error(e):
                            stage = "interferencia_usuario"
                        last_error = f"{stage}: {e}"
                        if stage == "interferencia_usuario":
                            log.error(
                                f"INTERFERÊNCIA DETECTADA no vídeo '{vs.name}': {e}. "
                                "A publicação será repetida automaticamente."
                            )
                        else:
                            log.error(
                                f"Falha no vídeo '{vs.name}' "
                                f"(tentativa {vs.attempts}/{max_retries}) - {last_error}"
                            )
                        log.debug(traceback.format_exc())
                        self._screenshot(page, vs.name, stage)
                        self._recover(page)
                        if _page_is_closed(page):
                            reopen_browser()

                        if vs.attempts < max_retries:
                            log.info(f"Preparando para retry do vídeo '{vs.name}'...")
                            self.emit("video_status", index=idx, status="preparing_retry")
                        else:
                            log.error(f"Esgotadas todas as tentativas para '{vs.name}'")

                if published:
                    vs.status = PUBLISHED
                    vs.error = None
                    vs.published_at = datetime.now().isoformat(timespec="seconds")
                    summary["published"] += 1
                    log.info(f"Vídeo concluído: {vs.name}")
                else:
                    vs.status = ERROR
                    vs.error = last_error or "erro desconhecido"
                    summary["failed"] += 1
                    log.error(f"Vídeo '{vs.name}' marcado com ERRO após {vs.attempts} tentativa(s)")
                    self.emit("video_status", index=idx, status=vs.status, error=vs.error)
                    if self.cfg.stop_on_error:
                        self.sm.save()
                        break

                self.sm.save()
                self.emit("video_status", index=idx, status=vs.status, error=vs.error)
                self.emit("progress", done=state.published_count(), total=total)

                remaining = [v for v in state.videos[idx + 1:] if v.status != PUBLISHED]
                if remaining:
                    self._sleep_between_videos()

        except CancelledRequest:
            cancelled = True
            log.warning("Operação cancelada pelo usuário")
        except LoginTimeout as e:
            fatal_error = str(e)
            log.error(fatal_error)
        except Exception as e:
            fatal_error = f"{type(e).__name__}: {e}"
            log.error(f"Erro fatal: {fatal_error}")
            log.debug(traceback.format_exc())
        finally:
            browser_mod.stop(pw, ctx)
            self.sm.save()
            summary["cancelled"] = cancelled
            summary["fatal_error"] = fatal_error
            if self.sm.state is not None:
                summary["pending"] = self.sm.state.pending_count()
            self.emit("finished", **summary)
            log.info("Navegador encerrado")

    def _assert_flow_alive(self, page, step_name: str, require_dialog: bool = True) -> None:
        log = get_logger()
        try:
            if require_dialog:
                dlg = page.locator(DIALOG_CSS).first
                if dlg.count() == 0 or not dlg.is_visible():
                    self._screenshot(page, "interferencia", "usuario_interrompeu")
                    raise PublishStepError(
                        "interferencia_usuario",
                        f"Fluxo interrompido na etapa '{step_name}': o diálogo de publicação "
                        "foi fechado ou substituído (sinal de interferência do usuário na tela).",
                    )
            else:
                current_url = page.url or ""
                if "instagram" not in current_url:
                    self._screenshot(page, "interferencia", "usuario_navegou")
                    raise PublishStepError(
                        "interferencia_usuario",
                        f"Fluxo interrompido na etapa '{step_name}': o navegador saiu do Instagram "
                        "(sinal de interferência do usuário na tela).",
                    )
        except PublishStepError:
            raise
        except Exception as e:
            log.warning(
                f"Não foi possível verificar o fluxo na etapa '{step_name}': {e}. "
                "A tentativa será repetida por segurança."
            )
            raise PublishStepError(
                "interferencia_usuario",
                f"Não foi possível confirmar o fluxo na etapa '{step_name}'.",
            ) from e

    def _publish_one(self, idx: int, vs: VideoState) -> None:
        page = self._page
        state = self.sm.state
        log = get_logger()

        self._checkpoint()
        log.info(f"Iniciando vídeo {idx + 1}/{len(state.videos)}: {vs.name} (tentativa {vs.attempts})")
        self.emit("current_video", name=vs.name, stage="Preparando")

        self._open_composer(page)
        self._checkpoint()
        self._small_delay(page)

        self.emit("current_video", name=vs.name, stage="Selecionando arquivo")
        self._set_video_file(page, vs.path)
        self._small_delay(page)
        self._assert_flow_alive(page, "selecionar_arquivo", require_dialog=False)

        self.emit("current_video", name=vs.name, stage="Expandindo vídeo para tamanho original")
        self._expand_to_original_size(page)

        vs.status = UPLOADING
        self.sm.save()
        self.emit("video_status", index=idx, status=UPLOADING)
        log.info("Upload iniciado")
        self._assert_flow_alive(page, "apos_expandir")

        self._process_and_reach_caption(page, vs.name, vs.path)

        vs.status = "describing"
        self.sm.save()
        self.emit("video_status", index=idx, status="describing")
        self.emit("current_video", name=vs.name, stage="Inserindo descrição")
        self._assert_flow_alive(page, "antes_descricao")
        self._small_delay(page, extra_ms=400)
        self._fill_caption(page, vs.name, vs.custom_description, vs.custom_hashtags)
        self._small_delay(page, extra_ms=500)

        vs.status = "publishing"
        self.sm.save()
        self.emit("video_status", index=idx, status="publishing")
        self.emit("current_video", name=vs.name, stage="Publicando")
        self._assert_flow_alive(page, "antes_publicar")
        self._small_delay(page, extra_ms=400)

        self._click_share(page)
        log.info("Botão Publicar acionado; aguardando confirmação...")
        self.emit("current_video", name=vs.name, stage="Confirmando publicação")
        self.wait_until_post_published(page)

        self._small_delay(page)
        log.info(f"Vídeo '{vs.name}' confirmado como publicado")

    def _open_composer(self, page) -> None:
        log = get_logger()
        self.emit("current_video", stage="Abrindo Instagram")
        page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        assert_no_challenge(page)
        if login_form_visible(page):
            raise ManualActionRequired(
                "Sessão expirada ou ausente. Faça login na janela do navegador."
            )

        clicked = False
        candidate_selectors = list(CREATE_CSS_CANDIDATES)
        for label in CREATE_BUTTON_LABELS:
            candidate_selectors.append(f'svg[aria-label="{label}"]')
            candidate_selectors.append(f'div[role="button"][aria-label="{label}"]')

        for sel in candidate_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=3500)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            log.warning("Botão de nova publicação não encontrado; tentando URL direta")
            page.goto(CREATE_URL_FALLBACK, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            assert_no_challenge(page)

        page.wait_for_timeout(800)

        POSTAR_TEXTS = ["Postar", "Post", "Create post", "Crear publicación", "Créer une publication"]
        postar_clicked = False
        for text in POSTAR_TEXTS:
            try:
                loc = page.get_by_text(text, exact=True).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=5000)
                    postar_clicked = True
                    log.info(f"Opção '{text}' clicada no menu de criação")
                    break
            except Exception:
                continue

        if not postar_clicked:
            raise PublishStepError(
                "abrir_compositor",
                "Não foi possível encontrar/clicar em 'Postar' no menu de criação.",
            )

        if not self._wait_dialog(page, timeout_s=20):
            raise PublishStepError(
                "abrir_compositor",
                "O diálogo de criação de publicação não apareceu após clicar em 'Postar'.",
            )
        self._small_delay(page)

    def _wait_dialog(self, page, timeout_s: float) -> bool:
        log = get_logger()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                dlg = page.locator(DIALOG_CSS)
                if dlg.count() > 0 and dlg.first.is_visible():
                    return True
            except Exception as exc:
                if _is_target_closed_error(exc):
                    raise PublishStepError(
                        "interferencia_usuario",
                        "A página foi fechada enquanto aguardava a confirmação.",
                    ) from exc
                log.warning(f"Interrupção detectada ao aguardar o diálogo: {exc}")
            page.wait_for_timeout(400)
        return False

    def _set_video_file(self, page, video_path: str) -> None:
        try:
            inp = page.locator(FILE_INPUT_ACCEPT_VIDEO).first
            if inp.count() == 0:
                inp = page.locator(FILE_INPUT_ANY).first
            if inp.count() > 0:
                inp.set_input_files(video_path)
                return

            btn = self._find_text_button(page, SELECT_FROM_COMPUTER_TEXTS, timeout_s=15)
            with page.expect_file_chooser(timeout=15000) as fc_info:
                btn.click()
            fc_info.value.set_files(video_path)
        except PublishStepError:
            raise
        except Exception as e:
            raise PublishStepError(
                "selecionar_arquivo", f"Falha ao selecionar o arquivo do vídeo: {e}"
            ) from e

    def _expand_to_original_size(self, page) -> None:
        log = get_logger()
        self._wait_crop_dialog_ready(page)
        self._screenshot(page, "debug", "antes_expandir")
        log.info("Procurando botão de crop/expandir...")

        expand_clicked = self._click_crop_button(page)
        if not expand_clicked:
            raise PublishStepError(
                "expandir_crop",
                "Não foi possível localizar/clicar no botão de corte (Selecionar corte) "
                "na tela de ajuste do vídeo.",
            )

        self._small_delay(page, extra_ms=500)

        original_clicked = self._select_original(page)
        if not original_clicked:
            raise PublishStepError(
                "selecionar_original",
                "O botão de corte foi clicado, mas a opção 'Original' não pôde ser "
                "confirmada no menu de proporção.",
            )

        log.info("Vídeo confirmado no formato original.")

    def _wait_crop_dialog_ready(self, page) -> None:
        log = get_logger()
        deadline = time.monotonic() + float(self.cfg.step_timeout_s)
        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                dlg = page.locator(DIALOG_CSS).first
                if dlg.count() == 0 or not dlg.is_visible():
                    page.wait_for_timeout(300)
                    continue
                has_video = page.locator('div[role="dialog"] video').count() > 0 or \
                            page.locator('div[role="dialog"] img').count() > 0
                video_ready = True
                if page.locator('div[role="dialog"] video').count() > 0:
                    try:
                        v = page.locator('div[role="dialog"] video').first
                        video_ready = bool(v.evaluate(
                            "el => el.readyState >= 2 && !el.paused"))
                    except Exception:
                        video_ready = False
                if has_video and video_ready:
                    log.info("Diálogo de ajuste do vídeo carregado.")
                    page.wait_for_timeout(800)
                    return
            except Exception:
                pass
            page.wait_for_timeout(400)
        log.info("Dialog carregado (timelimit); prosseguindo para busca do botão de corte.")

    def _click_crop_button(self, page) -> bool:
        log = get_logger()
        crop_selectors = [
            'div[role="dialog"] button[aria-label*="Selecionar corte" i]',
            'div[role="dialog"] button[aria-label*="Selecionar recorte" i]',
            'div[role="dialog"] button[aria-label*="corte" i]',
            'div[role="dialog"] [role="button"][aria-label*="Selecionar corte" i]',
            'div[role="dialog"] [role="button"][aria-label*="corte" i]',
            'div[role="dialog"] svg[aria-label*="Selecionar corte" i]',
            'div[role="dialog"] svg[aria-label*="corte" i]',
            'div[role="dialog"] button[aria-label*="crop" i]',
            'div[role="dialog"] button[aria-label*="resize" i]',
            'div[role="dialog"] button[aria-label*="expand" i]',
            'div[role="dialog"] button[aria-label*="Expandir" i]',
            'div[role="dialog"] button[aria-label*="recortar" i]',
            'div[role="dialog"] button[aria-label*="cortar" i]',
            'div[role="dialog"] svg[aria-label*="crop" i]',
            'div[role="dialog"] [role="button"][aria-label*="expand" i]',
            'div[role="button"][aria-label*="Expand" i]',
            'div[role="button"][aria-label*="Expandir" i]',
            'svg[aria-label*="Selecionar corte" i]',
            'svg[aria-label*="crop" i]',
            'svg[aria-label*="corte" i]',
        ]

        for attempt in range(4):
            self._checkpoint()
            located = self._find_clickable(page, crop_selectors)
            if located is not None:
                try:
                    located.click(timeout=5000)
                    log.info(f"Botão de corte/expandir clicado (tentativa {attempt + 1})")
                    page.wait_for_timeout(1400)
                    if self._proportion_menu_open(page):
                        self._screenshot(page, "debug", "menu_aberto")
                        log.info("Menu de proporção confirmado após clique no corte.")
                        return True
                    log.info("Menu ainda não detectado após clique; tentando novamente.")
                except Exception:
                    log.info("Clique no botão de corte falhou; tentando novamente.")
            else:
                found = self._scan_crop_button(page)
                if found:
                    if self._proportion_menu_open(page):
                        self._screenshot(page, "debug", "menu_aberto_varredura")
                        log.info("Menu de proporção confirmado via varredura.")
                        return True
            self._small_delay(page, extra_ms=500)
        return False

    def _find_clickable(self, page, selectors):
        for css in selectors:
            try:
                loc = page.locator(css).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _scan_crop_button(self, page) -> bool:
        log = get_logger()
        try:
            all_btns = page.locator(
                'div[role="dialog"] button, div[role="dialog"] [role="button"]'
            ).all()
            log.info(f"Varredura: {len(all_btns)} botões no diálogo")
            for btn in all_btns:
                try:
                    if not btn.is_visible():
                        continue
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    text = (btn.inner_text() or "").lower()
                    cls = (btn.get_attribute("class") or "").lower()
                    html = (btn.evaluate("el => el.outerHTML") or "").lower()
                    is_expand = any(w in aria or w in text for w in [
                        "crop", "corte", "selecionar", "recortar", "cortar",
                        "expand", "expandir", "resize", "ajustar", "full",
                    ])
                    is_expand = is_expand or "expand" in cls or "crop" in cls
                    is_expand = is_expand or "expand" in html or "crop" in html
                    if is_expand:
                        btn.click(timeout=4000)
                        log.info(f"Botão de corte clicado via varredura: aria='{aria}'")
                        page.wait_for_timeout(1400)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _proportion_menu_open(self, page) -> bool:
        try:
            for label in ("Original", "Tamanho original", "Original size", "16:9", "9:16", "16:9"):
                loc = page.get_by_text(label, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return True
            menu = page.locator('div[role="menu"]')
            if menu.count() > 0 and menu.first.is_visible():
                return True
        except Exception:
            pass
        return False

    def _select_original(self, page) -> bool:
        log = get_logger()
        original_texts = ["Original", "Tamanho original", "Original size"]
        for attempt in range(4):
            self._checkpoint()
            for text in original_texts:
                try:
                    loc = page.get_by_text(text, exact=True).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=4000)
                        log.info(f"Opção '{text}' clicada no menu de proporção (tentativa {attempt + 1})")
                        page.wait_for_timeout(1200)
                        self._screenshot(page, "debug", "depois_original")
                        return True
                except Exception:
                    continue
            try:
                menu_loc = page.locator(
                    'div[role="menu"] [role="menuitem"], '
                    'div[role="menu"] div'
                ).all()
                for el in menu_loc:
                    try:
                        if not el.is_visible():
                            continue
                        et = (el.inner_text() or "").strip().lower()
                        if et in ("original", "tamanho original", "horizontal"):
                            el.click(timeout=3000)
                            log.info(f"Opção '{et}' via varredura de menu (tentativa {attempt + 1})")
                            page.wait_for_timeout(1200)
                            self._screenshot(page, "debug", "depois_original_menu")
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            self._small_delay(page, extra_ms=600)
        return False

    def _is_clickable(self, loc) -> bool:
        try:
            if not loc.is_visible():
                return False
            if loc.get_attribute("disabled") is not None:
                return False
            aria_disabled = loc.get_attribute("aria-disabled")
            if aria_disabled is not None and str(aria_disabled).lower() == "true":
                return False
            cls = (loc.get_attribute("class") or "").split()
            if "disabled" in [c.lower() for c in cls]:
                return False
            return True
        except Exception:
            return False

    def _select_cover_frame(self, page, video_path: str | None = None) -> bool | None:
        log = get_logger()
        _log = lambda msg: log.info(f"[CAPA] {msg}")
        _err = lambda msg: log.info(f"[CAPA][ERRO] {msg}")

        _log("Entrando na tela de seleção de capa.")
        self._debug_cover_shot(page, "00_tela_selecao")

        preview = self._find_preview_locator(page)
        strip = self._find_cover_strip(page)
        if preview is None or strip is None:
            _log("Prévia grande ou seletor de timeline não encontrados; esta tela não é a de seleção de capa.")
            return None

        _log("Prévia grande encontrada.")
        _log("Seletor de timeline encontrado.")
        self._dump_cover_dom(page)

        _log("Aguardando vídeo grande carregar e ficar pausado/estável.")
        self._wait_preview_ready(page)

        _log("Analisando prévia grande (posição inicial)...")
        present, score, _ = self._preview_has_banner(page)
        if present:
            _log(f"Banner já presente na prévia inicial (score={score:.2f}).")
            _log("Nenhum movimento necessário.")
            self._debug_cover_shot(page, "05_capa_encontrada")
            _log("CAPA CONFIRMADA. Continuando fluxo normal.")
            return True

        _log("Banner NÃO encontrado na prévia inicial. Iniciando movimentação do seletor.")
        if self._sweep_cover_timeline(page):
            _log("CAPA CONFIRMADA. Continuando fluxo normal.")
            return True

        _err("Final da timeline alcançado e banner não encontrado.")
        self._debug_cover_shot(page, "06_nao_confirmada")
        return False

    def _capture_preview_img(self, page):
        try:
            import numpy as np
            import cv2

            preview = self._find_preview_locator(page)
            if preview is None:
                return None
            shot_bytes = preview.screenshot()
            if not shot_bytes:
                return None
            img = cv2.imdecode(np.frombuffer(shot_bytes, np.uint8), cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                return None
            return img
        except Exception:
            return None

    def _preview_has_banner(self, page) -> tuple[bool, float, float]:
        log = get_logger()
        img = self._capture_preview_img(page)
        if img is None:
            log.info("[CAPA] Prévia grande não pôde ser capturada para análise.")
            return False, 0.0, 0.0

        try:
            signature = cover_detector._banner_signature(img)
            score = cover_detector._compute_banner_score(signature)
            found = score >= 0.45
            log.info(f"[CAPA] Prévia analisada: banner={'SIM' if found else 'NÃO'} score={score:.2f}.")
            return found, score, score
        except Exception as exc:
            log.info(f"[CAPA][ERRO] Falha na análise local da prévia: {exc}")
            return False, 0.0, 0.0

    def _wait_preview_ready(self, page, timeout_s: float = 20.0) -> bool:
        log = get_logger()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._checkpoint()
            preview = self._find_preview_locator(page)
            if preview is not None:
                try:
                    preview.evaluate(
                        """(el) => {
                            if (el.tagName && el.tagName.toLowerCase() === 'video') {
                                try { el.pause(); } catch (e) {}
                            }
                        }"""
                    )
                except Exception:
                    pass
                try:
                    import cv2
                    import numpy as np

                    img = self._capture_preview_img(page)
                    if img is not None:
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
                        has_content = float(np.std(gray)) > 6.0
                        if not has_content:
                            page.wait_for_timeout(400)
                            continue
                except Exception:
                    pass
                s1 = self._capture_preview_sig(page)
                page.wait_for_timeout(500)
                s2 = self._capture_preview_sig(page)
                if s1 is not None and s2 is not None and self._sig_similar(s1, s2):
                    log.info("[CAPA] Prévia grande carregada, pausada e estável.")
                    return True
            page.wait_for_timeout(400)
        log.info("[CAPA] Prévia grande não ficou estável dentro do tempo limite.")
        return False

    def _wait_preview_stable(self, page, timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._checkpoint()
            s1 = self._capture_preview_sig(page)
            page.wait_for_timeout(500)
            s2 = self._capture_preview_sig(page)
            if s1 is not None and s2 is not None and self._sig_similar(s1, s2):
                return True
        return False

    def _move_seeker(self, page, fraction: float) -> str:
        log = get_logger()
        prev_sig = self._capture_preview_sig(page)
        ok, method = self._seek_cover_to(page, fraction, prev_sig=prev_sig)
        if not ok:
            log.info("[CAPA] Drag não confirmado; relocalizando o seletor para uma nova tentativa.")
            page.wait_for_timeout(300)
            ok, method = self._seek_cover_to(page, fraction, prev_sig=prev_sig)
        log.info(f"[CAPA] Drag concluído ({method}). Aguardando atualização da prévia...")
        self._wait_preview_stable(page)
        cur_sig = self._capture_preview_sig(page)
        moved = self._movement_desc(prev_sig, cur_sig)
        if moved == "previa_inerte":
            log.info("[CAPA][AVISO] O seletor foi movimentado, mas a prévia não mudou.")
        return method

    def _sweep_cover_timeline(self, page) -> bool:
        log = get_logger()
        _log = lambda msg: log.info(f"[CAPA] {msg}")
        _err = lambda msg: log.info(f"[CAPA][ERRO] {msg}")

        strip = self._find_cover_strip(page)
        if strip is None:
            _err("Seletor de capa não encontrado para iniciar a timeline.")
            return False
        try:
            box = strip.bounding_box()
            if box is None or box["width"] <= 0:
                _err("Timeline sem largura válida.")
                return False
            step = max(1.0 / 20.0, min(0.12, 80.0 / box["width"]))
        except Exception as exc:
            _err(f"Não foi possível medir a timeline: {exc}")
            return False

        targets = [0.0]
        while targets[-1] < 0.999 and len(targets) < 20:
            targets.append(min(1.0, targets[-1] + step))

        for attempt, fraction in enumerate(targets, start=1):
            self._checkpoint()
            _log(f"Movendo seletor para {fraction:.0%} (passo {attempt}/{len(targets)}).")
            self._debug_cover_shot(page, "03_antes_drag")
            self._move_seeker(page, fraction)
            self._debug_cover_shot(page, "04_depois_drag")
            present, score, _ = self._preview_has_banner(page)
            self._debug_cover_shot(page, "05_previa_analisada")
            if present:
                _log(f"BANNER ENCONTRADO na prévia ({fraction:.0%}, score={score:.2f}).")
                self._debug_cover_shot(page, "06_capa_encontrada")
                return True
            _log(f"Banner NÃO encontrado em {fraction:.0%}; aguardando próxima decisão.")

        _err(f"Banner não encontrado após {len(targets)} tentativas.")
        return False

    def _find_cover_strip(self, page):
        for css in COVER_RANGE_INPUT_CSS:
            try:
                loc = page.locator(css).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue

        try:
            for aria in COVER_SLIDER_ROLE_TEXTS:
                loc = page.get_by_role("slider", name=aria, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
        except Exception:
            pass

        try:
            loc = page.locator('div[role="dialog"] [role="slider"]').first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            pass

        for css in COVER_STRIP_CSS:
            try:
                loc = page.locator(css).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue

        try:
            dlg = page.locator(DIALOG_CSS).first
            if dlg.count() > 0 and dlg.is_visible():
                dbox = dlg.bounding_box()
                if dbox is None:
                    return None

                rail = None
                for cand in dlg.locator("canvas, img").all():
                    try:
                        if not cand.is_visible():
                            continue
                        box = cand.bounding_box()
                        if box is None:
                            continue
                        w, h = box["width"], box["height"]
                        if w <= 20 or h <= 5:
                            continue
                        wide = w >= max(0.5 * dbox["width"], 2 * h)
                        short = h <= max(120.0, 0.08 * dbox["height"])
                        yc = box["y"] + h / 2.0
                        lower_middle = dbox["y"] + 0.40 * dbox["height"] <= yc <= dbox["y"] + 0.90 * dbox["height"]
                        not_footer = yc <= dbox["y"] + 0.92 * dbox["height"]
                        if wide and short and lower_middle and not_footer:
                            rail = cand
                            if w > 0:
                                break
                    except Exception:
                        continue
                if rail is not None:
                    return rail
        except Exception:
            pass

        return None

    def _find_preview_locator(self, page):
        try:
            dlg = page.locator(DIALOG_CSS).first
            if dlg.count() == 0 or not dlg.is_visible():
                return None
            best = None
            best_area = 0.0
            for candidate in dlg.locator("video, img, canvas").all():
                try:
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                    if box is None:
                        continue
                    area = box["width"] * box["height"]
                    if area > best_area:
                        best_area = area
                        best = candidate
                except Exception:
                    continue
            return best
        except Exception:
            return None

    def _read_slider_attrs(self, page) -> dict:
        for css in COVER_RANGE_INPUT_CSS:
            try:
                loc = page.locator(css).first
                if loc.count() > 0 and loc.is_visible():
                    return {
                        "tipo": "input_range",
                        "min": loc.get_attribute("min"),
                        "max": loc.get_attribute("max"),
                        "value": loc.get_attribute("value"),
                    }
            except Exception:
                continue
        for aria in COVER_SLIDER_ROLE_TEXTS:
            try:
                loc = page.get_by_role("slider", name=aria, exact=False).first
                if loc.count() > 0 and loc.is_visible():
                    return {
                        "tipo": "role_slider",
                        "min": loc.get_attribute("aria-valuemin"),
                        "max": loc.get_attribute("aria-valuemax"),
                        "value": loc.get_attribute("aria-valuenow"),
                    }
            except Exception:
                continue
        try:
            loc = page.locator('div[role="dialog"] [role="slider"]').first
            if loc.count() > 0 and loc.is_visible():
                return {
                    "tipo": "role_slider",
                    "min": loc.get_attribute("aria-valuemin"),
                    "max": loc.get_attribute("aria-valuemax"),
                    "value": loc.get_attribute("aria-valuenow"),
                }
        except Exception:
            pass
        return {}

    def _dump_cover_dom(self, page) -> None:
        log = get_logger()
        try:
            dlg = page.locator(DIALOG_CSS).first
            if dlg.count() == 0 or not dlg.is_visible():
                log.info("[CAPA] dump: nenhum diálogo visível.")
                return
        except Exception:
            log.info("[CAPA] dump: falha ao inspecionar diálogo.")
            return

        def _describe(css, label):
            try:
                loc = page.locator(css)
                n = loc.count()
            except Exception:
                return
            for i in range(min(n, 10)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    tag = ""
                    role = ""
                    aria = ""
                    try:
                        tag = el.evaluate("(e) => e.tagName.toLowerCase()")
                    except Exception:
                        pass
                    try:
                        role = el.get_attribute("role") or ""
                    except Exception:
                        pass
                    try:
                        aria = (el.get_attribute("aria-label") or "")[:40]
                    except Exception:
                        pass
                    log.info(
                        f"[CAPA] dump {label}[{i}]: tag={tag} role={role} "
                        f"aria='{aria}' box={box and [round(box['x']), round(box['y']), round(box['width']), round(box['height'])]}"
                    )
                except Exception:
                    continue

        for css, label in [
            ('div[role="dialog"] input[type="range"]', "range"),
            ('div[role="dialog"] [role="slider"]', "slider"),
            ('div[role="dialog"] canvas', "canvas"),
            ('div[role="dialog"] img', "img"),
            ('div[role="dialog"] [aria-valuenow]', "valuenow"),
            ('div[role="dialog"] [aria-label*="cover" i]', "aria_cover"),
            ('div[role="dialog"] [aria-label*="capa" i]', "aria_capa"),
            ('div[role="dialog"] [aria-label*="frame" i]', "aria_frame"),
        ]:
            _describe(css, label)

        attrs = self._read_slider_attrs(page)
        if attrs:
            log.info(f"[CAPA] dump estado slider: {attrs}")

    def _capture_preview_sig(self, page):
        try:
            import cv2
            import numpy as np

            preview = self._find_preview_locator(page)
            if preview is None:
                return None
            shot_bytes = preview.screenshot()
            if not shot_bytes:
                return None
            shot = cv2.imdecode(np.frombuffer(shot_bytes, np.uint8), cv2.IMREAD_COLOR)
            if shot is None or shot.size == 0:
                return None

            h, w = shot.shape[:2]
            c = 0.82
            y0, y1 = int(h * (1 - c) / 2), int(h * (1 + c) / 2)
            x0, x1 = int(w * (1 - c) / 2), int(w * (1 + c) / 2)
            cropped = shot[max(0, y0):max(y0 + 1, y1), max(0, x0):max(x0 + 1, x1)]
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY) if cropped.ndim == 3 else cropped
            return cv2.resize(gray, (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
        except Exception:
            return None

    def _sig_similar(self, a, b, threshold: float = 0.93) -> bool | None:
        if a is None or b is None:
            return None
        try:
            import numpy as np

            a_flat = a.ravel()
            b_flat = b.ravel()
            if np.std(a_flat) < 1e-6 or np.std(b_flat) < 1e-6:
                return None
            corr = float(np.corrcoef(a_flat, b_flat)[0, 1])
            return corr >= threshold
        except Exception:
            return None

    def _set_range_value_via_dom(self, page, fraction: float) -> bool:
        for css in COVER_RANGE_INPUT_CSS:
            try:
                loc = page.locator(css).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                cur = loc.evaluate(
                    """(el) => ({
                        min: parseFloat(el.min || 0),
                        max: parseFloat(el.max || 100),
                        step: parseFloat(el.step) || (el.max ? (parseFloat(el.max)-parseFloat(el.min||0))/100 : 1),
                        value: parseFloat(el.value)
                    })"""
                )
                if cur is None or cur.get("max") is None:
                    continue
                f = max(0.0, min(1.0, fraction))
                target = cur["min"] + (cur["max"] - cur["min"]) * f
                loc.evaluate(
                    """(el, target) => {
                        el.focus();
                        el.value = String(target);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('pointerdown', {bubbles: true}));
                        el.dispatchEvent(new Event('pointerup', {bubbles: true}));
                    }""",
                    target,
                )
                page.wait_for_timeout(400)
                return True
            except Exception:
                continue
        return False

    def _set_aria_slider_via_dom(self, page, fraction: float) -> bool:
        for aria in COVER_SLIDER_ROLE_TEXTS:
            try:
                loc = page.get_by_role("slider", name=aria, exact=False).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                cur = loc.evaluate(
                    """(el) => ({
                        min: el.getAttribute('aria-valuemin'),
                        max: el.getAttribute('aria-valuemax'),
                        now: el.getAttribute('aria-valuenow')
                    })"""
                )
                if cur is None:
                    continue
                vmin = _to_float(cur.get("min"))
                vmax = _to_float(cur.get("max"))
                if vmin is None:
                    vmin = 0.0
                if vmax is None:
                    vmax = 100.0
                f = max(0.0, min(1.0, fraction))
                target = vmin + (vmax - vmin) * f
                loc.evaluate(
                    """(el, target) => {
                        el.focus();
                        el.setAttribute('aria-valuenow', String(target));
                        if ('value' in el) el.value = String(target);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }""",
                    target,
                )
                page.wait_for_timeout(400)
                return True
            except Exception:
                continue
        return False

    def _click_cover_at(self, page, x, y) -> None:
        page.mouse.move(x, y)
        page.wait_for_timeout(60)
        page.mouse.down()
        page.wait_for_timeout(60)
        page.mouse.up()

    def _drag_cover_to(self, page, box, fraction: float, start_fraction: float) -> bool:
        log = get_logger()
        try:
            left = box["x"] + box["width"] * 0.02
            right = box["x"] + box["width"] * 0.98
            y = box["y"] + box["height"] / 2.0
            start_x = left + (right - left) * max(0.0, min(1.0, start_fraction))
            target_x = left + (right - left) * max(0.0, min(1.0, fraction))
            log.info(
                f"[CAPA] Drag real: start=({start_x:.0f},{y:.0f}) target=({target_x:.0f},{y:.0f})"
            )
            page.mouse.move(start_x, y)
            page.wait_for_timeout(120)
            page.mouse.down()
            steps = max(8, int(abs(target_x - start_x) / 8.0))
            for i in range(1, steps + 1):
                page.mouse.move(start_x + (target_x - start_x) * (i / steps), y)
                page.wait_for_timeout(25)
            page.mouse.up()
            page.wait_for_timeout(1400)
            return True
        except Exception as e:
            log.info(f"[CAPA][ERRO] Drag real falhou: {e}")
            return False

    def _seek_cover_to(self, page, fraction: float, prev_sig=None) -> tuple[bool, str]:
        log = get_logger()
        strip = self._find_cover_strip(page)
        if strip is None:
            return False, "sem_slider"
        try:
            if not strip.is_visible():
                return False, "seletor_invisivel"
        except Exception:
            return False, "seletor_invisivel"
        try:
            box = strip.bounding_box()
            if box is None or box["width"] <= 0 or box["height"] <= 0:
                return False, "sem_bounding_box"
            if box["width"] < 100:
                parent_box = strip.locator("..").bounding_box()
                if parent_box is not None and parent_box["width"] > box["width"]:
                    box = parent_box
        except Exception:
            return False, "sem_bounding_box"

        log.info(
            f"[CAPA] Timeline box: x={box['x']:.0f} y={box['y']:.0f} "
            f"width={box['width']:.0f} height={box['height']:.0f}"
        )

        attrs_before = self._read_slider_attrs(page)
        current = _to_float(attrs_before.get("value"))
        minimum = _to_float(attrs_before.get("min"))
        maximum = _to_float(attrs_before.get("max"))
        if current is not None and maximum is not None and maximum > (minimum or 0.0):
            start_fraction = (current - (minimum or 0.0)) / (maximum - (minimum or 0.0))
        else:
            start_fraction = 0.0

        frac = max(0.0, min(1.0, fraction))
        log.info(f"[CAPA] Posição atual={start_fraction:.1%}; destino={frac:.1%}")

        try:
            log.info("[CAPA] Drag iniciado.")
            if not self._drag_cover_to(page, box, frac, start_fraction):
                return False, "drag_falhou"
            log.info("[CAPA] Drag finalizado; seletor solto.")
            self._wait_preview_stable(page)
            attrs_after = self._read_slider_attrs(page)
            selector_changed = attrs_before != attrs_after
            cur = self._capture_preview_sig(page)
            preview_changed = self._movement_desc(prev_sig, cur) == "previa_mudou"
            if selector_changed or preview_changed or abs(frac - start_fraction) < 0.01:
                log.info(
                    f"[CAPA] Movimento confirmado: seletor={'mudou' if selector_changed else 'inalterado'}, "
                    f"prévia={'mudou' if preview_changed else 'inalterada'}."
                )
                return True, "drag_real"
            log.info("[CAPA][ERRO] O seletor não se movimentou e a prévia não mudou.")
            return False, "seletor_inalterado"
        except Exception as e:
            log.info(f"[CAPA][ERRO] Erro ao posicionar seletor: {e}")
            return False, "mouse_erro"

    def _movement_desc(self, before, after) -> str:
        sim = self._sig_similar(before, after)
        if sim is None:
            return "sem_confirmacao_visual"
        return "previa_mudou" if not sim else "previa_inerte"

    def _find_text_button(self, page, texts, timeout_s: float = 10.0):
        rx = _texts_regex(texts)
        deadline = time.monotonic() + timeout_s
        while True:
            scopes = [page]
            try:
                dlg = page.locator(DIALOG_CSS).first
                if dlg.is_visible():
                    scopes.insert(0, dlg)
            except Exception:
                pass
            for scope in scopes:
                try:
                    loc = scope.get_by_role("button", name=rx).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass
                try:
                    loc = scope.get_by_text(rx).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                break
            self._checkpoint()
            page.wait_for_timeout(450)
        raise PublishStepError(
            "elemento_nao_encontrado",
            f"Botão/elemento não localizado na página: {' / '.join(texts[:3])}...",
        )

    def _peek_button(self, page, texts):
        try:
            return self._find_text_button(page, texts, timeout_s=0.5)
        except PublishStepError:
            return None

    def _process_and_reach_caption(self, page, video_name: str, video_path: str | None = None) -> None:
        log = get_logger()
        self.emit("current_video", stage="Aguardando processamento do vídeo")

        deadline = time.monotonic() + float(self.cfg.upload_timeout_s)
        ready_since = None
        next_btn = None

        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                cur = page.url or ""
                if "instagram" not in cur:
                    self._screenshot(page, "interferencia", "usuario_navegou_processamento")
                    raise PublishStepError(
                        "interferencia_usuario",
                        "Fluxo interrompido durante o processamento: o navegador saiu do "
                        "Instagram (sinal de interferência do usuário).",
                    )
            except PublishStepError:
                raise
            except Exception as exc:
                if _is_target_closed_error(exc):
                    raise PublishStepError(
                        "interferencia_usuario",
                        "A página foi fechada durante o processamento do vídeo.",
                    ) from exc
                raise PublishStepError(
                    "interferencia_usuario",
                    f"O fluxo foi alterado durante o processamento: {exc}",
                ) from exc
            if description_mod.caption_present(page):
                log.info("Tela final detectada diretamente")
                return
            nb = self._peek_button(page, NEXT_BUTTON_TEXTS)
            if nb is not None and self._is_clickable(nb):
                if ready_since is None:
                    ready_since = time.monotonic()
                elif time.monotonic() - ready_since >= 1.2:
                    next_btn = nb
                    break
            else:
                ready_since = None
            page.wait_for_timeout(600)

        if next_btn is None:
            self.emit("current_video", stage="Aguardando processamento do vídeo")
            raise PublishStepError(
                "processamento",
                f"O upload/processamento não foi concluído em {int(self.cfg.upload_timeout_s)}s.",
            )
        log.info("Upload concluído")

        for _ in range(4):
            self._checkpoint()
            if description_mod.caption_present(page):
                break
            nb = self._peek_button(page, NEXT_BUTTON_TEXTS)
            if nb is None:
                break
            try:
                nb.click(timeout=8000)
            except Exception as e:
                raise PublishStepError("avancar_etapa", f"Falha ao clicar em '{NEXT_BUTTON_TEXTS[0]}': {e}") from e
            self._small_delay(page)

        try:
            description_mod.find_caption(page, timeout_s=float(self.cfg.step_timeout_s))
        except description_mod.DescriptionError as e:
            raise PublishStepError("localizar_descricao", str(e)) from e
        log.info("Campo de descrição encontrado")

    def _fill_caption(self, page, video_name: str, custom_description: str = None,
                      custom_hashtags: str = None) -> None:
        default_desc = self.sm.state.description or ""
        allow_empty = bool(self.sm.state.allow_no_description)

        parts = []
        if custom_description:
            parts.append(custom_description)
        if custom_hashtags:
            parts.append(custom_hashtags)
        if default_desc:
            parts.append(default_desc)
        expected = "\n\n".join(parts)

        try:
            value = description_mod.fill_description(page, expected)
        except description_mod.DescriptionError as e:
            raise PublishStepError("inserir_descricao", str(e)) from e

        page.wait_for_timeout(500)
        value = value or ""
        if not description_mod.verify_filled(value, expected, allow_empty):
            preview = (value[:80] + "...") if len(value) > 80 else value
            raise PublishStepError(
                "verificar_descricao",
                f"A descrição lida na tela não confere com a configurada. Lido: {preview!r}",
            )
        get_logger().info("Descrição inserida e verificada")

    def _click_share(self, page) -> None:
        share = self._find_text_button(page, SHARE_BUTTON_TEXTS, timeout_s=12)
        deadline = time.monotonic() + 45
        while not self._is_clickable(share):
            if time.monotonic() >= deadline:
                raise PublishStepError(
                    "botao_publicar", "O botão Publicar permaneceu indisponível."
                )
            self._checkpoint()
            page.wait_for_timeout(600)
        self._small_delay(page)
        share.click(timeout=10000)

    def wait_until_post_published(self, page) -> None:
        log = get_logger()
        deadline = time.monotonic() + float(self.cfg.publish_confirm_timeout_s)

        while time.monotonic() < deadline:
            self._checkpoint()
            current_url = (page.url or "").lower()
            in_create_flow = "/create" in current_url or "instagram.com/create" in current_url

            try:
                t = page.get_by_text(_SUCCESS_RX).first
                if t.count() > 0 and t.is_visible() and not in_create_flow:
                    log.info("Confirmação de sucesso exibida pelo Instagram")
                    page.wait_for_timeout(1500)
                    self._click_conclude_if_present(page)
                    page.wait_for_timeout(2000)
                    self._verify_post_completed(page)
                    return
            except Exception as exc:
                if _is_target_closed_error(exc):
                    raise PublishStepError(
                        "interferencia_usuario",
                        "A página foi fechada enquanto aguardava a confirmação.",
                    ) from exc
                log.warning(f"Interrupção detectada durante a confirmação: {exc}")

            try:
                et = page.get_by_text(_ERROR_TOAST_RX).first
                if et.count() > 0 and et.is_visible():
                    msg = (et.inner_text() or "")[:120].strip()
                    raise PublishStepError("confirmacao", f"Instagram relatou erro: {msg}")
            except PublishStepError:
                raise
            except Exception:
                pass

            try:
                dlg = page.locator(DIALOG_CSS)
                if dlg.count() == 0 or not dlg.first.is_visible():
                    page.wait_for_timeout(2000)
                    if dlg.count() == 0 or not dlg.first.is_visible():
                        if in_create_flow:
                            log.info("Ainda no fluxo de criação; aguardando saída do diálogo antes de confirmar")
                        else:
                            log.info("Diálogo de criação encerrado; publicação aceita")
                            self._click_conclude_if_present(page)
                            page.wait_for_timeout(2000)
                            self._verify_post_completed(page)
                            return
            except Exception:
                pass

            page.wait_for_timeout(700)

        raise PublishStepError(
            "confirmacao",
            f"A confirmação da publicação não foi detectada em {int(self.cfg.publish_confirm_timeout_s)}s.",
        )

    def _verify_post_completed(self, page) -> None:
        log = get_logger()
        try:
            current_url = page.url or ""
            if "instagram.com" in current_url and "/create" not in current_url:
                log.info(f"Publicação confirmada; página atual ({current_url})")
            else:
                log.info(f"URL pós-publicação: {current_url}")
        except Exception as e:
            log.warning(f"Erro na verificação pós-publicação: {e}")

        self._screenshot(page, "confirmacao", "publicacao_confirmada")
        log.info("Screenshot de confirmação salvo")

    def _click_conclude_if_present(self, page) -> None:
        log = get_logger()
        CONCLUDE = ["Concluir", "Done", "OK", "Close", "Fechar", "Aceptar", "Fertig"]
        rx = _texts_regex(CONCLUDE)
        try:
            page.wait_for_timeout(800)
            for scope in [page.locator(DIALOG_CSS), page]:
                try:
                    btn = scope.get_by_role("button", name=rx).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=5000)
                        log.info("Botão 'Concluir' clicado no diálogo de sucesso")
                        page.wait_for_timeout(800)
                        return
                except Exception:
                    pass
                try:
                    btn = scope.get_by_text(rx).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=5000)
                        log.info("Texto 'Concluir' clicado no diálogo de sucesso")
                        page.wait_for_timeout(800)
                        return
                except Exception:
                    pass
        except Exception:
            pass

    def _recover(self, page) -> None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)

            for _ in range(2):
                dlg_visible = False
                try:
                    dlg = page.locator(DIALOG_CSS)
                    dlg_visible = dlg.count() > 0 and dlg.first.is_visible()
                except Exception:
                    pass
                if not dlg_visible:
                    break
                discard = self._peek_button(page, DISCARD_TEXTS)
                if discard is not None:
                    try:
                        discard.click(timeout=4000)
                        page.wait_for_timeout(900)
                    except Exception:
                        break
                else:
                    break

            try:
                dlg = page.locator(DIALOG_CSS)
                if dlg.count() > 0 and dlg.first.is_visible():
                    page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
            except Exception:
                pass
        except CancelledRequest:
            raise
        except Exception:
            try:
                page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
            except Exception:
                pass

    def _wait_manual(self, page, reason: str) -> None:
        log = get_logger()
        log.warning(f"ACAO MANUAL NECESSARIA: {reason}")
        self.emit("manual_required", active=True, message=reason)
        try:
            while True:
                if self.cancel_event.is_set():
                    raise CancelledRequest()
                while self.pause_event.is_set():
                    if self.cancel_event.is_set():
                        raise CancelledRequest()
                    time.sleep(0.2)

                ok = False
                try:
                    ok = (
                        has_session_cookie(self._ctx)
                        and not login_form_visible(page)
                        and not any(m in (page.url or "").lower() for m in CHALLENGE_URL_MARKERS)
                    )
                except Exception:
                    ok = False
                if ok:
                    break
                time.sleep(2.5)
        finally:
            self.emit("manual_required", active=False, message="")
            log.info("Intervenção manual concluída; retomando o processo")
