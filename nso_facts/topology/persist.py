"""Persist canonical static topology under state/."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import Settings
from nso_facts.topology.devices import nodes_from_device_names
from nso_facts.topology.graph import (
    empty_static_layers,
    summarize_static_physical,
    summarize_static_routing,
    summarize_static_services,
    summarize_static_underlay,
)


STATIC_FILENAME = "topology.static.json"


def static_path(state_dir: Path) -> Path:
    return state_dir / STATIC_FILENAME


def load_static_file(state_dir: Path) -> dict[str, Any] | None:
    path = static_path(state_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_static_file(state_dir: Path, payload: dict[str, Any]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = static_path(state_dir)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def static_rebuild_needed(
    state_dir: Path,
    device_names: list[str],
    *,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "force"

    cached = load_static_file(state_dir)
    if cached is None:
        return True, "initial"

    cached_nodes = {
        node.get("id")
        for node in (cached.get("nodes") or [])
        if isinstance(node, dict) and node.get("id")
    }
    current_nodes = set(device_names)
    if cached_nodes != current_nodes:
        if current_nodes - cached_nodes:
            return True, "device_added"
        if cached_nodes - current_nodes:
            return True, "device_removed"
        return True, "config_change"

    return False, "cache"


def topology_force_update(settings: Settings | None = None) -> bool:
    if settings is not None:
        return settings.topology_force_update
    return os.environ.get("TOPOLOGY_FORCE_UPDATE", "0") in ("1", "true", "yes")


def build_static_document(
    *,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
    underlay_edges: list[dict[str, Any]],
    routing_edges: list[dict[str, Any]] | None = None,
    services_edges: list[dict[str, Any]] | None = None,
    build_reason: str,
) -> dict[str, Any]:
    layers = empty_static_layers()
    layers["physical"] = {
        "edges": physical_edges,
        "summary": summarize_static_physical(physical_edges),
    }
    layers["underlay"] = {
        "edges": underlay_edges,
        "summary": summarize_static_underlay(underlay_edges),
    }
    layers["routing"] = {
        "edges": routing_edges or [],
        "summary": summarize_static_routing(routing_edges or []),
    }
    layers["services"] = {
        "edges": services_edges or [],
        "summary": summarize_static_services(services_edges or []),
    }
    return {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_reason": build_reason,
        "nodes": nodes_from_device_names(device_names),
        "layers": layers,
    }


def merge_services_layer(
    document: dict[str, Any],
    services_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Update services layer in an existing static document (in place + return)."""
    layers = document.setdefault("layers", empty_static_layers())
    layers["services"] = {
        "edges": services_edges,
        "summary": summarize_static_services(services_edges),
    }
    return document


def static_view_from_document(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": document.get("nodes") or [],
        "layers": document.get("layers") or empty_static_layers(),
    }
