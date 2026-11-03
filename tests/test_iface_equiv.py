import json
from pathlib import Path

from nso_facts.topology.iface_equiv import (
    compact_box_list,
    find_live_candidates,
    interface_port_address,
    load_interface_equivalences,
    resolve_equivalence,
)


def test_interface_port_address_long_and_short():
    assert interface_port_address("FourHundredGigE0/0/0/32") == "0/0/0/32"
    assert interface_port_address("Hu0/0/0/32") == "0/0/0/32"
    assert interface_port_address("Te0/0/0/36/0") == "0/0/0/36/0"


def test_find_live_candidates_type_change():
    cands, kind = find_live_candidates(
        "FourHundredGigE0/0/0/32",
        ["Hu0/0/0/32", "Hu0/0/0/33", "Lo0"],
    )
    assert kind == "type_change"
    assert cands == ["Hu0/0/0/32"]


def test_find_live_candidates_breakout():
    cands, kind = find_live_candidates(
        "FourHundredGigE0/0/0/36",
        ["Te0/0/0/36/0", "Te0/0/0/36/1", "Te0/0/0/36/2", "Te0/0/0/36/3"],
    )
    assert kind == "breakout"
    assert cands == [
        "Te0/0/0/36/0",
        "Te0/0/0/36/1",
        "Te0/0/0/36/2",
        "Te0/0/0/36/3",
    ]


def test_find_live_candidates_missing():
    cands, kind = find_live_candidates("HundredGigE0/0/0/22", ["Hu0/0/0/21"])
    assert kind == "missing"
    assert cands == []


def test_compact_box_list():
    assert compact_box_list([]) == "(not on box)"
    assert compact_box_list(["Hu0/0/0/32"]) == "Hu0/0/0/32"
    assert (
        compact_box_list(
            ["Te0/0/0/36/0", "Te0/0/0/36/1", "Te0/0/0/36/2", "Te0/0/0/36/3"]
        )
        == "Te0/0/0/36/0–3"
    )


def test_load_and_resolve_equivalences(tmp_path: Path):
    path = tmp_path / "eq.json"
    path.write_text(
        json.dumps(
            {
                "equivalences": [
                    {
                        "device": "renc-data-sw",
                        "nso": "FourHundredGigE0/0/0/32",
                        "box": "Hu0/0/0/32",
                    },
                    {
                        "device": "renc-data-sw",
                        "nso": "FourHundredGigE0/0/0/36",
                        "box": [
                            "Te0/0/0/36/0",
                            "Te0/0/0/36/1",
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    eq = load_interface_equivalences(path)
    assert resolve_equivalence(eq, "renc-data-sw", "FourHundredGigE0/0/0/32") == [
        "Hu0/0/0/32"
    ]
    assert resolve_equivalence(eq, "renc-data-sw", "FourHundredGigE0/0/0/36") == [
        "Te0/0/0/36/0",
        "Te0/0/0/36/1",
    ]
    assert resolve_equivalence(eq, "other", "x") is None


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_interface_equivalences(tmp_path / "nope.json") == {}
    assert load_interface_equivalences(None) == {}
