#!/usr/bin/env python3
"""Force-target uky-data-sw BVI4002 as up/down and run the FABRIC+MCP tool loop.

Run from a shell that can reach NSO and FABRIC (not the restricted Cursor sandbox):

  cd /Users/dec2023/Work/ESnet/NSO_summary_agent
  set -a && source .env && set +a
  python scripts/pretend_troubleshoot_bvi4002.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import load_settings
from agent.iface_troubleshoot import troubleshoot_interface
from agent.mcp_client import mcp_session


async def main() -> None:
    settings = load_settings()
    device = "uky-data-sw"
    # XR name is BVI4002 (BV4002 is the abbreviated form in some show output).
    iface = "BVI4002"
    print(f"Pretend target: {device} {iface} (admin up / oper down)")
    print(f"Model: {settings.fabric_model}")
    async with mcp_session(settings) as client:
        entry = await troubleshoot_interface(
            client,
            settings,
            device=device,
            interface=iface,
            reason="up_down",
            context={
                "admin_oper": {"admin": "up", "oper": "down", "status": "down"},
                "pretend": True,
                "note": (
                    "Forced up/down for exercise; live brief may still show up/up. "
                    "Alias BV4002 → BVI4002."
                ),
            },
        )
    print(json.dumps(entry, indent=2, default=str))
    print("\nDevices Alerts row would look like:")
    print(f"  {entry['interface']} — {entry['summary']}")


if __name__ == "__main__":
    asyncio.run(main())
