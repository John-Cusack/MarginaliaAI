"""Make a page's ink hard to read on purpose, so agreement becomes possible.

The planted set asks an auditor to find errors somebody invented. The errors
that actually escape a three-reader confirm step are the ones all three readers
made, and on clean synthetic prose that set came out **empty**: three engines do
not fail the same way on a crisp rendering. Real books are not crisp. This module
bends a page's ink the way print does, so the same wrong reading is the *easiest*
one for every engine:

* **small caps** — the word drawn as capitals at a reduced size, while the
  canonical text keeps its own case. A reader that answers ``MARGIN`` for
  ``Margin`` is reading the page, and is wrong against the text.
* **light ink** — grey instead of black, so thin strokes lose their edges.
* **tight kerning** — letters advanced closer than the font wants, which is how
  ``rn`` becomes ``m`` in every reader at once.
* **blur** — on top of whatever the page already carries.

None of it changes the truth: the page still says what it said, and every
degradation is recorded on the screen so a measurement says which one it used.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Degrade:
    """The ink treatments applied to a page, and by how much."""

    small_caps: bool = False
    light: bool = True
    kern: float = -0.8
    blur: float = 0.3
    ink: tuple[int, int, int] = (150, 150, 150)

    def record(self) -> dict[str, object]:
        return {
            "small_caps": self.small_caps,
            "light": self.light,
            "kern": self.kern,
            "blur": self.blur,
            "ink": list(self.ink),
        }

    @property
    def label(self) -> str:
        parts = []
        if self.small_caps:
            parts.append("caps")
        if self.light:
            parts.append("light")
        if self.kern:
            parts.append(f"kern{self.kern:+.1f}")
        if self.blur:
            parts.append(f"blur{self.blur:.1f}")
        return "-".join(parts) or "plain"

    def drawn(self, text: str, size: int) -> tuple[str, int]:
        """The text as it is drawn, and the size it is drawn at."""
        if self.small_caps and text.isalpha():
            return text.upper(), max(1, int(size * 0.82))
        return text, size
