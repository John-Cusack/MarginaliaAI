//! Correspondence-cadence math: bucketing, anomaly flags, and gap rhythm.
//!
//! Ports the pure core of `correspondence_cadence.tool_handler` (direction
//! split, `all_bins` timeline, silence/burst anomalies, summary) and the
//! rhythm core of `find_missing_letters._cadence` (median interval, the
//! two-week floor, gap confidence) plus `_count_by_method` and `RESOLVED`.
//!
//! What stays Python and why: both `tool_handler` entry points (they build
//! `EventFilter`s and read the event/extraction/entity/corpus clients),
//! `_referenced` (verdicts over live holdings), and `_as_uuid` /
//! `_as_datetime` (MCP-string validation glue for the orchestration that
//! stays — the strings it receives are validated at that boundary).

use std::collections::HashMap;

use chrono::{DateTime, FixedOffset};
use serde::Serialize;

/// Suffix core appends when it resolves a declared field type into
/// structured form. A literal here too: the pack must not reach into core's
/// service internals.
pub const RESOLVED_SUFFIX: &str = "_resolved";

/// Gap floor: a stretch counts as a gap only past twice the median interval,
/// and never below two weeks.
pub const MIN_GAP_DAYS: i64 = 14;

/// One correspondent letter: when it was sent and who sent it.
pub struct LetterEvent {
    /// `None` mirrors `timestamp_start=None`: counted in the totals, absent
    /// from every bucket.
    pub timestamp: Option<DateTime<FixedOffset>>,
    /// `payload["sender_entity_id"]` as a string.
    pub sender_id: String,
}

/// Which bins the timeline groups into. Anything but month/week is a day —
/// the Python `if/elif/else` falls through the same way.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeBin {
    Month,
    Week,
    Day,
}

/// Parse the `time_bin` argument: `"month"`, `"week"`, anything else a day.
pub fn parse_time_bin(time_bin: &str) -> TimeBin {
    match time_bin {
        "month" => TimeBin::Month,
        "week" => TimeBin::Week,
        _ => TimeBin::Day,
    }
}

/// `ts.strftime` for the three bins: `%Y-%m`, `%Y-W%W`, `%Y-%m-%d`.
///
/// `%W` is Monday-first with pre-first-Monday days in week 00 on both sides
/// (`chrono` documents the same definition CPython uses); the differential
/// pins early-January boundaries explicitly.
pub fn bucket_key(ts: &DateTime<FixedOffset>, bin: TimeBin) -> String {
    match bin {
        TimeBin::Month => ts.format("%Y-%m").to_string(),
        TimeBin::Week => ts.format("%Y-W%W").to_string(),
        TimeBin::Day => ts.format("%Y-%m-%d").to_string(),
    }
}

/// One timeline row: per-direction counts and their total for a bin.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BinRow {
    pub bin: String,
    pub a_to_b: usize,
    pub b_to_a: usize,
    pub total: usize,
}

/// The report summary. `average_per_bin` renders as integer `0` when there
/// are no bins — Python's `round(0, 1)` stays `int` on the empty path while
/// every non-empty average is a float — via [`zero_is_int`].
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CadenceSummary {
    pub total_letters: usize,
    pub a_to_b_count: usize,
    pub b_to_a_count: usize,
    pub time_span_bins: usize,
    #[serde(serialize_with = "zero_is_int")]
    pub average_per_bin: f64,
}

/// Integer `0` for the empty report, the float otherwise: exactly Python's
/// `round(avg, 1)` typing (`round(0, 1)` is `int`, `round(0.0, 1)` is `float`).
/// The value can only be zero when there are no bins — every listed bin holds
/// at least one letter — so the value alone selects the shape.
#[allow(clippy::trivially_copy_pass_by_ref)]
fn zero_is_int<S: serde::Serializer>(value: &f64, out: S) -> Result<S::Ok, S::Error> {
    if *value == 0.0 {
        out.serialize_u64(0)
    } else {
        out.serialize_f64(*value)
    }
}

/// One flagged bin: a silence (nothing where the rhythm says something) or a
/// burst (over three times the average). `count` is present on bursts only.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Anomaly {
    pub bin: String,
    #[serde(rename = "type")]
    pub anomaly_type: String,
    pub expected: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub count: Option<usize>,
}

