"""Where a Docling document's title comes from.

The first line of converted text was the only source, and on a scanned book that
is whatever the layout model read first. The four PDFs in this corpus came out
as `fies`, `Mfo mm`, `CENTRAL` and the boilerplate header of a thesis repository
— and every one of them stated its correct title in metadata nothing looked at.

The opposite failure is just as real: of 88 PDFs here, 54 declare a title and 19
of those are junk. Each rejection case below is a string an actual file on this
machine claims as its title.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_engine.modules.docling_converter import (
    _extract_title,
    _usable_author,
    _usable_title,
)


class TestTitlesWorthTaking:
    @pytest.mark.parametrize(
        "declared",
        [
            "The campaigns of napoleon",
            "The Civil War papers of George B. McClellan : selected correspondence",
            "Metaphors of Reading: Cognition and Embodiment in Contemporary Metafiction",
            "Empower: Saving, investing and advice",
            "Robinhood",  # one word, but it is what the document is
            "Bitwise Offer Letter_ John Cusack",  # an underscore standing in for a colon
        ],
    )
    def test_a_real_title_is_kept(self, declared):
        assert _usable_title(declared) == declared


class TestTitlesWorthRefusing:
    @pytest.mark.parametrize(
        ("declared", "because"),
        [
            ("Document1", "an authoring default"),
            ("(anonymous)", "an authoring default"),
            ("untitled", "an authoring default"),
            ("1099", "an account number, not a title"),
            ("749537 NCM9JP01", "a part number"),
            ("202412_Wage and Income_109171834966", "an export identifier"),
            ("output_CSantiago_fmlrKYMGCeW3Nj3", "an export identifier"),
            ("Microsoft Word - PFS Editable.doc", "the authoring app plus a filename"),
            ("B87023352[1].pdf", "a filename"),
            ("489723_1_En_Print.indd", "a typesetting filename"),
            ("", "empty"),
            (None, "absent"),
        ],
    )
    def test_junk_is_refused(self, declared, because):
        assert _usable_title(declared) is None, because


class TestTheExtensionRule:
    """Stripping beats rejecting, and the difference is a real document."""

    def test_a_real_title_that_merely_ends_in_an_extension_survives(self):
        assert _usable_title("Rape Gang Inquiry Report.docx") == "Rape Gang Inquiry Report"

    def test_stripping_does_not_rescue_a_filename(self):
        """`B87023352[1]` still has almost no letters once `.pdf` is gone."""
        assert _usable_title("B87023352[1].pdf") is None

    def test_a_name_that_is_only_an_extension_is_refused(self):
        assert _usable_title(".pdf") is None


class TestFallbackChain:
    def test_the_first_line_is_used_when_there_is_no_metadata(self, tmp_path):
        """Scans assembled by hand often declare nothing at all."""
        absent = tmp_path / "nothing.pdf"
        absent.write_bytes(b"not a pdf")  # fitz fails; the chain must not

        assert _extract_title("# A Real Heading\n\nBody.", absent) == "A Real Heading"

    def test_image_placeholders_are_not_titles(self, tmp_path):
        text = "<!-- image -->\n\n<!-- image -->\n\nThe Actual Heading\n\nBody."

        assert _extract_title(text, tmp_path / "x.pdf") == "The Actual Heading"

    def test_the_filename_is_the_last_resort(self, tmp_path):
        assert _extract_title("", tmp_path / "campaignsofnapol.pdf") == "campaignsofnapol"

    def test_a_declared_title_beats_the_first_line(self):
        """The whole point: `fies` is the first line of Campaigns of Napoleon."""
        pdf = Path("/home/john/Downloads/campaignsofnapol0000unse_1.pdf")
        if not pdf.exists():
            pytest.skip("corpus PDF not present on this machine")

        assert _extract_title("fies\n\nmore text", pdf) == "The campaigns of napoleon"


class TestAuthors:
    """`/Author` is junkier than `/Title`: 15 of the 22 PDFs here that fill it
    name something that is not a person."""

    @pytest.mark.parametrize(
        "declared",
        [
            "Nicholas Wolterstorff",
            "Amanda L. Bailey",
            "Mott, Stephen Charles",
            "McClellan, George B. (George Brinton), 1826-1885",
            "Miranda, José Porfirio",
            "US Government IRS",  # an institution is still an author
        ],
    )
    def test_a_real_author_is_kept(self, declared):
        assert _usable_author(declared) == declared

    @pytest.mark.parametrize(
        ("declared", "because"),
        [
            ("Registered to: GEICO", "a software licensee"),
            ("Registered to: SGFS", "a software licensee"),
            ("PWinter", "a login name"),
            ("SBenigno", "a login name"),
            ("Administrator", "an authoring default"),
            ("(anonymous)", "an authoring default"),
            ("CamScanner", "the scanning app"),
            ("PP53454", "an internal identifier"),
            ("Pagination_Cover", "a production tool"),
            ("", "empty"),
            (None, "absent"),
        ],
    )
    def test_junk_is_refused(self, declared, because):
        assert _usable_author(declared) is None, because

    def test_a_surname_beginning_with_two_capitals_is_not_a_login(self):
        """The login rule must not eat real names. `McClellan` and `MacArthur`
        have a lowercase second character; `PWinter` does not."""
        assert _usable_author("McClellan") == "McClellan"
        assert _usable_author("MacArthur") == "MacArthur"

    def test_a_compound_library_field_is_kept_whole(self):
        """Archive.org crams contributors and a subtitle into one field.
        Splitting it is a different problem; guessing loses what was claimed."""
        declared = (
            "McClellan, George Brinton, 1826-1885; Prime, William Cowper, "
            "1825-1905. Life, services, and character of George B. McClellan"
        )
        assert _usable_author(declared) == declared
