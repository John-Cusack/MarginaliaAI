//! Script-aware token estimation, mirroring `chunking.py` (`chars_per_token`
/// and friends, re-exported through `services/text/tokens.py`).
///
/// Rates are the 10th percentile of bge-m3 measurements on this corpus, not
/// the median: the estimate stops a passage being too big, so over-counting
/// is the safe direction. ASCII stays at exactly 4.0 — changing it would
/// re-chunk every Latin-script document for a 10% gain inside tolerance.
pub const DEFAULT_CHARS_PER_TOKEN: f64 = 4.0;
pub const ABSOLUTE_MAX_TOKENS: i64 = 2_000;

const UNKNOWN_CHARS_PER_TOKEN: f64 = 2.0;
const SAMPLE_CEILING: usize = 60_000;

/// (first, last, chars-per-token) codepoint ranges.
const SCRIPT_RANGES: &[(u32, u32, f64)] = &[
    (0x0370, 0x03FF, 1.68),                    // Greek
    (0x1F00, 0x1FFF, 1.68),                    // Greek extended
    (0x0590, 0x05FF, 1.40),                    // Hebrew
    (0xFB1D, 0xFB4F, 1.40),                    // Hebrew presentation forms
    (0x0400, 0x04FF, DEFAULT_CHARS_PER_TOKEN), // Cyrillic
    (0x0600, 0x06FF, 3.40),                    // Arabic
    (0x0750, 0x077F, 3.40),                    // Arabic supplement
    (0x3040, 0x30FF, 1.50),                    // Hiragana/Katakana
    (0x3400, 0x4DBF, 1.50),                    // CJK ext A
    (0x4E00, 0x9FFF, 1.50),                    // CJK unified
    (0xAC00, 0xD7AF, 1.50),                    // Hangul
    (0xF900, 0xFAFF, 1.50),                    // CJK compat
    (0x0100, 0x024F, 3.00),                    // Latin extended
    (0x1E00, 0x1EFF, 3.00),                    // Latin extended additional
    (0x0300, 0x036F, 2.00),                    // Combining marks
];

fn tokens_per_char(codepoint: u32) -> f64 {
    let mut rate = UNKNOWN_CHARS_PER_TOKEN;
    if codepoint < 0x80 {
        rate = DEFAULT_CHARS_PER_TOKEN;
    } else {
        for &(first, last, script_rate) in SCRIPT_RANGES {
            if first <= codepoint && codepoint <= last {
                rate = script_rate;
                break;
            }
        }
    }
    1.0 / rate
}

/// Sample step over a decoded slice: every char up to the ceiling, every
/// Nth char past it — stepped in place, with no `Vec` to collect.
fn sample_step(len: usize) -> usize {
    if len <= SAMPLE_CEILING {
        1
    } else {
        len / SAMPLE_CEILING + 1
    }
}

/// Conservative average characters-per-token for mixed scripts.
pub fn chars_per_token(text: &str) -> f64 {
    if text.is_empty() || text.is_ascii() {
        return DEFAULT_CHARS_PER_TOKEN;
    }
    let chars: Vec<char> = text.chars().collect();
    chars_per_token_of(&chars)
}

/// [`chars_per_token`] over an already-decoded slice: same values in the
/// same order through [`neumaier_sum`], so bit-identical, with no second
/// decode. No ASCII/empty fast paths: the math yields exactly
/// `DEFAULT_CHARS_PER_TOKEN` for ASCII, and callers hold non-empty spans
/// (the empty slice still resolves to `1` downstream, as before).
pub fn chars_per_token_of(chars: &[char]) -> f64 {
    debug_assert!(!chars.is_empty());
    let step = sample_step(chars.len());
    let tokens = neumaier_sum(
        chars
            .iter()
            .step_by(step)
            .map(|&c| tokens_per_char(c as u32)),
    );
    chars.len().div_ceil(step) as f64 / tokens
}

/// Characters per token for the densest script present in `text`.
pub fn min_chars_per_token(text: &str) -> f64 {
    if text.is_empty() || text.is_ascii() {
        return DEFAULT_CHARS_PER_TOKEN;
    }
    let chars: Vec<char> = text.chars().collect();
    min_chars_per_token_of(&chars)
}

/// [`min_chars_per_token`] over an already-decoded slice, as above.
pub fn min_chars_per_token_of(chars: &[char]) -> f64 {
    debug_assert!(!chars.is_empty());
    let step = sample_step(chars.len());
    let peak = chars
        .iter()
        .step_by(step)
        .map(|&c| tokens_per_char(c as u32))
        .fold(0.0_f64, f64::max);
    1.0 / peak
}

/// CPython-compatible float summation (Neumaier compensation).
///
/// `chars_per_token` sums per-character rates with the builtin `sum()`, which
/// has compensated since 3.12 — a naive fold differs by 1 ulp, and that ulp
/// can flip the `int()` truncation in `approx_tokens`. Verified bit-exact
/// against CPython 3.13 over 20,000 randomized inputs.
fn neumaier_sum(values: impl Iterator<Item = f64>) -> f64 {
    let mut sum = 0.0;
    let mut comp = 0.0;
    for x in values {
        let t = sum + x;
        if sum.abs() >= x.abs() {
            comp += (sum - t) + x;
        } else {
            comp += (x - t) + sum;
        }
        sum = t;
    }
    sum + comp
}

/// Estimated token count; 1 for empty text, never 0.
pub fn approx_tokens(text: &str, rate: Option<f64>) -> i64 {
    if text.is_empty() {
        return 1;
    }
    let rate = rate.unwrap_or_else(|| chars_per_token(text));
    ((text.chars().count() as f64 / rate) as i64).max(1)
}

/// Character budget for `max_tokens` at a measured `rate`.
pub fn token_budget_chars(max_tokens: i64, rate: f64) -> i64 {
    ((max_tokens as f64 * rate) as i64).max(1)
}
