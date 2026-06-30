from __future__ import annotations

import os
from pathlib import Path


DEFAULT_ENV_FILES = (".env", ".env.example")


def load_env_files(base_dir: Path | None = None, filenames: tuple[str, ...] = DEFAULT_ENV_FILES) -> list[Path]:
    base_dir = base_dir or Path.cwd()
    loaded: list[Path] = []
    for filename in filenames:
        path = base_dir / filename
        if path.exists():
            load_env_file(path)
            loaded.append(path)
    return loaded


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_inline_comment(value.strip())
        value = strip_quotes(value)
        if key and key not in os.environ:
            os.environ[key] = value


def strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return value[:index].strip()
    return value


def strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
