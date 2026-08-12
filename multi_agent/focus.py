"""Layer focus helpers for multi-agent runs."""

from __future__ import annotations


def resolve_layer_focus(
    *, isis_only: bool = False, bgp_only: bool = False
) -> tuple[bool, bool]:
    """Return ``(run_isis, run_bgp)``.

    Raises ``ValueError`` if both exclusive flags are set.
    """
    if isis_only and bgp_only:
        raise ValueError("--isis-only and --bgp-only are mutually exclusive")
    if isis_only:
        return True, False
    if bgp_only:
        return False, True
    return True, True