/// The full cadence report: timeline, summary, anomalies.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CadenceReport {
    pub timeline: Vec<BinRow>,
    pub summary: CadenceSummary,
    pub anomalies: Vec<Anomaly>,
}

/// Analyze correspondence cadence between two correspondents.
///
/// Direction follows the sender: `sender_id == a_id` counts a-to-b, anything
/// else counts b-to-a — including letters whose sender is neither, exactly
/// like the Python `else`.
pub fn analyze(a_id: &str, _b_id: &str, events: &[LetterEvent], bin: TimeBin) -> CadenceReport {
    let mut a_to_b: HashMap<String, usize> = HashMap::new();
    let mut b_to_a: HashMap<String, usize> = HashMap::new();
    for event in events {
        let Some(ts) = event.timestamp else { continue };
        let key = bucket_key(&ts, bin);
        if event.sender_id == a_id {
            *a_to_b.entry(key).or_insert(0) += 1;
        } else {
            *b_to_a.entry(key).or_insert(0) += 1;
        }
    }
    let mut all_bins: Vec<String> = a_to_b.keys().chain(b_to_a.keys()).cloned().collect();
    all_bins.sort();
    all_bins.dedup();

    let totals: Vec<usize> = all_bins
        .iter()
        .map(|b| a_to_b.get(b).copied().unwrap_or(0) + b_to_a.get(b).copied().unwrap_or(0))
        .collect();
    let avg = if totals.is_empty() {
        0.0
    } else {
        totals.iter().sum::<usize>() as f64 / totals.len() as f64
    };

    // Every listed bin holds at least one letter: `all_bins` collects only
    // keys present in a direction map, each counted at least once, so every
    // total is >= 1. Two Python guards are therefore unsatisfiable and are
    // removed, not branched around: the `count == 0` "silence" arm, and the
    // `avg > 0` guard (the loop itself subsumes it — empty input means zero
    // iterations). If `all_bins` ever spans empty bins, re-examine this.
    let mut anomalies = Vec::new();
    for (i, b) in all_bins.iter().enumerate() {
        let count = totals[i];
        if count as f64 > avg * 3.0 {
            anomalies.push(Anomaly {
                bin: b.clone(),
                anomaly_type: "burst".to_owned(),
                expected: round1(avg),
                count: Some(count),
            });
        }
    }

    CadenceReport {
        timeline: all_bins
            .iter()
            .enumerate()
            .map(|(i, b)| BinRow {
                bin: b.clone(),
                a_to_b: a_to_b.get(b).copied().unwrap_or(0),
                b_to_a: b_to_a.get(b).copied().unwrap_or(0),
                total: totals[i],
            })
            .collect(),
        summary: CadenceSummary {
            total_letters: events.len(),
            a_to_b_count: a_to_b.values().sum(),
            b_to_a_count: b_to_a.values().sum(),
            time_span_bins: all_bins.len(),
            average_per_bin: round1(avg),
        },
        anomalies,
    }
}

/// Python's `round(x, 1)`: half to even on the exact binary value.
///
/// Callers only pass non-negative averages (sums of bucket counts over a
/// positive bin count, or literal `0.0`), so there is no sign arm to cover.
/// Scaling first and rounding the product double-rounds — `round(0.25, 1)`
/// must answer `0.2`, while `(0.25 * 10).round() / 10` answers `0.3` — so
/// the correctly-rounded one-place expansion is rounded instead, the same
/// technique as the Phase 3 `round4` helper (proven against CPython there).
pub fn round1(x: f64) -> f64 {
    debug_assert!(x >= 0.0, "round1 only ever sees averages");
    let rendered = format!("{x:.20}");
    let (int, frac) = rendered.split_once('.').unwrap_or((rendered.as_str(), ""));
    let mut digits: Vec<u8> = int.bytes().chain(frac.bytes().take(1)).collect();
    let mut int_len = int.len();
    // The kept digit is frac[0]; the verdict comes from frac[1] with the
    // tail behind it, exactly like the Phase 3 round4 helper at keep=4.
    let second = frac.as_bytes().get(1).copied().unwrap_or(b'0');
    let rest_zero = frac
        .as_bytes()
        .get(2..)
        .is_none_or(|rest| rest.iter().all(|b| *b == b'0'));
    let odd_kept = digits.last().is_some_and(|d| d % 2 == 1);
    if second > b'5' || (second == b'5' && (!rest_zero || odd_kept)) {
        let mut carry = true;
        for digit in digits.iter_mut().rev() {
            if !carry {
                break;
            }
            if *digit == b'9' {
                *digit = b'0';
            } else {
                *digit += 1;
                carry = false;
            }
        }
        if carry {
            // Carry out front (9.96 to 10.0) shifts the point right.
            digits.insert(0, b'1');
            int_len += 1;
        }
    }
    digits.truncate(int_len + 1);
    let head_len = int_len.min(digits.len());
    let text = format!(
        "{}.{}",
        String::from_utf8_lossy(&digits[..head_len]),
        String::from_utf8_lossy(&digits[head_len..]),
    );
    text.parse().unwrap_or(x)
}

