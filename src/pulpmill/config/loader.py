"""Configuration loading.

Layering, lowest precedence first:

1. `config/pipeline.yaml`               -- committed defaults
2. `config/pipeline.local.yaml`         -- git-ignored local overrides
3. `$PULPMILL_CONFIG`                   -- explicit extra file
4. A small set of scalar environment overrides (data dir, log level)

Secrets never participate: they are read separately by `SecretStore`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from pulpmill.config.models import AppConfig, deep_merge
from pulpmill.config.secrets import load_env_file
from pulpmill.domain.errors import ConfigError

DEFAULT_CONFIG_RELPATH = Path("config/pipeline.yaml")
LOCAL_CONFIG_RELPATH = Path("config/pipeline.local.yaml")
ENV_FILE_RELPATH = Path(".env")

#: Environment variables that override single config values. Kept short on
#: purpose -- config belongs in YAML; this is for per-invocation overrides.
_SCALAR_ENV_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "PULPMILL_DATA_DIR": ("runtime", "data_dir"),
    "PULPMILL_LOG_LEVEL": ("runtime", "logging", "level"),
    "PULPMILL_DB_PATH": ("runtime", "database", "path"),
}


def find_project_root(start: Path | None = None) -> Path:
    """Walk upwards looking for the markers that identify the project.

    Lets the CLI work from any subdirectory without hard-coding an absolute
    path anywhere. Falls back to the source tree's own root, so an installed
    copy still finds its config and migrations.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / DEFAULT_CONFIG_RELPATH
        ).is_file():
            return candidate

    # src/pulpmill/config/loader.py -> project root is four levels up.
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / DEFAULT_CONFIG_RELPATH).is_file():
        return source_root
    return current


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("could not read configuration file", path=str(path)) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError("configuration file is not valid YAML", path=str(path)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            "configuration file must contain a YAML mapping at the top level",
            path=str(path),
            found=type(data).__name__,
        )
    return data


def _apply_env_overrides(data: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    for env_name, path in _SCALAR_ENV_OVERRIDES.items():
        value = environ.get(env_name)
        if value is None or not value.strip():
            continue
        cursor = data
        for key in path[:-1]:
            nested = cursor.get(key)
            if not isinstance(nested, dict):
                nested = {}
                cursor[key] = nested
            cursor = nested
        cursor[path[-1]] = value.strip()
    return data


def _format_validation_error(exc: ValidationError, sources: list[Path]) -> str:
    lines = [f"configuration is invalid ({exc.error_count()} problem(s)):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    lines.append("checked: " + ", ".join(str(path) for path in sources))
    return "\n".join(lines)


def load_config(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    load_dotenv: bool = True,
) -> AppConfig:
    """Load, layer and validate the application configuration.

    Raises `ConfigError` with every validation problem listed, rather than
    failing on the first one.
    """
    root = (project_root or find_project_root()).resolve()

    # Seed os.environ from .env before reading env-based overrides, so a `.env`
    # entry can drive both secrets and scalar overrides.
    if load_dotenv and environ is None:
        load_env_file(root / ENV_FILE_RELPATH)
    env = environ if environ is not None else os.environ

    layers: list[Path] = []
    base_path = config_path or (root / DEFAULT_CONFIG_RELPATH)
    if not base_path.is_file():
        raise ConfigError("base configuration file not found", path=str(base_path))
    layers.append(base_path)

    if config_path is None:
        local_path = root / LOCAL_CONFIG_RELPATH
        if local_path.is_file():
            layers.append(local_path)

    extra = env.get("PULPMILL_CONFIG", "").strip()
    if extra:
        extra_path = Path(extra).expanduser()
        if not extra_path.is_absolute():
            extra_path = root / extra_path
        if not extra_path.is_file():
            raise ConfigError("PULPMILL_CONFIG points at a missing file", path=str(extra_path))
        layers.append(extra_path)

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = deep_merge(merged, _read_yaml(layer))
    merged = _apply_env_overrides(merged, env)
    merged["project_root"] = root

    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, layers)) from exc
