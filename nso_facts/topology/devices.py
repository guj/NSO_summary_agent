"""Parse MCP device inventory responses."""

from __future__ import annotations

from typing import Any


def parse_device_names(response: Any) -> list[str]:
    """Extract device names from list_devices (or similar) MCP payloads."""
    names: list[str] = []

    payload = response
    if isinstance(payload, dict) and payload.get("status") == "success":
        payload = payload.get("data") or payload

    if isinstance(payload, dict):
        devices = payload.get("devices")
        if isinstance(devices, list):
            for entry in devices:
                name = _name_from_device_entry(entry)
                if name:
                    names.append(name)
            return sorted(set(names))

        for key in ("tailf-ncs:device", "device"):
            raw = payload.get(key)
            if isinstance(raw, list):
                for entry in raw:
                    name = _name_from_device_entry(entry)
                    if name:
                        names.append(name)
                return sorted(set(names))

    if isinstance(payload, list):
        for entry in payload:
            name = _name_from_device_entry(entry)
            if name:
                names.append(name)
        return sorted(set(names))

    return []


def nodes_from_device_names(device_names: list[str]) -> list[dict[str, str | None]]:
    return [{"id": name, "site_id": None} for name in sorted(device_names)]


def _name_from_device_entry(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("name", "id", "device"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""