/// Whole days between two instants, exactly `(later - earlier).days`.
///
/// Timestamps cross as integer microseconds so the floor is exact: flooring
/// truncated epoch seconds instead would flip pairs split across a day
/// boundary by sub-second parts (0.9 s → 86400.1 s is 0 days, not 1).
pub fn gap_days(earlier_micros: i64, later_micros: i64) -> i64 {
    const MICROS_PER_DAY: i64 = 86_400_000_000;
    (later_micros - earlier_micros).div_euclid(MICROS_PER_DAY)
}

/// One rhythm gap: a stretch longer than the correspondence's own pace.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Gap {
    /// ISO start/end of the stretch the gap spans.
    pub start_iso: String,
    pub end_iso: String,
    pub gap_days: i64,
    pub median_interval_days: i64,
    pub confidence: f64,
}

/// Stretches longer than this correspondence's own rhythm.
///
/// `dated` is `(micros since epoch, ISO string)` per letter; it is sorted
/// here, mirroring the Python `sorted(...)` on timestamps. The median is the
/// upper-middle of the sorted day gaps (`sorted(intervals)[len // 2][0]`);
/// sorting bare deltas agrees because equal deltas are interchangeable at the
/// middle index. Confidence is `min(0.8, (delta / threshold) * 0.5)` with the
/// operations in Python's order, so the bits agree.
pub fn find_gaps(dated: &[(i64, String)]) -> Vec<Gap> {
    let mut ordered: Vec<(i64, &str)> = dated.iter().map(|(m, iso)| (*m, iso.as_str())).collect();
    ordered.sort_by_key(|(m, _)| *m);
    if ordered.len() < 2 {
        return Vec::new();
    }
    let mut deltas: Vec<i64> = ordered
        .windows(2)
        .map(|w| gap_days(w[0].0, w[1].0))
        .collect();
    deltas.sort_unstable();
    let median = deltas[deltas.len() / 2];
    let threshold = (median * 2).max(MIN_GAP_DAYS);
    ordered
        .windows(2)
        .filter_map(|w| {
            let delta = gap_days(w[0].0, w[1].0);
            if delta > threshold {
                Some(Gap {
                    start_iso: w[0].1.to_owned(),
                    end_iso: w[1].1.to_owned(),
                    gap_days: delta,
                    median_interval_days: median,
                    confidence: (delta as f64 / threshold as f64 * 0.5).min(0.8),
                })
            } else {
                None
            }
        })
        .collect()
}

