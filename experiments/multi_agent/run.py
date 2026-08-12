"""Deprecated: use `nso-multi-agent-run` or `python -m multi_agent.run`."""

from __future__ import annotations

import sys

from multi_agent.run import main

if __name__ == "__main__":
    print(
        "warning: experiments.multi_agent is deprecated; "
        "use nso-multi-agent-run or python -m multi_agent.run",
        file=sys.stderr,
    )
    raise SystemExit(main())
