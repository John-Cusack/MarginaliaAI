#!/usr/bin/env python3
"""Regenerate repr_table.rs from the running Python's unicodedata.

CPython's str repr() escapes a char iff it is not "printable":
``unicodedata.category(ch)`` in Cc/Cf/Cs/Co/Cn/Zl/Zp/Zs, except U+0020 SPACE
which repr passes through raw. The rule is data (Unicode version), not logic:
this script dumps the UNPRINTABLE ranges (minus U+0020) so
``marginalia_text::repr::is_py_printable`` needs no per-version tables by hand.

Regenerate only on Python/Unicode-data upgrades; any range-count change
fails the pinned count test in `repr.rs`. Mirrors etc/gen_word_table.py
for the ``re \\w`` classes.
"""

from __future__ import annotations

import unicodedata

UNPRINTABLE_CATS = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


def main() -> None:
    points: list[int] = [
        code
        for code in range(0x110000)
        if code != 0x20 and unicodedata.category(chr(code)) in UNPRINTABLE_CATS
    ]
    ranges: list[tuple[int, int]] = []
    for code in points:
        if ranges and code == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], code)
        else:
            ranges.append((code, code))
    lines = [
        "//! Printable table for Python `repr` emulation (generated — do not edit).",
        "//!",
        "//! Regenerate with `etc/gen_printable_table.py` on Python upgrades.",
        "//! Each entry is an inclusive UNPRINTABLE range (U+0020 excepted);",
        "//! `is_py_printable` passes anything outside them (plus U+0020).",
        "",
        "/// Inclusive unprintable ranges for `repr`, U+0020 SPACE excepted.",
        f"/// Count pinned by test: {len(ranges)} ranges.",
        "pub(crate) const UNPRINTABLE_RANGES: &[(u32, u32)] = &[",
    ]
    lines.extend(f"    (0x{lo:04X}, 0x{hi:04X})," for lo, hi in ranges)
    lines.append("];")
    print(f"{len(ranges)} ranges")
    with open("crates/marginalia-text/src/repr_table.rs", "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
