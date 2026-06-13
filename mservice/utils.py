import os
import sys
from typing import Optional


COLORS = {
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "bright_black": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
    "bright_white": "97",
}


def colorize(text: str, color: str, bold: bool = False) -> str:
    if not sys.stdout.isatty():
        return text
    code = COLORS.get(color.lower(), "37")
    bold_code = "1;" if bold else ""
    return f"\033[{bold_code}{code}m{text}\033[0m"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def expand_env(env: dict, base_env: Optional[dict] = None) -> dict:
    result = dict(base_env or os.environ)
    for k, v in env.items():
        result[k] = str(v)
    return result
