"""Secret access.

Secrets are read from the process environment (optionally seeded from a `.env`
file) and are never sourced from YAML, never logged, and never serialised into
error messages. `SecretStore` is the only place that reads them, so there is one
audited surface rather than `os.environ` calls scattered through adapters.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Prefix for pulpmill-owned variables. Third-party SDK variables that we do not
#: control (ANTHROPIC_API_KEY) are read under their conventional names.
ENV_PREFIX = "PULPMILL_"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines from a `.env` file.

    A tiny deliberate implementation instead of a dependency. Supports comments,
    blank lines, `export ` prefixes and single/double quoting. It does not
    support interpolation -- a `$` in a password should stay a `$`.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path, environ: dict[str, str] | None = None) -> int:
    """Seed the environment from `path` without overwriting existing variables.

    Real environment variables win, so `PULPMILL_X=1 pulpmill run` overrides
    `.env`. Returns the number of variables set. Missing file is not an error.
    """
    target = environ if environ is not None else os.environ
    if not path.is_file():
        return 0
    applied = 0
    for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
        if key not in target:
            target[key] = value
            applied += 1
    return applied


@dataclass(frozen=True, slots=True)
class SecretStore:
    """Read-only view over secret environment variables.

    Deliberately has no `__str__`/`__repr__` that could dump values: it holds a
    reference to the environment mapping rather than copies of the secrets.
    """

    environ: Mapping[str, str]

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> SecretStore:
        return cls(environ if environ is not None else os.environ)

    def get(self, name: str, *, prefixed: bool = True) -> str | None:
        """Return a secret, or None when unset or blank.

        Blank is treated as unset because a `.env` copied from `.env.example`
        leaves empty values everywhere; those must not read as "configured".
        """
        key = f"{ENV_PREFIX}{name}" if prefixed else name
        value = self.environ.get(key)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def require(self, name: str, *, prefixed: bool = True) -> str:
        value = self.get(name, prefixed=prefixed)
        if value is None:
            key = f"{ENV_PREFIX}{name}" if prefixed else name
            raise KeyError(f"required secret {key} is not set")
        return value

    def has(self, name: str, *, prefixed: bool = True) -> bool:
        return self.get(name, prefixed=prefixed) is not None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SecretStore(<{len(self.environ)} environment variables>)"
