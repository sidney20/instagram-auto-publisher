from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
ERRORS_DIR = LOGS_DIR / "errors"
PROFILE1_DIR = BASE_DIR / "browser_profile_perfil1"
PROFILE2_DIR = BASE_DIR / "browser_profile_perfil2"

STATE_FILE_1 = DATA_DIR / "state_perfil1.json"
STATE_FILE_2 = DATA_DIR / "state_perfil2.json"
CONFIG_FILE = DATA_DIR / "config.json"

PROFILE_COUNT = 2

APP_VERSION = "1.0.0"


DEFAULTS: dict = {
    "profile1_folder": "",
    "profile2_folder": "",
    "profile1_description": "",
    "profile2_description": "",
    "profile1_config_height": 0,
    "profile2_config_height": 0,
    "require_description": True,
    "max_attempts": 4,
    "headless": False,
    "browser_channel": "auto",
    "viewport_width": 1280,
    "viewport_height": 640,
    "locale": "pt-BR",
    "action_delay_min_ms": 300,
    "action_delay_max_ms": 900,
    "delay_between_videos_s": 5.0,
    "publish_mode": "both",
    "autopilot_speed": "normal",
    "upload_timeout_s": 900,
    "step_timeout_s": 120,
    "publish_confirm_timeout_s": 180,
    "login_wait_timeout_s": 1200,
    "stop_on_error": False,
    "log_level": "INFO",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, ERRORS_DIR, PROFILE1_DIR, PROFILE2_DIR):
        d.mkdir(parents=True, exist_ok=True)


class AppConfig(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @classmethod
    def load(cls) -> "AppConfig":
        ensure_dirs()
        cfg = cls(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if k in DEFAULTS:
                        cfg[k] = v
            except Exception:
                pass
        return cfg

    def save(self) -> None:
        ensure_dirs()
        data = {k: self.get(k, DEFAULTS[k]) for k in DEFAULTS}
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
