from __future__ import annotations

from playwright.sync_api import BrowserContext, Playwright

from config import PROFILE1_DIR


class BrowserLaunchError(RuntimeError):
    pass


_CHANNELS = ["chrome", "msedge", None]


def _resolve_channels(channel_cfg: str) -> list:
    c = (channel_cfg or "auto").lower()
    if c == "chrome":
        return ["chrome", None]
    if c == "msedge":
        return ["msedge", None]
    if c == "chromium":
        return [None]
    return list(_CHANNELS)


def launch(cfg, profile_dir=None) -> tuple[Playwright, BrowserContext]:
    pw = sync_playwright_start()
    profile_dir = profile_dir or PROFILE1_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for channel in _resolve_channels(cfg.browser_channel):
        try:
            kwargs: dict = {
                "user_data_dir": str(profile_dir),
                "headless": bool(cfg.headless),
                "viewport": {"width": int(cfg.viewport_width), "height": int(cfg.viewport_height)},
                "locale": cfg.locale,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if channel:
                kwargs["channel"] = channel
            ctx = pw.chromium.launch_persistent_context(**kwargs)
            return pw, ctx
        except Exception as e:
            last_error = e
            continue
    try:
        pw.stop()
    except Exception:
        pass
    raise BrowserLaunchError(
        f"Não foi possível abrir o navegador. Instale o Google Chrome ou execute 'playwright install chromium'. Detalhe: {last_error}"
    )


def sync_playwright_start() -> Playwright:
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def first_page(ctx: BrowserContext):
    if ctx.pages:
        return ctx.pages[0]
    return ctx.new_page()


def stop(pw: Playwright | None, ctx: BrowserContext | None) -> None:
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass
