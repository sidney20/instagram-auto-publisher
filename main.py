from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import LOGS_DIR, STATE_FILE_1, STATE_FILE_2, AppConfig, ensure_dirs
from core.logger import setup_logger
from core.state_manager import StateManager


def main() -> None:
    ensure_dirs()
    cfg = AppConfig.load()
    logger, gui_queue = setup_logger(LOGS_DIR, cfg.log_level)
    logger.info("Programa iniciado (modo 2 perfis)")

    from gui.app import App

    state_managers = [StateManager(STATE_FILE_1), StateManager(STATE_FILE_2)]
    app = App(cfg, gui_queue, state_managers)
    try:
        app.mainloop()
    finally:
        logger.info("Programa encerrado")


if __name__ == "__main__":
    main()
