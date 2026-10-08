#!/usr/bin/env python3
"""Resolve executor models from capabilities, overrides, and live catalogs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


CAPABILITIES = ("fast", "balanced", "hard")
CAPABILITY_ALIASES = {"frontier": "hard"}

EXECUTOR_MODELS: dict[str, dict[str, list[str]]] = {
    "claude": {
        "fast": ["haiku"],
        "balanced": ["sonnet", "claude-sonnet-5"],
        "hard": ["opus", "claude-opus-5"],
    },
    "codex": {
        "fast": ["gpt-5.6-luna"],
        "balanced": ["gpt-5.6-luna"],
        "hard": ["gpt-5.6-sol"],
    },
    "kimi": {
        "fast": ["kimi-code/k3"],
        "balanced": ["kimi-code/k3"],
        "hard": ["kimi-code/k3"],
    },
    "grok": {
        "fast": ["grok-4.5"],
        "balanced": ["grok-4.6", "grok-4.5"],
        "hard": ["grok-4.6", "grok-4.5"],
    },
    "pi": {
        "fast": [],
        "balanced": [],
        "hard": [],
    },
}

_GROK_MODEL = re.compile(r"(?<![A-Za-z0-9._/-])(grok-[A-Za-z0-9][A-Za-z0-9._/-]*)")
_VERSION_PART = re.compile(r"\d+|[A-Za-z]+")


def _validate(executor: str, capability: str) -> str:
    capability = CAPABILITY_ALIASES.get(capability, capability)
    if executor not in EXECUTOR_MODELS:
        raise ValueError(f"unsupported executor for model resolution: {executor}")
    if capability not in CAPABILITIES:
        raise ValueError(f"unsupported level: {capability}")
    return capability


def parse_model_catalog(output: str | bytes) -> list[str]:
    """Return unique Grok model identifiers in the order advertised."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    seen: set[str] = set()
    models: list[str] = []
    for match in _GROK_MODEL.finditer(output):
        model = match.group(1).rstrip(".,:;)]}")
        if model not in seen:
            seen.add(model)
            models.append(model)
    return models


def _catalog_values(catalog: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for item in catalog:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _natural_key(model: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for part in _VERSION_PART.findall(model.removeprefix("grok-")):
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part.lower()))
    return tuple(parts)


def _older_grok_model(catalog: list[str]) -> str | None:
    def version(model: str) -> tuple[int, ...] | None:
        numbers = tuple(int(part) for part in re.findall(r"\d+", model))
        return numbers or None

    compatible = [
        model
        for model in catalog
        if model.startswith("grok-")
        and version(model) is not None
        and version(model) < (4, 5)
    ]
    return max(compatible, key=_natural_key, default=None)


def resolve_model(
    executor: str,
    capability: str,
    override: str | None = None,
    catalog: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Resolve one model and report where the decision came from."""
    capability = _validate(executor, capability)
    catalog_values = None if catalog is None else _catalog_values(catalog)
    if override is not None:
        return {"model": override, "source": "override", "catalog": catalog_values}

    preferred = EXECUTOR_MODELS[executor].get(capability, [])
    if catalog_values is not None:
        for candidate in preferred:
            if candidate in catalog_values:
                return {
                    "model": candidate,
                    "source": "discovered",
                    "catalog": catalog_values,
                }
        if executor == "grok" and preferred:
            older = _older_grok_model(catalog_values)
            if older is not None:
                return {
                    "model": older,
                    "source": "discovered",
                    "catalog": catalog_values,
                }
        return {"model": None, "source": "inherited", "catalog": catalog_values}

    if not preferred:
        return {"model": None, "source": "inherited", "catalog": None}
    return {
        "model": preferred[0],
        "source": "alias" if executor == "claude" else "preferred",
        "catalog": None,
    }


def resolve_effort(
    executor: str, capability: str, override: str | None = None
) -> str:
    """Resolve real runner effort defaults without inventing unsupported knobs."""
    _validate(executor, capability)
    if override is not None:
        return override
    if executor == "codex":
        return "xhigh"
    if executor == "claude":
        return "high"
    return "inherited"
