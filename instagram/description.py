from __future__ import annotations

import time

from playwright.sync_api import Locator, Page

from instagram.selectors import CAPTION_ARIA_LABELS, CAPTION_CSS_BY_LABEL, CAPTION_GENERIC_CSS


class DescriptionError(RuntimeError):
    pass


def caption_present(page: Page) -> bool:
    return _locate_caption(page, quick=True) is not None


def find_caption(page: Page, timeout_s: float = 20.0) -> Locator:
    deadline = time.monotonic() + timeout_s
    while True:
        loc = _locate_caption(page, quick=True)
        if loc is not None:
            return loc
        if time.monotonic() >= deadline:
            raise DescriptionError("Campo de descrição/legenda não foi encontrado na tela.")
        page.wait_for_timeout(500)


def _locate_caption(page: Page, quick: bool = False):
    for label in CAPTION_ARIA_LABELS:
        for css in CAPTION_CSS_BY_LABEL:
            try:
                loc = page.locator(css.format(label=label)).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
    for css in CAPTION_GENERIC_CSS:
        try:
            loc = page.locator(css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def read_value(loc: Locator) -> str:
    try:
        return loc.input_value()
    except Exception:
        pass
    try:
        return loc.inner_text() or ""
    except Exception:
        return ""


def fill_description(page: Page, text: str) -> str:
    if not text.strip():
        return read_value(_locate_caption(page) or find_caption(page))

    loc = find_caption(page)
    try:
        loc.click(timeout=5000)
    except Exception:
        pass

    filled = False
    try:
        loc.fill(text)
        filled = True
    except Exception:
        filled = False

    if not filled:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        except Exception:
            pass
        try:
            press = getattr(loc, "press_sequentially", None)
            if press is not None:
                press(text, delay=20)
            else:
                loc.type(text, delay=20)
        except Exception as e:
            raise DescriptionError(f"Não foi possível digitar a descrição: {e}") from e

    page.wait_for_timeout(600)
    return read_value(loc)


def verify_filled(value: str, expected: str, allow_empty: bool) -> bool:
    got = (value or "").replace("\r\n", "\n").strip()
    want = (expected or "").replace("\r\n", "\n").strip()
    if not want:
        return allow_empty or got == ""
    return got == want
