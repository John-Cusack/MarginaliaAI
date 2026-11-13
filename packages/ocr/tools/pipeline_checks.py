"""The independent checks (design 7.5), re-exported from the service.

WP0's measurement and the production path must not disagree about what a check
catches, so the implementations live in ``marginalia_ocr.server.checks`` and this
module exists only so the measurement scripts and their tests keep importing
``pipeline_checks``. Names not re-exported here were the tool's own.
"""

from marginalia_ocr.server.checks import (  # noqa: F401
    CHROME_MARKERS,
    book_vocabulary,
    catches,
    character_density,
    chrome_leaks,
    coverage_failures,
    dashed,
    edit_distance,
    heading_vs_toc,
    implausible_numbers,
    line_count_mismatch,
    line_texts,
    normalize_heading,
    note_sequence,
    number_shape,
    running_head_word,
    split_unknown,
    system_dictionary,
    table_number_formats,
    unmatched_words,
)
