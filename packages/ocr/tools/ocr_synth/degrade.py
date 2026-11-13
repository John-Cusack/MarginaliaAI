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

CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("rn", "m"),
    ("cl", "d"),
    ("li", "h"),
    ("e", "c"),
    ("1", "l"),
    ("0", "O"),
)


def confuse_glyphs(text: str) -> str:
    """Draw the confusable pairs as the other glyph, so readers substitute.

    A word drawn this way is still *ink* and still read — as the wrong word. That
    is the class of error a three-reader confirm step cannot catch and a drop
    cannot teach, and it is the class the operator asked to have at least thirty
    members of before its sensitivity means anything.
    """
    drawn = text
    for source, replacement in CONFUSIONS:
        drawn = drawn.replace(source, replacement)
    return drawn


@dataclass(frozen=True)
class Degrade:
    """The ink treatments applied to a page, and by how much."""

    small_caps: bool = False
    light: bool = True
    kern: float = -0.8
    blur: float = 0.3
    # Glyph pairs drawn as one, so the error is a *wrong word*, not a dropped
    # one: rn/m, cl/d, li/h, e/c, and the numeral/letter pairs every reader
    # confuses. The operator's list, drawn at light ink and a smaller size.
    confuse: bool = False
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
        if self.confuse:
            parts.append("confuse")
        if self.light:
            parts.append("light")
        if self.kern:
            parts.append(f"kern{self.kern:+.1f}")
        if self.blur:
            parts.append(f"blur{self.blur:.1f}")
        return "-".join(parts) or "plain"

    def drawn(self, text: str, size: int) -> tuple[str, int]:
        """The text as it is drawn, and the size it is drawn at."""
        drawn = text
        if self.confuse:
            drawn = confuse_glyphs(drawn)
        if self.small_caps and drawn.isalpha():
            return drawn.upper(), max(1, int(size * 0.82))
        if self.confuse:
            return drawn, max(1, int(size * 0.9))
        return drawn, size
