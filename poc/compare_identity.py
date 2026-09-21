"""
Compare the InventoryIdentity dumps of two probe_identity.py runs (offline, plain Python).

    python poc/compare_identity.py [poc/probe_output.txt]

For every weapon of the latest run, the script looks for the same body in the run before
and reports which bytes of the 216-byte identity agree between the two dumps and which
differ, as offset ranges. Bytes that agree across runs for the same gun but differ between
guns are the identity candidates. Pointers (owned allocations) show up as differing words;
values (seeds, levels, part indices) as agreeing ones.
"""

from __future__ import annotations

import re
import sys
from collections import OrderedDict
from pathlib import Path

LINE = re.compile(r"^identity run=(\S+) slot=(\d+) body=(\S+) id=(\d+) bytes=([0-9a-f]+)$")
WORD_BYTES = 8


def read_runs(path: Path) -> OrderedDict[str, list[tuple[int, str, int, bytes]]]:
    runs: OrderedDict[str, list[tuple[int, str, int, bytes]]] = OrderedDict()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(line.strip())
        if match is None:
            continue
        run, slot, body, instance_id, hex_bytes = match.groups()
        runs.setdefault(run, []).append((int(slot), body, int(instance_id), bytes.fromhex(hex_bytes)))
    return runs


def ranges(offsets: list[int]) -> str:
    """[0, 1, 2, 8, 9] -> '0-2, 8-9'."""
    if not offsets:
        return "none"
    parts: list[str] = []
    start = previous = offsets[0]
    for offset in offsets[1:]:
        if offset == previous + 1:
            previous = offset
            continue
        parts.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = offset
    parts.append(f"{start}-{previous}" if start != previous else str(start))
    return ", ".join(parts)


def compare(old: bytes, new: bytes) -> tuple[list[int], list[int]]:
    same = [offset for offset in range(min(len(old), len(new))) if old[offset] == new[offset]]
    different = [offset for offset in range(min(len(old), len(new))) if old[offset] != new[offset]]
    return same, different


def word_table(label: str, data: bytes) -> None:
    words = [data[offset : offset + WORD_BYTES].hex() for offset in range(0, len(data), WORD_BYTES)]
    print(f"   {label}")
    for index in range(0, len(words), 4):
        print(f"      {index * WORD_BYTES:4d}: {'  '.join(words[index : index + 4])}")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "probe_output.txt"
    runs = read_runs(path)
    if len(runs) < 2:
        print(f"{len(runs)} run(s) found in {path}; two are needed")
        return 1
    (old_run, old_weapons), (new_run, new_weapons) = list(runs.items())[-2:]
    print(f"comparing run {old_run} with run {new_run}")
    for slot, body, instance_id, new_bytes in new_weapons:
        candidates = [weapon for weapon in old_weapons if weapon[1] == body]
        if not candidates:
            print(f"slot {slot} {body} #{instance_id}: no weapon with that body in the earlier run")
            continue
        for old_slot, _, old_id, old_bytes in candidates:
            same, different = compare(old_bytes, new_bytes)
            print(
                f"slot {slot} {body} #{instance_id} vs earlier slot {old_slot} #{old_id}: "
                f"{len(same)} bytes agree, {len(different)} differ; differing offsets: {ranges(different)}"
            )
    print("stable bytes that differ between the guns of the latest run are the identity candidates:")
    if len(new_weapons) >= 2:
        first = new_weapons[0][3]
        varying = [offset for offset in range(len(first)) if any(weapon[3][offset] != first[offset] for weapon in new_weapons[1:])]
        print(f"   offsets that vary between guns: {ranges(varying)}")
    for slot, body, instance_id, new_bytes in new_weapons:
        word_table(f"slot {slot} {body} #{instance_id}", new_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