/// Candidates per detection method, first-seen order — the Python dict keeps
/// insertion order, so the JSON object does too.
pub fn count_by_method(methods: &[&str]) -> Vec<(String, usize)> {
    let mut counts: Vec<(String, usize)> = Vec::new();
    for method in methods {
        if let Some(slot) = counts.iter_mut().find(|(m, _)| m == method) {
            slot.1 += 1;
        } else {
            counts.push(((*method).to_owned(), 1));
        }
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn ts(y: i32, mo: u32, d: u32) -> DateTime<FixedOffset> {
        FixedOffset::east_opt(0)
            .unwrap()
            .with_ymd_and_hms(y, mo, d, 0, 0, 0)
            .unwrap()
    }

    fn letter(y: i32, mo: u32, d: u32, sender: &str) -> LetterEvent {
        LetterEvent {
            timestamp: Some(ts(y, mo, d)),
            sender_id: sender.to_owned(),
        }
    }

    // --- time_bin parsing -------------------------------------------------

    #[test]
    fn unknown_bins_fall_through_to_day() {
        assert_eq!(parse_time_bin("month"), TimeBin::Month);
        assert_eq!(parse_time_bin("week"), TimeBin::Week);
        assert_eq!(parse_time_bin("day"), TimeBin::Day);
        assert_eq!(parse_time_bin("year"), TimeBin::Day);
        assert_eq!(parse_time_bin(""), TimeBin::Day);
    }

    #[test]
    fn bucket_keys_match_strftime() {
        let t = ts(1862, 10, 25);
        assert_eq!(bucket_key(&t, TimeBin::Month), "1862-10");
        assert_eq!(bucket_key(&t, TimeBin::Day), "1862-10-25");
        assert_eq!(bucket_key(&t, TimeBin::Week), "1862-W42");
    }

    #[test]
    fn week_zero_before_the_first_monday() {
        // 1865-01-01 was a Sunday: Python %W says week 00.
        assert_eq!(bucket_key(&ts(1865, 1, 1), TimeBin::Week), "1865-W00");
        assert_eq!(bucket_key(&ts(1865, 1, 2), TimeBin::Week), "1865-W01");
    }

    // --- analyze ----------------------------------------------------------

    #[test]
    fn empty_events_report_zeros() {
        let report = analyze("a", "b", &[], TimeBin::Month);
        assert!(report.timeline.is_empty());
        assert!(report.anomalies.is_empty());
        assert_eq!(
            report.summary,
            CadenceSummary {
                total_letters: 0,
                a_to_b_count: 0,
                b_to_a_count: 0,
                time_span_bins: 0,
                average_per_bin: 0.0,
            }
        );
        // The empty average renders as integer 0, like `round(0, 1)`.
        assert_eq!(
            serde_json::to_value(&report.summary).unwrap()["average_per_bin"],
            serde_json::json!(0)
        );
    }

    #[test]
    fn directions_split_on_the_sender() {
        let events = vec![
            letter(1862, 10, 1, "a"),
            letter(1862, 10, 2, "b"),
            letter(1862, 10, 3, "stranger"),
        ];
        let report = analyze("a", "b", &events, TimeBin::Month);
        assert_eq!(report.timeline.len(), 1);
        assert_eq!(report.timeline[0].a_to_b, 1);
        // Anything not from A counts B-to-A, exactly like the Python else.
        assert_eq!(report.timeline[0].b_to_a, 2);
        assert_eq!(report.timeline[0].total, 3);
        assert_eq!(report.summary.total_letters, 3);
        // A non-zero average renders as a float, unlike the empty-report 0.
        assert_eq!(
            serde_json::to_value(&report.summary).unwrap()["average_per_bin"],
            serde_json::json!(3.0)
        );
    }

    #[test]
    fn silence_shape_omits_count() {
        // `analyze` cannot produce silences (every listed bin holds a
        // letter — see the removal proof above), but the Python dict shape
        // for one omits `count`, and the struct keeps that shape.
        let silence = Anomaly {
            bin: "1862-02".to_owned(),
            anomaly_type: "silence".to_owned(),
            expected: 3.4,
            count: None,
        };
        let value = serde_json::to_value(&silence).unwrap();
        assert_eq!(value["type"], "silence");
        assert!(value.get("count").is_none());
    }

    #[test]
    fn undated_events_count_but_do_not_bucket() {
        let events = vec![
            letter(1862, 10, 1, "a"),
            LetterEvent {
                timestamp: None,
                sender_id: "a".to_owned(),
            },
        ];
        let report = analyze("a", "b", &events, TimeBin::Month);
        assert_eq!(report.summary.total_letters, 2);
        assert_eq!(report.summary.a_to_b_count, 1);
        assert_eq!(report.summary.time_span_bins, 1);
    }

    #[test]
    fn burst_flags_over_three_times_average() {
        let mut events = Vec::new();
        for m in 1..5 {
            events.push(letter(1862, m, 1, "a"));
        }
        for _ in 0..13 {
            events.push(letter(1862, 5, 1, "a"));
        }
        let report = analyze("a", "b", &events, TimeBin::Month);
        assert_eq!(report.anomalies.len(), 1);
        assert_eq!(report.anomalies[0].bin, "1862-05");
        assert_eq!(report.anomalies[0].anomaly_type, "burst");
        assert_eq!(report.anomalies[0].count, Some(13));
        assert_eq!(report.anomalies[0].expected, 3.4);
        // Burst rows carry count; the envelope shape matches the Python dicts.
        let value = serde_json::to_value(&report.anomalies[0]).unwrap();
        assert_eq!(value["type"], "burst");
        assert_eq!(value["count"], 13);
    }
    #[test]
    fn round1_is_half_even_on_the_binary_value() {
        // Exact ties go to even: 0.25 and 0.75 are exact in binary.
        assert_eq!(round1(0.25), 0.2);
        assert_eq!(round1(0.75), 0.8);
        // Near ties follow the binary value, not the decimal spelling.
        assert_eq!(round1(2.05), 2.0);
        assert_eq!(round1(1.05), 1.1);
        assert_eq!(round1(3.4), 3.4);
        assert_eq!(round1(0.0), 0.0);
        // 9.95 sits below the tie in binary; 9.96 carries out front.
        assert_eq!(round1(9.95), 9.9);
        assert_eq!(round1(9.96), 10.0);
    }

    // --- gaps -------------------------------------------------------------

    fn micros(y: i32, mo: u32, d: u32) -> i64 {
        ts(y, mo, d).timestamp_micros()
    }

    fn dated(days: &[(i32, u32, u32)]) -> Vec<(i64, String)> {
        days.iter()
            .map(|(y, m, d)| (micros(*y, *m, *d), ts(*y, *m, *d).to_rfc3339()))
            .collect()
    }

    #[test]
    fn steady_rhythm_has_no_gaps() {
        let letters = dated(&[(1862, 1, 1), (1862, 1, 8), (1862, 1, 15), (1862, 1, 22)]);
        assert!(find_gaps(&letters).is_empty());
    }

    #[test]
    fn long_stretch_past_rhythm_is_a_gap() {
        let letters = dated(&[(1862, 1, 1), (1862, 1, 8), (1862, 1, 15), (1862, 4, 1)]);
        let gaps = find_gaps(&letters);
        assert_eq!(gaps.len(), 1);
        assert_eq!(gaps[0].gap_days, 76);
        assert_eq!(gaps[0].median_interval_days, 7);
        assert_eq!(gaps[0].confidence, 0.8);
        assert_eq!(gaps[0].start_iso, ts(1862, 1, 15).to_rfc3339());
        assert_eq!(gaps[0].end_iso, ts(1862, 4, 1).to_rfc3339());
    }

    #[test]
    fn two_week_floor_holds_for_daily_rhythm() {
        let letters = dated(&[(1862, 1, 1), (1862, 1, 2), (1862, 1, 3), (1862, 1, 20)]);
        let gaps = find_gaps(&letters);
        assert_eq!(gaps.len(), 1);
        assert_eq!(gaps[0].median_interval_days, 1);
        // Threshold is max(2, 14) = 14; 17 days past it.
        assert!((gaps[0].confidence - (17.0 / 14.0 * 0.5)).abs() < 1e-15);
    }

    #[test]
    fn fewer_than_two_letters_has_no_rhythm() {
        assert!(find_gaps(&[]).is_empty());
        assert!(find_gaps(&dated(&[(1862, 1, 1)])).is_empty());
    }

    #[test]
    fn gap_days_floors_like_timedelta() {
        let a = micros(1862, 1, 1);
        assert_eq!(gap_days(a, a + 86_400_000_000 - 1), 0);
        assert_eq!(gap_days(a, a + 86_400_000_000), 1);
    }

    // --- method counts ----------------------------------------------------

    #[test]
    fn counts_keep_first_seen_order() {
        assert_eq!(
            count_by_method(&["cadence", "referenced", "cadence"]),
            vec![("cadence".to_owned(), 2), ("referenced".to_owned(), 1),]
        );
        assert!(count_by_method(&[]).is_empty());
    }

    #[test]
    fn resolved_suffix_is_a_literal() {
        assert_eq!(RESOLVED_SUFFIX, "_resolved");
        assert_eq!(MIN_GAP_DAYS, 14);
    }
}
