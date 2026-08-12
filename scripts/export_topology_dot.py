"""Export topology.static.json edges to Graphviz DOT (and optionally render).

Default layers are underlay + routing (device↔device links). Physical edges are
mostly inventory interfaces with remote=null and are noisy unless you pass
--layers physical or --include-unlinked. Service local-only edges
(``remote=null``, type ``service_endpoint_pair``) draw as self-loops when
``services`` is in ``--layers``.

Examples:

  python scripts/export_topology_dot.py > topo.dot
  python scripts/export_topology_dot.py -o topo.dot --render png
  python scripts/export_topology_dot.py --layers underlay,routing,physical -o all.dot
  python scripts/export_topology_dot.py --layers underlay,routing,services -o svc.dot
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "state" / "topology.static.json"
DEFAULT_LAYERS = ("underlay", "routing")


def _load_topology(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    # Allow passing latest.json / snapshot.json
    if "topology" in data and isinstance(data["topology"], dict):
        topo = data["topology"]
        static = topo.get("static")
        if isinstance(static, dict):
            return static
    return data


def _endpoint_device(side: object) -> str | None:
    if not isinstance(side, dict):
        return None
    device = side.get("device")
    return device if isinstance(device, str) and device else None


def _endpoint_label(side: object) -> str:
    if not isinstance(side, dict):
        return ""
    iface = side.get("interface")
    addr = side.get("address")
    if isinstance(iface, str) and iface:
        return iface
    if isinstance(addr, str) and addr:
        return addr
    return ""


def _service_edge_label(edge: dict) -> str:
    meta = edge.get("meta") or {}
    st = meta.get("service_type")
    name = meta.get("name")
    if isinstance(st, str) and st and isinstance(name, str) and name:
        return f"{st}/{name}"
    return str(edge.get("type") or "service")


def _is_service_endpoint_pair(edge: dict, layer: str) -> bool:
    return edge.get("type") == "service_endpoint_pair" or layer == "services"


def edges_to_dot(
    topology: dict,
    *,
    layers: tuple[str, ...],
    include_unlinked: bool,
) -> str:
    lines: list[str] = [
        "digraph topology {",
        "  rankdir=LR;",
        '  node [shape=box, fontname="Helvetica"];',
        '  edge [fontname="Helvetica", fontsize=10];',
        "",
    ]
    layer_map = topology.get("layers") or {}
    drawn = 0
    skipped = 0

    for layer in layers:
        block = layer_map.get(layer) or {}
        edges = block.get("edges") or []
        lines.append(f"  // layer: {layer} ({len(edges)} edges in JSON)")
        for edge in edges:
            if not isinstance(edge, dict):
                skipped += 1
                continue
            local = edge.get("local")
            remote = edge.get("remote")
            da = _endpoint_device(local)
            db = _endpoint_device(remote)
            etype = edge.get("type") or layer
            if da and db:
                if _is_service_endpoint_pair(edge, layer):
                    label = _service_edge_label(edge)
                else:
                    la = _endpoint_label(local)
                    lb = _endpoint_label(remote)
                    label = etype if not (la or lb) else f"{etype}\\n{la} — {lb}"
                lines.append(
                    f'  "{da}" -> "{db}" [label="{label}", dir=both];'
                )
                drawn += 1
            elif da and _is_service_endpoint_pair(edge, layer) and remote is None:
                label = _service_edge_label(edge)
                lines.append(
                    f'  "{da}" -> "{da}" [label="{label}"];'
                )
                drawn += 1
            elif include_unlinked and da:
                iface = _endpoint_label(local) or (edge.get("id") or "")
                node = f"{da}\\n{iface}" if iface else da
                lines.append(
                    f'  "{node}" [shape=plaintext, fontsize=9];'
                )
                drawn += 1
            else:
                skipped += 1
        lines.append("")

    lines.append(f"  // exported_edges={drawn} skipped={skipped}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render(dot_path: Path, fmt: str) -> Path:
    if not shutil.which("dot"):
        raise SystemExit(
            "graphviz 'dot' not found on PATH; install graphviz or omit --render"
        )
    out = dot_path.with_suffix(f".{fmt}")
    subprocess.run(
        ["dot", f"-T{fmt}", str(dot_path), "-o", str(out)],
        check=True,
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export NSO summary topology JSON to Graphviz DOT."
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"topology JSON (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write DOT to this file (default: stdout)",
    )
    parser.add_argument(
        "--layers",
        default=",".join(DEFAULT_LAYERS),
        help="comma-separated layers (default: underlay,routing)",
    )
    parser.add_argument(
        "--include-unlinked",
        action="store_true",
        help="include physical/inventory edges with no remote device",
    )
    parser.add_argument(
        "--render",
        choices=("png", "svg", "pdf"),
        help="run graphviz 'dot' to render (requires -o)",
    )
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 1

    layers = tuple(part.strip() for part in args.layers.split(",") if part.strip())
    if not layers:
        print("error: --layers is empty", file=sys.stderr)
        return 1

    topology = _load_topology(args.input)
    dot = edges_to_dot(
        topology, layers=layers, include_unlinked=args.include_unlinked
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(dot, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
        if args.render:
            out = _render(args.output, args.render)
            print(f"wrote {out}", file=sys.stderr)
    else:
        if args.render:
            print("error: --render requires -o/--output", file=sys.stderr)
            return 1
        sys.stdout.write(dot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
