"""JSON-safe serialization for MCP tool return values.

The stockscan service layer speaks ``Decimal``, ``date``/``datetime``, frozen
dataclasses, SQLAlchemy ``Row`` objects, enums, and numpy scalars. MCP tool
results have to be plain JSON, so every tool funnels its output through
:func:`jsonable`, which recursively normalizes those types. Keeping this in one
place means no tool has to remember the conversion rules.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from decimal import Decimal
from enum import Enum
from typing import Any


def jsonable(obj: Any) -> Any:
    """Recursively convert ``obj`` into JSON-serializable primitives.

    Handles None/bool/int/float/str as-is; Decimal -> float; date/datetime/time
    -> ISO string; Enum -> value; SQLAlchemy Row -> dict; pydantic model ->
    dict; dataclass -> dict; set/tuple -> list; numpy scalar/array -> python.
    Anything unrecognized falls back to ``str(obj)`` so a tool never crashes on
    serialization.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return jsonable(obj.value)

    # SQLAlchemy Row / RowMapping
    mapping = getattr(obj, "_mapping", None)
    if mapping is not None:
        return {str(k): jsonable(v) for k, v in dict(mapping).items()}

    # pydantic v2 models
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return jsonable(model_dump(mode="python"))
        except Exception:  # fall through to other strategies
            pass

    # dataclass instances (not the class itself)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}

    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]

    # numpy scalars / arrays (without importing numpy)
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return jsonable(tolist())
        except Exception:
            pass
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return jsonable(item())
        except Exception:
            pass

    return str(obj)
