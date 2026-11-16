"""One machine-readable output shape for every read-only CLI command.

An operator driving this CLI from a script — or a language model driving it
from a shell — otherwise has to parse column-aligned prose with regular
expressions, which breaks the moment a label is reworded. `--json` exists so
that reading the local state never depends on how it is spelled (ADR-B095).

Nothing here decides *what* a command may disclose. Each command passes the
same values its human output already shows, so the two shapes can never
diverge on privacy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import typer


def _plain(value: Any) -> Any:
    """Return a JSON-safe copy of one value, without inventing precision."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        # A Decimal is money or a rate; a float would silently round it.
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    return value


def emit_json(payload: Any) -> None:
    """Print one canonical JSON document for a read-only command."""
    typer.echo(
        json.dumps(_plain(payload), ensure_ascii=False, indent=2, sort_keys=True)
    )
