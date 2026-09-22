//! Read the dates people actually wrote in letters.
//!
//! Python source: `services/text/dates.py`. A schema field `type:
//! fuzzy_date` parses the model's free-text date into a span plus a
//! precision; `scan_dates` finds datelines inside running text that has been
//! through a scanner.
//!
//! Pure Gregorian calendar arithmetic throughout: the module never needs a
//! non-Gregorian calendar, so `chrono` alone carries it — no `icu_calendar`
//! dependency (checked: `_at`/`_end_of` build plain UTC midnights, and the
//! only calendar query is month length).
//!
//! Regex notes: every `\s` in the Python patterns is spelled as the
//! [`PY_WS`] class — the `regex` crate's `\s` misses U+001C–U+001F, which
//! Python's `re` matches (see the crate-level parity findings).

use std::collections::HashMap;
use std::sync::LazyLock;

use chrono::{DateTime, Datelike, Duration, NaiveDate, TimeZone, Utc};
use regex::Regex;

use marginalia_types::events::FuzzyDate;

/// Python `re \s` == `str.isspace()`: Unicode White_Space plus U+001C–U+001F.
pub const PY_WS: &str = r"\p{White_Space}\p{Z}\x1c\x1d\x1e\x1f";

pub static MONTHS: &[(&str, u32)] = &[
    ("january", 1),
    ("jan", 1),
    ("jany", 1),
    ("janr", 1),
    ("february", 2),
    ("feb", 2),
    ("feby", 2),
    ("febr", 2),
    ("march", 3),
    ("mar", 3),
    ("april", 4),
    ("apr", 4),
    ("may", 5),
    ("june", 6),
    ("jun", 6),
    ("july", 7),
    ("jul", 7),
    ("august", 8),
    ("aug", 8),
    ("september", 9),
    ("sep", 9),
    ("sept", 9),
    ("sepr", 9),
    ("october", 10),
    ("oct", 10),
    ("octr", 10),
    ("november", 11),
    ("nov", 11),
    ("novr", 11),
    ("december", 12),
    ("dec", 12),
    ("decr", 12),
];

/// Northern-hemisphere seasons: `(start_month, start_day, end_month,
/// end_day, end_year_shift)`. Winter spans the year boundary.
type SeasonDef = (u32, u32, u32, u32, i32);
pub static SEASONS: &[(&str, SeasonDef)] = &[
    ("spring", (3, 1, 5, 31, 0)),
    ("summer", (6, 1, 8, 31, 0)),
    ("autumn", (9, 1, 11, 30, 0)),
    ("fall", (9, 1, 11, 30, 0)),
    ("winter", (12, 1, 2, 28, 1)),
];

/// Misreads frequent enough in scanned print to name outright.
pub static MONTH_OCR: &[(&str, u32)] = &[
    ("dee", 12),
    ("deo", 12),
    ("dec'", 12),
    ("jime", 6),
    ("jnne", 6),
    ("jnue", 6),
    ("jnly", 7),
    ("julv", 7),
    ("angust", 8),
    ("augnst", 8),
    ("marcli", 3),
    ("mareh", 3),
    ("mareli", 3),
    ("febmary", 2),
    ("febrnary", 2),
    ("jaimary", 1),
    ("jannary", 1),
    ("aprii", 4),
    ("apl", 4),
    ("apnl", 4),
    ("oet", 10),
    ("octr", 10),
    ("xov", 11),
    ("novr", 11),
    ("sepr", 9),
    ("sepc", 9),
];

/// How close a scanned word must be to a month name to read as one.
pub const MONTH_SIMILARITY: f64 = 0.75;

/// How far past the day to look for the year.
pub const YEAR_WINDOW: usize = 22;

/// Python `_MONTH_NAMES`: `MONTHS` keys longest-first. Both sorts are stable,
/// so equal-length names keep `MONTHS` order in both languages; verified
/// against the live value
/// (`september|february|...|dec`).
static MONTH_ALT: LazyLock<String> = LazyLock::new(|| {
    let mut keys: Vec<&str> = MONTHS.iter().map(|(name, _)| *name).collect();
    keys.sort_by_key(|a| std::cmp::Reverse(a.len()));
    keys.join("|")
});

/// Period ordinals: `2d` and `3d` are as common as `2nd`/`3rd`.
const ORDINAL: &str = r"(?:st|nd|rd|th|d)";

pub static LEAD_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"^(?:[{w}]*(?:your[s]?[{w}]+(?:\w+[{w}]+){{0,3}}of[{w}]+|letter[s]?[{w}]+of[{w}]+|in[{w}]+reply[{w}]+to[{w}]+|received[{w}]+|acknowledg\w*[{w}]+|dated[{w}]+|written[{w}]+|on[{w}]+|of[{w}]+|the[{w}]+)+)",
        w = PY_WS
    ))
    .expect("LEAD regex")
});

static ISO_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(\d{4})-(\d{1,2})-(\d{1,2})$").expect("ISO regex"));

static NUMERIC_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$").expect("NUMERIC regex"));

static MONTH_DAY_YEAR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^({m})\.?[{w}]+(\d{{1,2}}){o}?[{w}]*,?[{w}]*(\d{{4}})$",
        m = MONTH_ALT.as_str(),
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("MONTH_DAY_YEAR regex")
});

static DAY_MONTH_YEAR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(\d{{1,2}}){o}?[{w}]+({m})\.?[{w}]*,?[{w}]*(\d{{4}})$",
        m = MONTH_ALT.as_str(),
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("DAY_MONTH_YEAR regex")
});

static MONTH_YEAR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^({m})\.?[{w}]*,?[{w}]*(\d{{4}})$",
        m = MONTH_ALT.as_str(),
        w = PY_WS
    ))
    .expect("MONTH_YEAR regex")
});

static MONTH_DAY_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^({m})\.?[{w}]+(\d{{1,2}}){o}?$",
        m = MONTH_ALT.as_str(),
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("MONTH_DAY regex")
});

static DAY_MONTH_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(\d{{1,2}}){o}?[{w}]+({m})\.?$",
        m = MONTH_ALT.as_str(),
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("DAY_MONTH regex")
});

static SEASON_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(spring|summer|autumn|fall|winter)[{w}]+(?:of[{w}]+)?(\d{{4}})$",
        w = PY_WS
    ))
    .expect("SEASON regex")
});

static DECADE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?i)^(\d{{3}})0[{w}]*'?s$", w = PY_WS)).expect("DECADE regex")
});

static YEAR_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\d{4})$").expect("YEAR regex"));

static WEEK_OF_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"(?i)^week[{w}]+of[{w}]+(.+)$", w = PY_WS)).expect("WEEK_OF regex")
});

static RELATIVE_MONTH_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"(?i)^(\d{{1,2}}){o}?[{w}]*(ult(?:o|imo)?|inst(?:\.|ant)?|prox(?:o|imo)?)\.?$",
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("RELATIVE_MONTH regex")
});

/// Case-sensitive like the Python original: `15D` alone is not a date, while
/// `May 15D, 1862` parses (that pattern carries `re.IGNORECASE`).
static BARE_DAY_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"^(\d{{1,2}}){o}?$", o = ORDINAL)).expect("BARE_DAY regex")
});

