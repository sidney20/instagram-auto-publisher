from __future__ import annotations

import time

from playwright.sync_api import BrowserContext, Page

from core.logger import get_logger
from instagram.selectors import (
    CHALLENGE_URL_MARKERS,
    INSTAGRAM_HOME_URL,
    LOGIN_INPUT_CSS,
    SESSION_COOKIE_NAME,
)


class LoginTimeout(RuntimeError):
    pass


class ManualActionRequired(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.user_message = message


def has_session_cookie(ctx: BrowserContext) -> bool:
    try:
        cookies = ctx.cookies("https://www.instagram.com")
        return any(c.get("name") == SESSION_COOKIE_NAME and c.get("value") for c in cookies)
    except Exception:
        return False


def login_form_visible(page: Page) -> bool:
    try:
        loc = page.locator(LOGIN_INPUT_CSS).first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def looks_logged_in(ctx: BrowserContext, page: Page) -> bool:
    try:
        if not has_session_cookie(ctx):
            return False
        page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        assert_no_challenge(page)
        return not login_form_visible(page)
    except ManualActionRequired:
        return False
    except Exception:
        return False


def assert_no_challenge(page: Page) -> None:
    url = (page.url or "").lower()
    if any(marker in url for marker in CHALLENGE_URL_MARKERS):
        raise ManualActionRequired(
            "O Instagram solicitou verificação de segurança/login. Resolva na janela do navegador."
        )


def ensure_logged_in(ctx: BrowserContext, page: Page, cfg, notify=None, gate=None) -> None:
    log = get_logger()
    log.info("Abrindo Instagram e verificando sessão...")
    page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    assert_no_challenge(page)

    if has_session_cookie(ctx) and not login_form_visible(page):
        log.info("Sessão encontrada")
        return

    log.warning("Sessão não encontrada. Faça login manualmente na janela do navegador.")
    if notify:
        notify(True)

    deadline = time.monotonic() + float(cfg.login_wait_timeout_s)
    try:
        while time.monotonic() < deadline:
            if gate is not None:
                gate()
            if has_session_cookie(ctx) and not login_form_visible(page):
                break
            url_ok = not any(m in (page.url or "").lower() for m in CHALLENGE_URL_MARKERS)
            if url_ok and has_session_cookie(ctx):
                break
            page.wait_for_timeout(2500)
        else:
            raise LoginTimeout(
                "Tempo esgotado aguardando o login manual no Instagram."
            )
    finally:
        if notify:
            notify(False)

    page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    assert_no_challenge(page)
    if login_form_visible(page):
        raise LoginTimeout("Login não foi concluído corretamente.")
    log.info("Login confirmado")
