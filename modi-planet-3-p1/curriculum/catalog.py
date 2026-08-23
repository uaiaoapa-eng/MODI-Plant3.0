"""Read-only access to the P1 curriculum selection shell."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any


_CATALOG_PATH = Path(__file__).with_name("catalog.json")


def _read_catalog() -> dict[str, Any]:
    with _CATALOG_PATH.open(encoding="utf-8") as catalog_file:
        return json.load(catalog_file)


def list_grade_bands() -> list[dict[str, Any]]:
    """Return detached catalog data so callers cannot mutate process state."""
    return deepcopy(_read_catalog()["grade_bands"])


def get_grade_band(grade_band: str) -> dict[str, Any] | None:
    for band in list_grade_bands():
        if band["id"] == grade_band:
            return band
    return None