static TODAY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(today|to-day|this day)$").expect("TODAY regex"));

static YESTERDAY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(yesterday)$").expect("YESTERDAY regex"));

static MONTH_AND_DAY_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"\b([A-Za-z]{{3,9}})\.?[{w}\]\)\[\(\|,]{{0,4}}(\d{{1,2}}){o}?\.?",
        w = PY_WS,
        o = ORDINAL
    ))
    .expect("MONTH_AND_DAY regex")
});

static FULL_YEAR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(1[5-9]\d{2}|20\d{2})\b").expect("FULL_YEAR regex"));

static SLASHED_YEAR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"^[{w}\]\)\[\(\|,]{{0,3}}/[{w}]?(\d{{2}})\b",
        w = PY_WS
    ))
    .expect("SLASHED_YEAR regex")
});

/// Parse a written date into a span and a precision, or return `None`.
///
/// `relative_to` is the date of the document the phrase was written in.
/// Forms naming only a day mean nothing without it and are refused rather
/// than guessed.
pub fn parse_fuzzy_date(text: &str, relative_to: Option<DateTime<Utc>>) -> Option<FuzzyDate> {
    if py_strip(text).is_empty() {
        return None;
    }
    let no_lead = LEAD_RE.replace(py_strip(text), "");
    let cleaned = py_strip(no_lead.as_ref()).trim_end_matches([',', '.']);
    if cleaned.is_empty() {
        return None;
    }

    if let Some(caps) = WEEK_OF_RE.captures(cleaned) {
        let inner_text = caps
            .get(1)
            .expect("WEEK_OF always captures its tail")
            .as_str();
        let inner = parse_fuzzy_date(inner_text, relative_to)?;
        let end = add_days(&inner.start, 6);
        return Some(span(
            inner.start,
            end,
            marginalia_types::common::DatePrecision::Week,
        ));
    }

    if let Some(found) = try_iso(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_numeric(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_month_day_year(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_day_month_year(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_season(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_month_year(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_decade(cleaned) {
        return Some(found);
    }
    if let Some(found) = try_year(cleaned) {
        return Some(found);
    }

    // Everything below needs to know when the letter was written.
    let anchor = relative_to?;
    try_relative(cleaned, anchor)
}

/// Every date this text states, as `(start, end, date)` char offsets in
/// document order.
pub fn scan_dates(text: &str, century: Option<i64>) -> Vec<(usize, usize, FuzzyDate)> {
    let mut found = Vec::new();
    for m in MONTH_AND_DAY_RE.find_iter(text) {
        let word = m.as_str();
        // Group 1 is the month word; re-capture for the group text. `find`
        // offsets are byte offsets; the contract (and Python) counts chars.
        // `word` is the slice this same regex just matched, so it
        // re-captures infallibly: the pattern is unanchored, matching is
        // forward-only, and the leading `\b` holds at the start of a
        // letter-led word exactly as it did at the match start.
        let caps = MONTH_AND_DAY_RE
            .captures(word)
            .expect("find_iter match re-captures");
        let month_word = caps
            .get(1)
            .expect("MONTH_AND_DAY always captures its word")
            .as_str();
        let Some(month) = read_month(month_word) else {
            continue;
        };
        let day_text = caps
            .get(2)
            .expect("MONTH_AND_DAY always captures its day")
            .as_str();
        let Ok(day) = day_text.parse::<u32>() else {
            continue;
        };
        let match_start_char = byte_to_char(text, m.start());
        let match_end_char = byte_to_char(text, m.end());
        // Python `text[match.end():match.end() + YEAR_WINDOW]` slices chars;
        // the tail window is 22 characters, not bytes.
        let tail: String = text[m.end()..].chars().take(YEAR_WINDOW).collect();
        let (year, end_char) = if let Some(full) = FULL_YEAR_RE.find(tail.as_str()) {
            // A four-digit year just past the day wins over anything shorter:
            // "March 24 11 am 1862" is 1862, not 1811.
            let Ok(year) = full.as_str().parse::<i32>() else {
                continue;
            };
            (
                year,
                match_end_char + byte_to_char(tail.as_str(), full.end()),
            )
        } else if let Some(caps) = SLASHED_YEAR_RE.captures(tail.as_str()) {
            // `^`-anchored, so this is Python `match` on the tail: any hit
            // starts at 0.
            let whole = caps.get(0).expect("SLASHED_YEAR always matches");
            let digits = caps
                .get(1)
                .expect("SLASHED_YEAR always captures digits")
                .as_str();
            let Some(century) = century else {
                continue;
            };
            let Ok(two) = digits.parse::<i64>() else {
                continue;
            };
            let Ok(year) = i32::try_from(century + two) else {
                continue;
            };
            (
                year,
                match_end_char + byte_to_char(tail.as_str(), whole.end()),
            )
        } else {
            continue;
        };
        if let Some(parsed) = day_of(year, month, day) {
            found.push((match_start_char, end_char, parsed));
        }
    }
    found
}

/// The century this text is written about, from the years it spells out:
/// e.g. 1800 when four-digit years are mostly 18xx. Needs at least five
/// stated years with half or more in one century.
pub fn dominant_century(text: &str) -> Option<i64> {
    let years: Vec<i64> = FULL_YEAR_RE
        .find_iter(text)
        .filter_map(|m| m.as_str().parse::<i64>().ok())
        .collect();
    if years.len() < 5 {
        return None;
    }
    // First-seen order: Python `max` keeps the earliest maximal century.
    let mut counts: Vec<(i64, usize)> = Vec::new();
    for year in &years {
        let century = year / 100 * 100;
        if let Some(entry) = counts.iter_mut().find(|(c, _)| *c == century) {
            entry.1 += 1;
        } else {
            counts.push((century, 1));
        }
    }
    let mut best = counts[0];
    for candidate in counts.iter().skip(1) {
        if candidate.1 > best.1 {
            best = *candidate;
        }
    }
    // `best[1] >= len(years) / 2`: integer doubling, exact for all lengths.
    if best.1 * 2 >= years.len() {
        Some(best.0)
    } else {
        None
    }
}

/// A month number from a word a scanner may have damaged: exact table,
/// then the OCR table, then a `difflib`-ratio similarity fallback.
pub fn read_month(word: &str) -> Option<u32> {
    // Python `word.strip(".").lower()`; inputs are ASCII month words and
    // `[A-Za-z]{3,9}` scan tokens, where Rust `to_lowercase` agrees with
    // `str.lower` (exotic casings such as Turkish dotted-İ are out of scope).
    let cleaned = word.trim_matches('.').to_lowercase();
    if let Some((_, month)) = MONTHS.iter().find(|(name, _)| *name == cleaned) {
        return Some(*month);
    }
    if let Some((_, month)) = MONTH_OCR.iter().find(|(name, _)| *name == cleaned) {
        return Some(*month);
    }
    // `difflib.get_close_matches(cleaned, MONTHS.keys(), n=1,
    // cutoff=0.75)`: keep every candidate passing all three filters, then
    // take the max by `(ratio, key)`. CPython 3.13's `heapq.nlargest(1, ...)`
    // delegates to `max`, so exact-ratio ties resolve to the
    // lexicographically largest key (probed: `ma`→`may`, `ju`→`jun`,
    // `jun`+`jul` both 0.8 yet `jun` wins).
    let mut best: Option<(f64, &str)> = None;
    for (key, _) in MONTHS.iter() {
        let (real_quick, quick, ratio) = difflib_triple(key, &cleaned);
        if real_quick >= MONTH_SIMILARITY && quick >= MONTH_SIMILARITY && ratio >= MONTH_SIMILARITY
        {
            let wins = match best {
                None => true,
                Some((best_ratio, best_key)) => {
                    ratio > best_ratio || (ratio == best_ratio && *key > best_key)
                }
            };
            if wins {
                best = Some((ratio, key));
            }
        }
    }
    best.and_then(|(_, key)| {
        MONTHS
            .iter()
            .find(|(name, _)| *name == key)
            .map(|(_, m)| *m)
    })
}

/// `difflib.SequenceMatcher(None, a, b).ratio()` ported exactly: junk-free
/// below 200 characters, matching blocks chained by longest match.
pub fn difflib_ratio(a: &str, b: &str) -> f64 {
    difflib_triple(a, b).2
}

/// Python `calendar.monthrange` leap rule: proleptic Gregorian.
pub fn month_length(year: i32, month: u32) -> u32 {
    // `calendar.monthrange` leap rule with Python `%` (floored) semantics via
    // `rem_euclid`, so pre-astronomical years agree too.
    let leap = year.rem_euclid(4) == 0 && (year.rem_euclid(100) != 0 || year.rem_euclid(400) == 0);
    match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Python `str.strip()` with no args strips `str.isspace()`, which covers
/// U+001C–U+001F beyond Rust's `char::is_whitespace` (Unicode White_Space).
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Byte offset in `text` as a char offset (Python `str` indices count chars).
fn byte_to_char(text: &str, byte_idx: usize) -> usize {
    text[..byte_idx].chars().count()
}

fn span(
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    precision: marginalia_types::common::DatePrecision,
) -> FuzzyDate {
    FuzzyDate {
        start,
        end,
        precision,
    }
}

fn at_day(year: i32, month: u32, day: u32) -> DateTime<Utc> {
    // Proof the date is always valid: every caller passes month/day already
    // checked against `month_length` (`day_of` checks, `month_of` uses day 1
    // and the month's last day, seasons use fixed table dates, decades and
    // years use Jan 1), and years come from `\d{3,4}` (at most 9999, inside
    // chrono's range).
    let naive = NaiveDate::from_ymd_opt(year, month, day)
        .expect("callers pass validated dates")
        .and_hms_opt(0, 0, 0)
        .expect("midnight is valid");
    Utc.from_utc_datetime(&naive)
}

fn end_of_day(year: i32, month: u32, day: u32) -> DateTime<Utc> {
    // Same proof as `at_day`: validated month/day, bounded year.
    let naive = NaiveDate::from_ymd_opt(year, month, day)
        .expect("callers pass validated dates")
        .and_hms_opt(23, 59, 59)
        .expect("end of day is valid");
    Utc.from_utc_datetime(&naive)
}

fn day_of(year: i32, month: u32, day: u32) -> Option<FuzzyDate> {
    if !(1..=12).contains(&month) {
        return None;
    }
    let last = month_length(year, month);
    if day < 1 || day > last {
        // "The 31st" of a thirty-day month is a transcription or OCR error.
        return None;
    }
    Some(span(
        at_day(year, month, day),
        end_of_day(year, month, day),
        marginalia_types::common::DatePrecision::Day,
    ))
}

fn month_of(year: i32, month: u32) -> Option<FuzzyDate> {
    // No range check: the only caller passes `lookup_month` results
    // (1..=12), so month/day are valid by construction (see `at_day`).
    let last = month_length(year, month);
    Some(span(
        at_day(year, month, 1),
        end_of_day(year, month, last),
        marginalia_types::common::DatePrecision::Month,
    ))
}

/// Python `_add_days`: shift by whole days, then pin to 23:59:59.
fn add_days(moment: &DateTime<Utc>, days: i64) -> DateTime<Utc> {
    // Proof the shift cannot overflow: the only caller passes week starts
    // parsed from `\d{4}` years (at most 9999) shifted by 6 days, nowhere
    // near chrono's ±262000-year ceiling.
    let shifted = moment
        .checked_add_signed(Duration::days(days))
        .expect("parsed dates sit far from chrono's ceiling");
    let naive = shifted
        .date_naive()
        .and_hms_opt(23, 59, 59)
        .expect("end of day is valid");
    Utc.from_utc_datetime(&naive)
}

/// A month with no year is the most recent occurrence of it.
fn year_for(anchor: &DateTime<Utc>, month: u32) -> i32 {
    if month <= anchor.month() {
        anchor.year()
    } else {
        anchor.year() - 1
    }
}

fn shift_month(year: i32, month: u32, offset: i32) -> (i32, u32) {
    // Python `//`/`%` are floored; `div_euclid`/`rem_euclid` match.
    let index = year as i64 * 12 + (month as i64 - 1) + offset as i64;
    (
        index.div_euclid(12) as i32,
        (index.rem_euclid(12) + 1) as u32,
    )
}

/// A numbered capture group, which always participates when the overall
/// match succeeds: every pattern here spells its groups bare, never under a
/// quantifier or inside an alternation branch that could skip one.
fn group<'t>(caps: &regex::Captures<'t>, index: usize) -> &'t str {
    caps.get(index)
        .expect("numbered group participates on match")
        .as_str()
}

fn lookup_month(word: &str) -> u32 {
    // Proof the lookup always hits: the only callers pass group 1 of the
    // month patterns, which match exactly the `MONTHS` keys
    // (case-insensitively), so the lowercased word is always a key.
    let lower = word.to_lowercase();
    MONTHS
        .iter()
        .find(|(name, _)| *name == lower)
        .map(|(_, month)| *month)
        .expect("month alternation only matches MONTHS names")
}

/// `\d{1,4}` groups are ASCII in every test; `str::parse` refuses
/// non-ASCII decimal digits (which Python `int()` would accept) rather than
/// guessing — the only intentional divergence, and untested upstream.
/// Proof parsing never fails: every caller passes a `\d{1,4}` group, at most
/// 9999, far below `i32::MAX`.
fn parse_int(text: &str) -> i32 {
    text.parse::<i32>().expect("regex digits fit i32")
}

fn try_iso(text: &str) -> Option<FuzzyDate> {
    let caps = ISO_RE.captures(text)?;
    let year = parse_int(group(&caps, 1));
    let month = parse_int(group(&caps, 2));
    let day = parse_int(group(&caps, 3));
    day_of(year, month as u32, day as u32)
}

fn try_numeric(text: &str) -> Option<FuzzyDate> {
    // `5/15/1862`, read month-first: the American convention.
    let caps = NUMERIC_RE.captures(text)?;
    let month = parse_int(group(&caps, 1));
    let day = parse_int(group(&caps, 2));
    let year = parse_int(group(&caps, 3));
    day_of(year, month as u32, day as u32)
}

fn try_month_day_year(text: &str) -> Option<FuzzyDate> {
    let caps = MONTH_DAY_YEAR_RE.captures(text)?;
    let month = lookup_month(group(&caps, 1));
    let day = parse_int(group(&caps, 2));
    let year = parse_int(group(&caps, 3));
    day_of(year, month, day as u32)
}

fn try_day_month_year(text: &str) -> Option<FuzzyDate> {
    let caps = DAY_MONTH_YEAR_RE.captures(text)?;
    let day = parse_int(group(&caps, 1));
    let month = lookup_month(group(&caps, 2));
    let year = parse_int(group(&caps, 3));
    day_of(year, month, day as u32)
}

fn try_month_year(text: &str) -> Option<FuzzyDate> {
    let caps = MONTH_YEAR_RE.captures(text)?;
    let month = lookup_month(group(&caps, 1));
    let year = parse_int(group(&caps, 2));
    month_of(year, month)
}

fn try_season(text: &str) -> Option<FuzzyDate> {
    let caps = SEASON_RE.captures(text)?;
    let season = group(&caps, 1).to_lowercase();
    // Proof the season is always known: `SEASON_RE` only matches the five
    // `SEASONS` names (case-insensitively), lowercased above.
    let (_, (start_month, start_day, end_month, end_day, year_shift)) = SEASONS
        .iter()
        .find(|(name, _)| *name == season)
        .expect("SEASON_RE only matches the five SEASONS names");
    let year = parse_int(group(&caps, 2));
    let end_year = year + year_shift;
    // February's end day is leap-resolved in the END year: "winter of 1863"
    // ends 1864-02-29, "winter of 1864" ends 1865-02-28.
    let end_day = if *end_month == 2 {
        month_length(end_year, 2)
    } else {
        *end_day
    };
    Some(span(
        at_day(year, *start_month, *start_day),
        end_of_day(end_year, *end_month, end_day),
        marginalia_types::common::DatePrecision::Season,
    ))
}

fn try_decade(text: &str) -> Option<FuzzyDate> {
    let caps = DECADE_RE.captures(text)?;
    // Proof the multiply cannot overflow: three digits top out at 999.
    let start_year = parse_int(group(&caps, 1))
        .checked_mul(10)
        .expect("three decade digits times ten fit i32");
    Some(span(
        at_day(start_year, 1, 1),
        end_of_day(start_year + 9, 12, 31),
        marginalia_types::common::DatePrecision::Decade,
    ))
}

fn try_year(text: &str) -> Option<FuzzyDate> {
    let caps = YEAR_RE.captures(text)?;
    let year = parse_int(group(&caps, 1));
    Some(span(
        at_day(year, 1, 1),
        end_of_day(year, 12, 31),
        marginalia_types::common::DatePrecision::Year,
    ))
}

/// The epistolary forms, resolved against the letter's own date.
fn try_relative(text: &str, anchor: DateTime<Utc>) -> Option<FuzzyDate> {
    if TODAY_RE.is_match(text) {
        return day_of(anchor.year(), anchor.month(), anchor.day());
    }
    if YESTERDAY_RE.is_match(text) {
        let previous = anchor.checked_sub_signed(Duration::days(1))?;
        return day_of(previous.year(), previous.month(), previous.day());
    }

    if let Some(caps) = RELATIVE_MONTH_RE.captures(text) {
        let day = parse_int(group(&caps, 1));
        let keyword = group(&caps, 2).to_lowercase();
        let keyword = keyword.trim_end_matches('.');
        // First prefix match in `ult`/`inst`/`prox` order; the alternatives
        // are disjoint on their first letter, so order never decides. The
        // regex alternatives all start with one of the three (matched
        // case-insensitively, lowercased above), so anything reaching the
        // final arm names next month — the `prox` arm Python's prefix
        // search resolves for the same inputs.
        let offset = if keyword.starts_with("ult") {
            -1
        } else if keyword.starts_with("inst") {
            0
        } else {
            1
        };
        let (year, month) = shift_month(anchor.year(), anchor.month(), offset);
        return day_of(year, month, day as u32);
    }

    if let Some(caps) = BARE_DAY_RE.captures(text) {
        // "The 15th" with no month named is the current month by convention —
        // and if that day has not arrived yet when the letter was written,
        // the writer meant last month.
        let day = parse_int(group(&caps, 1));
        let (mut year, mut month) = (anchor.year(), anchor.month());
        if day as u32 > anchor.day() {
            (year, month) = shift_month(year, month, -1);
        }
        return day_of(year, month, day as u32);
    }

    if let Some(caps) = MONTH_DAY_RE.captures(text) {
        let month = lookup_month(group(&caps, 1));
        let day = parse_int(group(&caps, 2));
        return day_of(year_for(&anchor, month), month, day as u32);
    }

    if let Some(caps) = DAY_MONTH_RE.captures(text) {
        let day = parse_int(group(&caps, 1));
        let month = lookup_month(group(&caps, 2));
        return day_of(year_for(&anchor, month), month, day as u32);
    }

    None
}

/// `(real_quick_ratio, quick_ratio, ratio)` over char sequences, mirroring
/// `difflib.SequenceMatcher(None, a, b)` with `a` the candidate and `b` the
/// word — the order `get_close_matches` uses (`set_seq2(word)`, then
/// `set_seq1(candidate)`), which fixes longest-match tie-breaking.
fn difflib_triple(a: &str, b: &str) -> (f64, f64, f64) {
    let a_chars: Vec<char> = a.chars().collect();
    let b_chars: Vec<char> = b.chars().collect();
    let real_quick = real_quick_ratio(a_chars.len(), b_chars.len());
    let quick = quick_ratio(&a_chars, &b_chars);
    let ratio = sequence_ratio(&a_chars, &b_chars);
    (real_quick, quick, ratio)
}

fn calc_ratio(matches: usize, length: usize) -> f64 {
    if length > 0 {
        2.0 * matches as f64 / length as f64
    } else {
        1.0
    }
}

fn real_quick_ratio(la: usize, lb: usize) -> f64 {
    calc_ratio(la.min(lb), la + lb)
}

/// Multiset intersection over chars: order-free upper bound on the ratio.
fn quick_ratio(a: &[char], b: &[char]) -> f64 {
    let mut full: HashMap<char, usize> = HashMap::new();
    for elt in b {
        *full.entry(*elt).or_insert(0) += 1;
    }
    let mut avail: HashMap<char, isize> = HashMap::new();
    let mut matches = 0usize;
    for elt in a {
        let numb = if let Some(n) = avail.get(elt) {
            *n
        } else {
            *full.get(elt).unwrap_or(&0) as isize
        };
        avail.insert(*elt, numb - 1);
        if numb > 0 {
            matches += 1;
        }
    }
    calc_ratio(matches, a.len() + b.len())
}

fn sequence_ratio(a: &[char], b: &[char]) -> f64 {
    let matches: usize = matching_blocks(a, b).iter().map(|(_, _, n)| *n).sum();
    calc_ratio(matches, a.len() + b.len())
}

/// `get_matching_blocks` without junk: `isjunk` is `None` and month words are
/// far under the 200-char autojunk threshold, so `b2j` holds every char and
/// there are no extend-through-junk passes — the chained longest run is the
/// match.
fn matching_blocks(a: &[char], b: &[char]) -> Vec<(usize, usize, usize)> {
    let la = a.len();
    let lb = b.len();
    // `b2j`: ascending index lists per char, exactly `__chain_b` order.
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (i, elt) in b.iter().enumerate() {
        b2j.entry(*elt).or_default().push(i);
    }
    let mut queue = vec![(0usize, la, 0usize, lb)];
    let mut blocks: Vec<(usize, usize, usize)> = Vec::new();
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = find_longest_match(a, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            blocks.push((i, j, k));
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    blocks.sort();
    // Collapse adjacent equal blocks (Python ≥2.5 guarantee).
    let mut merged: Vec<(usize, usize, usize)> = Vec::new();
    let (mut i1, mut j1, mut k1) = (0usize, 0usize, 0usize);
    for (i2, j2, k2) in blocks {
        if i1 + k1 == i2 && j1 + k1 == j2 {
            k1 += k2;
        } else {
            if k1 > 0 {
                merged.push((i1, j1, k1));
            }
            (i1, j1, k1) = (i2, j2, k2);
        }
    }
    if k1 > 0 {
        merged.push((i1, j1, k1));
    }
    merged.push((la, lb, 0));
    merged
}

/// Earliest-in-`a`, then earliest-in-`b`, among maximal matches: strict `>`
/// keeps the first maximal block, and `b2j` lists ascend. Takes `b2j`
/// instead of `b`: with no junk-extension passes nothing indexes `b`
/// directly.
fn find_longest_match(
    a: &[char],
    b2j: &HashMap<char, Vec<usize>>,
    alo: usize,
    ahi: usize,
    blo: usize,
    bhi: usize,
) -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
    let mut j2len: HashMap<usize, usize> = HashMap::new();
    for (i, &ch) in a.iter().enumerate().take(ahi).skip(alo) {
        let mut newj2len: HashMap<usize, usize> = HashMap::new();
        if let Some(idxs) = b2j.get(&ch) {
            for j in idxs {
                let j = *j;
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let k = j2len.get(&(j.wrapping_sub(1))).copied().unwrap_or(0) + 1;
                newj2len.insert(j, k);
                if k > bestsize {
                    (besti, bestj, bestsize) = (i + 1 - k, j + 1 - k, k);
                }
            }
        }
        j2len = newj2len;
    }
    // No junk-extension passes: the main loop chains every in-window pair
    // through `j2len` and records the longest run, so extending that run
    // in either direction would itself be a longer run the main loop had
    // already recorded — a contradiction. (CPython's passes exist to
    // extend through junk characters; `isjunk` is `None` here, so they are
    // no-ops — confirmed by exhaustive binary-alphabet fuzzing to length
    // 5 plus 460k random and month-shaped trials, in which neither loop
    // ever fired.)
    (besti, bestj, bestsize)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use marginalia_types::common::DatePrecision;

    fn anchor() -> DateTime<Utc> {
        // McClellan on the Peninsula, mirroring the Python `ANCHOR`.
        midnight(1862, 5, 20)
    }

    fn midnight(year: i32, month: u32, day: u32) -> DateTime<Utc> {
        let naive = NaiveDate::from_ymd_opt(year, month, day)
            .expect("fixture date is valid")
            .and_hms_opt(0, 0, 0)
            .expect("midnight is valid");
        Utc.from_utc_datetime(&naive)
    }

    fn day_of(text: &str, relative_to: Option<DateTime<Utc>>) -> FuzzyDate {
        parse_fuzzy_date(text, relative_to).unwrap_or_else(|| panic!("{text:?} did not parse"))
    }

    fn ymd(date: &DateTime<Utc>) -> (i32, u32, u32) {
        (date.year(), date.month(), date.day())
    }

    fn ranged(text: &str) -> (FuzzyDate, usize, usize) {
        let found = scan_dates(text, None);
        assert_eq!(found.len(), 1, "{text:?} should yield exactly one date");
        let (start, end, date) = found.into_iter().next().expect("exactly one date");
        (date, start, end)
    }

    fn char_slice(text: &str, start: usize, end: usize) -> String {
        text.chars().skip(start).take(end - start).collect()
    }

    // --- TestFullDates ---

    #[test]
    fn test_the_same_day_written_seven_ways() {
        // Mirrors `TestFullDates::test_the_same_day_written_seven_ways`.
        for text in [
            "May 15, 1862",
            "May 15th, 1862",
            "May 15th 1862",
            "15 May 1862",
            "15th May, 1862",
            "1862-05-15",
            "5/15/1862",
        ] {
            let result = day_of(text, None);
            assert_eq!(ymd(&result.start), (1862, 5, 15), "{text:?}");
            assert_eq!(result.precision, DatePrecision::Day, "{text:?}");
        }
    }

    #[test]
    fn test_period_month_abbreviations() {
        // Mirrors `TestFullDates::test_period_month_abbreviations`.
        for (text, month) in [
            ("Jany 3d, 1863", 1),
            ("Feby 3d, 1863", 2),
            ("Sept 3d, 1863", 9),
            ("Octr 3d, 1863", 10),
        ] {
            assert_eq!(day_of(text, None).start.month(), month, "{text:?}");
        }
    }

    #[test]
    fn test_period_ordinals() {
        // Mirrors `TestFullDates::test_period_ordinals`.
        for text in [
            "May 2d, 1862",
            "May 2nd, 1862",
            "May 3d, 1862",
            "May 3rd, 1862",
        ] {
            assert_eq!(day_of(text, None).precision, DatePrecision::Day, "{text:?}");
        }
    }

    // --- TestCoarserPrecision ---

    #[test]
    fn test_month_spans_the_month() {
        let result = day_of("May 1862", None);
        assert_eq!((result.start.day(), result.end.day()), (1, 31));
        assert_eq!(result.precision, DatePrecision::Month);
    }

    #[test]
    fn test_february_in_a_leap_year() {
        assert_eq!(day_of("February 1864", None).end.day(), 29);
    }

    #[test]
    fn test_century_february_resolves_the_gregorian_exception() {
        // Probed against CPython `calendar.monthrange`: 1900 is not a leap
        // year (28 days) while 2000 is (29 days).
        assert_eq!(month_length(1900, 2), 28);
        assert_eq!(month_length(2000, 2), 29);
        assert_eq!(day_of("February 1900", None).end.day(), 28);
        assert_eq!(day_of("February 2000", None).end.day(), 29);
    }

    #[test]
    fn test_year_spans_the_year() {
        let result = day_of("1862", None);
        assert_eq!((result.start.month(), result.end.month()), (1, 12));
        assert_eq!(result.precision, DatePrecision::Year);
    }

    #[test]
    fn test_season() {
        let result = day_of("spring of 1862", None);
        assert_eq!((result.start.month(), result.end.month()), (3, 5));
        assert_eq!(result.precision, DatePrecision::Season);
    }

    #[test]
    fn test_winter_runs_into_the_following_year() {
        let result = day_of("winter of 1862", None);
        assert_eq!(ymd(&result.start), (1862, 12, 1));
        assert_eq!((result.end.year(), result.end.month()), (1863, 2));
    }

    #[test]
    fn test_winter_february_uses_the_end_year_leap_rule() {
        // Probed: winter of 1863 ends 1864-02-29; winter of 1864 ends 1865-02-28.
        assert_eq!(ymd(&day_of("winter of 1863", None).end), (1864, 2, 29));
        assert_eq!(ymd(&day_of("winter of 1864", None).end), (1865, 2, 28));
    }

    #[test]
    fn test_decade() {
        let result = day_of("1860s", None);
        assert_eq!((result.start.year(), result.end.year()), (1860, 1869));
        assert_eq!(result.precision, DatePrecision::Decade);
    }

    #[test]
    fn test_week_of() {
        let result = day_of("week of May 15, 1862", None);
        assert_eq!((result.start.day(), result.end.day()), (15, 21));
        assert_eq!(result.precision, DatePrecision::Week);
    }

    #[test]
    fn test_week_of_a_relative_form() {
        // Probed: the inner phrase resolves against the anchor first.
        let result = day_of("week of the 15th", Some(anchor()));
        assert_eq!(
            (ymd(&result.start), ymd(&result.end)),
            ((1862, 5, 15), (1862, 5, 21))
        );
        assert_eq!(result.precision, DatePrecision::Week);
    }

    // --- TestEpistolaryForms ---

    #[test]
    fn test_ultimo_is_the_previous_month() {
        assert_eq!(
            ymd(&day_of("the 3d ult.", Some(anchor())).start),
            (1862, 4, 3)
        );
    }

    #[test]
    fn test_instant_is_the_current_month() {
        assert_eq!(
            ymd(&day_of("the 15th inst.", Some(anchor())).start),
            (1862, 5, 15)
        );
    }

    #[test]
    fn test_proximo_is_the_next_month() {
        assert_eq!(
            ymd(&day_of("the 2d prox.", Some(anchor())).start),
            (1862, 6, 2)
        );
    }

    #[test]
    fn test_long_and_short_forms() {
        // Mirrors `TestEpistolaryForms::test_long_and_short_forms`.
        for text in ["the 3d ult.", "the 3d ulto", "the 3d ultimo"] {
            assert_eq!(day_of(text, Some(anchor())).start.month(), 4, "{text:?}");
        }
    }

    #[test]
    fn test_a_lead_in_phrase_is_stripped() {
        assert_eq!(
            day_of("yours of the 15th inst.", Some(anchor()))
                .start
                .day(),
            15
        );
    }

    #[test]
    fn test_a_bare_day_already_past_is_this_month() {
        assert_eq!(
            ymd(&day_of("the 15th", Some(anchor())).start),
            (1862, 5, 15)
        );
    }

    #[test]
    fn test_a_bare_day_still_to_come_is_last_month() {
        assert_eq!(
            ymd(&day_of("the 25th", Some(anchor())).start),
            (1862, 4, 25)
        );
    }

    #[test]
    fn test_a_month_without_a_year_is_the_most_recent_one() {
        assert_eq!(day_of("June 3d", Some(anchor())).start.year(), 1861);
    }

    #[test]
    fn test_ultimo_crossing_a_year_boundary() {
        let january = midnight(1863, 1, 10);
        let result = day_of("the 28th ult.", Some(january));
        assert_eq!((result.start.year(), result.start.month()), (1862, 12));
    }

    // --- TestRefusals ---

    #[test]
    fn test_a_relative_form_without_an_anchor_is_refused() {
        assert_eq!(parse_fuzzy_date("the 3d ult.", None), None);
    }

    #[test]
    fn test_a_bare_day_without_an_anchor_is_refused() {
        assert_eq!(parse_fuzzy_date("the 15th", None), None);
    }

    #[test]
    fn test_a_phrase_that_is_not_a_date_is_refused() {
        // Mirrors `TestRefusals::test_a_phrase_that_is_not_a_date_is_refused`.
        for text in ["your last", "some time ago", "recently", ""] {
            assert_eq!(parse_fuzzy_date(text, Some(anchor())), None, "{text:?}");
        }
    }

    #[test]
    fn test_an_impossible_day_is_refused() {
        assert_eq!(parse_fuzzy_date("April 31, 1862", None), None);
    }

    #[test]
    fn test_an_impossible_month_is_refused() {
        assert_eq!(parse_fuzzy_date("1862-13-01", None), None);
    }

    #[test]
    fn test_yesterday_at_the_chrono_floor_is_refused() {
        // One day before `DateTime::MIN_UTC` is unrepresentable, so the
        // relative form is refused instead of overflowing.
        assert_eq!(
            parse_fuzzy_date("yesterday", Some(DateTime::<Utc>::MIN_UTC)),
            None
        );
    }

    #[test]
    #[should_panic(expected = "did not parse")]
    fn test_day_of_helper_panics_on_an_unparsable_phrase() {
        day_of("your last", Some(anchor()));
    }

    #[test]
    fn test_none_and_whitespace() {
        assert_eq!(parse_fuzzy_date("", None), None);
        assert_eq!(parse_fuzzy_date("   ", None), None);
    }

    // --- TestScanningScannedText ---

    #[test]
    fn test_a_plain_dateline() {
        let (date, _, _) = ranged("Cincinnati, April 24, 1861");
        assert_eq!(ymd(&date.start), (1861, 4, 24));
    }

    #[test]
    fn test_no_comma_between_place_and_date() {
        let (date, _, _) = ranged("Head Quarters OVM Cincinnati April 29 1861");
        assert_eq!(date.start.day(), 29);
    }

    #[test]
    fn test_a_year_the_editor_supplied() {
        let (date, _, _) = ranged("Cincinnati Dec 27 [1860]");
        assert_eq!(date.start.year(), 1860);
    }

    #[test]
    fn test_a_month_the_scanner_misread() {
        let (date, _, _) = ranged("Cincinnati Dee 27 [1860]");
        assert_eq!(date.start.month(), 12);
    }

    #[test]
    fn test_a_month_misread_no_one_listed() {
        let (date, _, _) = ranged("Camp near Sharpsburg, Marcli 24, 1862");
        assert_eq!(date.start.month(), 3);
    }

    #[test]
    fn test_a_slashed_short_year_needs_a_century() {
        assert_eq!(scan_dates("Cincinnati April 18/61", None), vec![]);
        let found = scan_dates("Cincinnati April 18/61", Some(1800));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].2.start.year(), 1861);
    }

    #[test]
    fn test_the_month_and_day_split_by_a_bracket() {
        let (date, _, _) = ranged("[Washington, August] 16th [1861]");
        assert_eq!(ymd(&date.start), (1861, 8, 16));
    }

    #[test]
    fn test_a_telegram_time_is_not_a_year() {
        let (date, _, _) =
            ranged("Head Quarters, Army of the Potomac, Seminary March 24 11 am 1862");
        assert_eq!(date.start.year(), 1862);
        assert_eq!(date.start.month(), 3);
    }

    #[test]
    fn test_other_telegram_times() {
        // Mirrors `TestScanningScannedText::test_other_telegram_times`.
        for line in [
            "Near Yorktown April 11 12.30 am 1862",
            "Berkeley August 4 12m 1862",
            "Camp Lincoln June 14, 11 a.m. 1862",
        ] {
            let (date, _, _) = ranged(line);
            assert_eq!(date.start.year(), 1862, "{line:?}");
        }
    }

    #[test]
    fn test_an_editor_s_alternative_day_is_stepped_over() {
        let (date, _, _) = ranged("## Washington Aug 9 [10] 1861 1 am.");
        assert_eq!(date.start.year(), 1861);
    }

    #[test]
    fn test_dates_come_back_in_document_order() {
        let found = scan_dates("May 3, 1862 ... June 4, 1862 ... July 5, 1862", None);
        let months: Vec<u32> = found.iter().map(|(_, _, d)| d.start.month()).collect();
        assert_eq!(months, vec![5, 6, 7]);
    }

    #[test]
    fn test_the_span_addresses_the_text_it_read() {
        let text = "Camp near Sharpsburg, Sept. 20, 1862 — my dear Nelly";
        let (_, start, end) = ranged(text);
        assert_eq!(
            char_slice(text, start, end).trim().trim_end_matches(','),
            "Sept. 20, 1862"
        );
    }

    #[test]
    fn test_ordinary_prose_yields_nothing() {
        assert_eq!(
            scan_dates("I have 3 brigades and 4 batteries in the field.", None),
            vec![]
        );
    }

    // --- TestDominantCentury ---

    #[test]
    fn test_read_from_the_years_the_text_states() {
        assert_eq!(
            dominant_century("1861 1862 1862 1863 1864 1865"),
            Some(1800)
        );
    }

    #[test]
    fn test_refused_when_there_is_too_little_to_go_on() {
        assert_eq!(dominant_century("1862"), None);
    }

    #[test]
    fn test_refused_when_the_text_is_split_between_centuries() {
        assert_eq!(
            dominant_century("1861 1862 1863 1961 1962 1963 2001 2002"),
            None
        );
    }

    // --- TestPhrasesTheLettersActuallyUse ---

    #[test]
    fn test_a_letter_referred_to_by_day() {
        // Mirrors `TestPhrasesTheLettersActuallyUse::test_a_letter_referred_to_by_day`.
        for (phrase, day) in [
            ("yours of the 2nd", 2),
            ("your letters of the 19", 19),
            ("the 19", 19),
            ("your communication of the 14th inst", 14),
            ("your confidential letter of the 23rd", 23),
            ("your very kind letter of the 3d", 3),
        ] {
            assert_eq!(
                day_of(phrase, Some(anchor())).start.day(),
                day,
                "{phrase:?}"
            );
        }
    }

    #[test]
    fn test_of_today() {
        assert_eq!(
            day_of("your telegram of today", Some(anchor()))
                .start
                .date_naive(),
            anchor().date_naive()
        );
    }

    #[test]
    fn test_of_yesterday() {
        let result = day_of("your note of yesterday", Some(anchor()));
        assert_eq!(result.start.day(), anchor().day() - 1);
    }

    #[test]
    fn test_yesterday_across_a_month_boundary() {
        let first = midnight(1862, 3, 1);
        let result = day_of("yesterday", Some(first));
        assert_eq!((result.start.month(), result.start.day()), (2, 28));
    }

    #[test]
    fn test_a_bare_day_still_needs_an_anchor() {
        assert_eq!(parse_fuzzy_date("the 19", None), None);
    }

    // --- Probed boundaries beyond the suite ---

    #[test]
    fn test_probed_lead_and_case_boundaries() {
        // Each probed live against CPython 3.13 before porting.
        assert_eq!(ymd(&day_of("3d ult.", Some(anchor())).start), (1862, 4, 3));
        assert_eq!(day_of("15th ultimo", Some(anchor())).start.month(), 4);
        assert_eq!(day_of("15th proximo", Some(anchor())).start.month(), 6);
        assert_eq!(day_of("15th instant", Some(anchor())).start.month(), 5);
        assert_eq!(ymd(&day_of("MAY 15TH, 1862", None).start), (1862, 5, 15));
        assert_eq!(day_of("Sept. 20, 1862", None).start.day(), 20);
        assert_eq!(parse_fuzzy_date("15D", Some(anchor())), None);
        assert_eq!(parse_fuzzy_date("dated May 3d", None), None);
        assert_eq!(parse_fuzzy_date("week of nonsense", Some(anchor())), None);
        assert_eq!(parse_fuzzy_date("Feb 30, 1862", None), None);
        assert_eq!(parse_fuzzy_date("13/15/1862", None), None);
        assert_eq!(parse_fuzzy_date(" 1862s", None), None);
        assert_eq!(parse_fuzzy_date("of today", None), None);
    }

    // --- difflib vectors, captured live from CPython 3.13 ---

    #[test]
    fn test_difflib_ratios_match_cpython() {
        // `(a, b, real_quick, quick, ratio)` probed live; `a` is the month
        // key (candidate), `b` the damaged word, as `get_close_matches`
        // orders them.
        let vectors = [
            (
                "marcli",
                "march",
                0.9090909090909091,
                0.7272727272727273,
                0.7272727272727273,
            ),
            (
                "marcli",
                "mar",
                0.6666666666666666,
                0.6666666666666666,
                0.6666666666666666,
            ),
            ("dee", "dec", 1.0, 0.6666666666666666, 0.6666666666666666),
            (
                "sepc",
                "sep",
                0.8571428571428571,
                0.8571428571428571,
                0.8571428571428571,
            ),
            ("sepc", "sepr", 1.0, 0.75, 0.75),
            (
                "jann",
                "jan",
                0.8571428571428571,
                0.8571428571428571,
                0.8571428571428571,
            ),
            (
                "januaryy",
                "january",
                0.9333333333333333,
                0.9333333333333333,
                0.9333333333333333,
            ),
            (
                "maye",
                "may",
                0.8571428571428571,
                0.8571428571428571,
                0.8571428571428571,
            ),
            ("the", "may", 1.0, 0.0, 0.0),
            ("and", "aug", 1.0, 0.3333333333333333, 0.3333333333333333),
            ("xyz", "may", 1.0, 0.3333333333333333, 0.3333333333333333),
            (
                "mareli",
                "march",
                0.9090909090909091,
                0.5454545454545454,
                0.5454545454545454,
            ),
            (
                "jaimary",
                "january",
                1.0,
                0.7142857142857143,
                0.7142857142857143,
            ),
            (
                "apnl",
                "april",
                0.8888888888888888,
                0.6666666666666666,
                0.6666666666666666,
            ),
            ("oet", "oct", 1.0, 0.6666666666666666, 0.6666666666666666),
            ("xov", "nov", 1.0, 0.6666666666666666, 0.6666666666666666),
            ("", "may", 0.0, 0.0, 0.0),
            ("may", "may", 1.0, 1.0, 1.0),
            (
                "febuary",
                "february",
                0.9333333333333333,
                0.9333333333333333,
                0.9333333333333333,
            ),
            ("januray", "january", 1.0, 1.0, 0.8571428571428571),
            (
                "septembr",
                "september",
                0.9411764705882353,
                0.9411764705882353,
                0.9411764705882353,
            ),
            ("a", "may", 0.5, 0.5, 0.5),
            ("march", "march", 1.0, 1.0, 1.0),
            ("jnne", "june", 1.0, 0.75, 0.75),
            ("jnly", "july", 1.0, 0.75, 0.75),
            (
                "julv",
                "jul",
                0.8571428571428571,
                0.8571428571428571,
                0.8571428571428571,
            ),
            (
                "angust",
                "august",
                1.0,
                0.8333333333333334,
                0.8333333333333334,
            ),
            ("mareh", "march", 1.0, 0.8, 0.8),
        ];
        for (a, b, real_quick, quick, ratio) in vectors {
            let (rq, q, r) = difflib_triple(a, b);
            assert_eq!(rq, real_quick, "real_quick {a:?} {b:?}");
            assert_eq!(q, quick, "quick {a:?} {b:?}");
            assert_eq!(r, ratio, "ratio {a:?} {b:?}");
            assert_eq!(difflib_ratio(a, b), ratio, "difflib_ratio {a:?} {b:?}");
        }
    }

    #[test]
    fn test_read_month_matches_the_triple_filter() {
        // Battery probed live: `(word, expected)`, covering every OCR key,
        // month mutations, and non-months.
        let vectors = [
            ("dee", Some(12)),
            ("deo", Some(12)),
            ("dec'", Some(12)),
            ("jime", Some(6)),
            ("jnne", Some(6)),
            ("jnue", Some(6)),
            ("jnly", Some(7)),
            ("julv", Some(7)),
            ("angust", Some(8)),
            ("augnst", Some(8)),
            ("marcli", Some(3)),
            ("mareh", Some(3)),
            ("mareli", Some(3)),
            ("febmary", Some(2)),
            ("febrnary", Some(2)),
            ("jaimary", Some(1)),
            ("jannary", Some(1)),
            ("aprii", Some(4)),
            ("apl", Some(4)),
            ("apnl", Some(4)),
            ("oet", Some(10)),
            ("octr", Some(10)),
            ("xov", Some(11)),
            ("novr", Some(11)),
            ("sepr", Some(9)),
            ("sepc", Some(9)),
            ("march", Some(3)),
            ("Marcli", Some(3)),
            ("MARCH", Some(3)),
            ("dec", Some(12)),
            ("Dee", Some(12)),
            ("xyz", None),
            ("the", None),
            ("and", None),
            ("brigades", None),
            ("batteries", None),
            ("field", None),
            ("Sharpsburg", None),
            ("Cincinnati", None),
            ("jan", Some(1)),
            ("jann", Some(1)),
            ("januaryy", Some(1)),
            ("apri", Some(4)),
            ("maye", Some(5)),
            ("junne", Some(6)),
            ("july", Some(7)),
            ("augustt", Some(8)),
            ("septembr", Some(9)),
            ("octoberr", Some(10)),
            ("novembr", Some(11)),
            ("decembr", Some(12)),
            ("febuary", Some(2)),
            ("januray", Some(1)),
            ("3rd", None),
            ("1862", None),
            ("am", None),
            ("Near", None),
            ("Yorktown", None),
            ("Seminary", None),
            ("Potomac", None),
            // Exact-ratio ties resolve to the lexicographically largest key.
            ("ma", Some(5)),
            ("ju", Some(6)),
        ];
        for (word, expected) in vectors {
            assert_eq!(read_month(word), expected, "{word:?}");
        }
    }

    #[test]
    fn test_lead_only_phrase_parses_to_nothing() {
        // Probed: "on ," sheds its lead word, then the trailing comma,
        // leaving nothing — `None` with or without an anchor.
        assert_eq!(parse_fuzzy_date("on ,", None), None);
        assert_eq!(parse_fuzzy_date("on ,", Some(anchor())), None);
    }

    #[test]
    fn test_scan_refuses_non_ascii_digits() {
        // `str::parse` is ASCII-only where Python `int()` reads `\d`
        // (probed: CPython finds 1862-03-12, 1862-03-05, and 1899-03-05
        // here) — the one intentional divergence, so the scan yields
        // nothing instead of guessing.
        assert_eq!(scan_dates("March \u{661}\u{662} 1862", None), vec![]);
        assert_eq!(scan_dates("March 5 186\u{662}", None), vec![]);
        assert_eq!(scan_dates("March 5 /\u{669}\u{669}", Some(1800)), vec![]);
    }

    #[test]
    fn test_scan_year_overflow_and_missing_year_yield_nothing() {
        // A century that cannot land in an `i32` year refuses gracefully
        // (CPython raises `OverflowError` in `datetime` here); a day with
        // no year at all is not a date to the scanner either (probed `[]`).
        assert_eq!(scan_dates("March 5 /99", Some(3_000_000_000)), vec![]);
        assert_eq!(scan_dates("March 5", None), vec![]);
    }

    #[test]
    fn test_scan_skips_an_impossible_day() {
        // "February 30" matches the dateline scan but fails `day_of` (1862
        // is not a leap year, probed via `calendar.monthrange`), so the
        // candidate is skipped rather than reported.
        assert_eq!(scan_dates("February 30, 1862", None), vec![]);
    }

    #[test]
    fn test_later_century_wins_dominant_century() {
        // Probed: five stated years with three in the 1900s resolve to
        // 1900, exercising the best-tracking update.
        assert_eq!(dominant_century("1801 1802 1901 1902 1903"), Some(1900));
    }

    #[test]
    fn test_future_month_means_last_year() {
        // Probed against a May 1862 anchor: December's most recent
        // occurrence is December 1861, in both day-first and month-first
        // order.
        for text in ["December 25", "25 December"] {
            let result = day_of(text, Some(anchor()));
            assert_eq!(ymd(&result.start), (1861, 12, 25), "{text:?}");
            assert_eq!(result.precision, DatePrecision::Day, "{text:?}");
        }
    }
    #[test]
    fn test_current_or_past_month_means_this_year() {
        // The other arm of the same rule: May and March have occurred in
        // 1862 by a May 1862 anchor, so they resolve to this year.
        for (text, expected) in [
            ("May 3", (1862, 5, 3)),
            ("March 10", (1862, 3, 10)),
            ("3 May", (1862, 5, 3)),
        ] {
            let result = day_of(text, Some(anchor()));
            assert_eq!(ymd(&result.start), expected, "{text:?}");
            assert_eq!(result.precision, DatePrecision::Day, "{text:?}");
        }
    }

    #[test]
    fn test_empty_ratio_is_one() {
        // `SequenceMatcher(None, "", "").ratio() == 1.0`: no characters,
        // no mismatches.
        assert_eq!(difflib_ratio("", ""), 1.0);
    }
}
