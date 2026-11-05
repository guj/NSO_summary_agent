"""Devices NSO could not authenticate to this scan, in NSO's own words, wherever the scan recorded it.

A fact about NSO's session to a device, such as a host key that no longer matches the one NSO
stores: it says nothing about the device's health, and the scan changes nothing about it.
"""
from __future__ import annotations

from typing import Any

from nso_facts.mcp_client import nso_authentication_failures


def _strings(node: Any):
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "authenticate" in item:
                yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def authentication_failures(case: Any) -> dict[str, list[str]]:
    """Each device NSO failed to authenticate to at least once, with NSO's reasons; a case or its saved dict."""
    if isinstance(case, dict):
        issues, evidence = case.get("issues") or [], case.get("evidence") or []
    else:
        issues, evidence = getattr(case, "issues", None) or [], getattr(case, "evidence", None) or []
    found: dict[str, set[str]] = {}
    for text in _strings([issues, evidence]):
        for device, reason in nso_authentication_failures(text):
            found.setdefault(device, set()).add(reason)
    return {device: sorted(reasons) for device, reasons in sorted(found.items())}


def failure_reason(message: str, device: str) -> str | None:
    """NSO's reason when ``message`` says it could not authenticate to ``device``."""
    return next((reason for name, reason in nso_authentication_failures(message) if name == device), None)


def names(devices: list[str]) -> str:
    quoted = [f"`{device}`" for device in devices]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1] if len(quoted) > 1 else quoted[0]


def host_key_followup(devices: list[str]) -> str:
    """What a person does about devices whose host key NSO rejected."""
    many = len(devices) > 1
    return (f"Verify the SSH host keys of {names(devices)} out-of-band: NSO rejected the key "
            f"{'each' if many else 'the device'} presented at least once this scan (SSH host key mismatch), "
            f"so readings from {'them' if many else 'it'} may be partial. If a key changed for a known reason, "
            "such as an upgrade, a person compares the fingerprint and then updates the key NSO stores "
            "(`devices device <name> ssh fetch-host-keys` in the NSO CLI); an unexplained change is a security "
            "concern. This scan changed nothing.")


def credentials_followup(devices: list[str], reasons: list[str]) -> str:
    """What a person does about devices NSO could not log in to for another reason."""
    many = len(devices) > 1
    return (f"Check the credentials NSO uses for {names(devices)}: NSO could not authenticate to "
            f"{'them' if many else 'it'} at least once this scan ({'; '.join(reasons)}), so readings from "
            f"{'them' if many else 'it'} may be partial. This scan changed nothing.")
