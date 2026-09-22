//! Event service grouping: timeline buckets over stored events.
//!
//! Python source: `packages/core/src/research_engine/services/events/service.py`.
//!
//! Only the pure bucketing rules live here. Inserts, filtered queries, and
//! actor writes stay behind the repository ports (framework/DB-bound work,
//! out of scope for this phase).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// The two fields grouping reads: a missing timestamp cannot bucket, and the
/// type feeds both the `event_type` grouping and the per-bucket aggregates.
#[derive(Debug, Clone, PartialEq)]
pub struct TimedEvent {
    pub timestamp_start: Option<DateTime<Utc>>,
    pub event_type: String,
}

/// One bucket of a timeline aggregation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimelineBucket {
    pub bucket: String,
    pub count: usize,
    pub aggregates: BucketAggregates,
}

/// Per-bucket aggregates. `event_types` is the deduplicated set of the
/// bucket's event types, sorted — Python builds it as `list(set(...))`,
/// whose order varies run to run under hash randomization, so the Rust side
/// pins the sorted order instead (same precedent as Phase 2's
/// `weighted_fuse` tie-ordering: scores/counts are the contract, set order
/// is not).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BucketAggregates {
    pub event_types: Vec<String>,
}

/// Bucket key for one event's start timestamp.
///
/// `None` mirrors a missing `timestamp_start`: the event buckets nowhere and
/// [`group_events`] skips it. An unknown `group_by` falls through to the
/// monthly key — Python's trailing `return ts.strftime("%Y-%m")` catches
/// everything, including an unset grouping.
pub fn bucket_key(
    timestamp_start: Option<DateTime<Utc>>,
    group_by: &str,
    event_type: &str,
) -> Option<String> {
    let ts = timestamp_start?;
    let key = match group_by {
        "day" => ts.format("%Y-%m-%d").to_string(),
        // `%W` is Monday-first, 00-53, in both Python's `strftime` and
        // chrono's: days before the year's first Monday are week 00. The
        // Sunday-edge test below pins it (2023-01-01, a Sunday, is W00).
        "week" => ts.format("%Y-W%W").to_string(),
        "month" => ts.format("%Y-%m").to_string(),
        "year" => ts.format("%Y").to_string(),
        "event_type" => event_type.to_owned(),
        _ => ts.format("%Y-%m").to_string(),
    };
    Some(key)
}

/// Group events into sorted timeline buckets.
///
/// Keys sort ascending; events without a timestamp are skipped. Each bucket
/// counts its events and aggregates their deduplicated, sorted event types.
pub fn group_events(events: &[TimedEvent], group_by: &str) -> Vec<TimelineBucket> {
    let mut groups: BTreeMap<String, (usize, Vec<String>)> = BTreeMap::new();
    for event in events {
        let Some(key) = bucket_key(event.timestamp_start, group_by, &event.event_type) else {
            continue;
        };
        let entry = groups.entry(key).or_insert_with(|| (0, Vec::new()));
        entry.0 += 1;
        entry.1.push(event.event_type.clone());
    }
    groups
        .into_iter()
        .map(|(bucket, (count, mut event_types))| {
            event_types.sort();
            event_types.dedup();
            TimelineBucket {
                bucket,
                count,
                aggregates: BucketAggregates { event_types },
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn utc(datetime: &str) -> DateTime<Utc> {
        datetime.parse().expect("test timestamps parse")
    }

    fn event(datetime: &str, event_type: &str) -> TimedEvent {
        TimedEvent {
            timestamp_start: Some(utc(datetime)),
            event_type: event_type.to_owned(),
        }
    }

    #[test]
    fn bucket_key_day() {
        assert_eq!(
            bucket_key(Some(utc("1863-07-03T12:00:00Z")), "day", "battle"),
            Some("1863-07-03".to_owned())
        );
    }

    #[test]
    fn bucket_key_week_matches_python_monday_first() {
        // Monday opens a new week; the surrounding Sunday still belongs to
        // the old one.
        assert_eq!(
            bucket_key(Some(utc("2023-01-02T00:00:00Z")), "week", "x"),
            Some("2023-W01".to_owned())
        );
        assert_eq!(
            bucket_key(Some(utc("2023-01-08T23:59:59Z")), "week", "x"),
            Some("2023-W01".to_owned())
        );
        assert_eq!(
            bucket_key(Some(utc("2023-01-09T00:00:00Z")), "week", "x"),
            Some("2023-W02".to_owned())
        );
    }

    #[test]
    fn bucket_key_week_sunday_edge_is_week_zero() {
        // 2023-01-01 is a Sunday before the year's first Monday: Python's
        // `%W` (like chrono's) numbers it week 00, not week 01.
        assert_eq!(
            bucket_key(Some(utc("2023-01-01T00:00:00Z")), "week", "x"),
            Some("2023-W00".to_owned())
        );
    }

    #[test]
    fn bucket_key_month_year_event_type() {
        let ts = Some(utc("1863-07-03T12:00:00Z"));
        assert_eq!(
            bucket_key(ts, "month", "battle"),
            Some("1863-07".to_owned())
        );
        assert_eq!(bucket_key(ts, "year", "battle"), Some("1863".to_owned()));
        assert_eq!(
            bucket_key(ts, "event_type", "battle"),
            Some("battle".to_owned())
        );
    }

    #[test]
    fn bucket_key_unknown_grouping_falls_back_to_month() {
        let ts = Some(utc("1863-07-03T12:00:00Z"));
        assert_eq!(
            bucket_key(ts, "fortnight", "battle"),
            Some("1863-07".to_owned())
        );
    }

    #[test]
    fn bucket_key_missing_timestamp_is_none() {
        assert_eq!(bucket_key(None, "month", "battle"), None);
    }

    #[test]
    fn group_events_buckets_and_sorts_unsorted_input() {
        let events = vec![
            event("1863-07-03T12:00:00Z", "battle"),
            event("1861-04-12T04:30:00Z", "bombardment"),
            event("1863-07-01T08:00:00Z", "battle"),
        ];
        let buckets = group_events(&events, "month");
        let keys: Vec<&str> = buckets.iter().map(|b| b.bucket.as_str()).collect();
        assert_eq!(keys, ["1861-04", "1863-07"]);
        assert_eq!(buckets[1].count, 2);
    }

    #[test]
    fn group_events_skips_missing_timestamps() {
        let events = vec![
            event("1863-07-03T12:00:00Z", "battle"),
            TimedEvent {
                timestamp_start: None,
                event_type: "undated".to_owned(),
            },
        ];
        let buckets = group_events(&events, "month");
        assert_eq!(buckets.len(), 1);
        assert_eq!(buckets[0].count, 1);
    }

    #[test]
    fn group_events_aggregates_deduped_sorted_event_types() {
        let events = vec![
            event("1863-07-01T08:00:00Z", "skirmish"),
            event("1863-07-03T12:00:00Z", "battle"),
            event("1863-07-04T12:00:00Z", "battle"),
        ];
        let buckets = group_events(&events, "month");
        assert_eq!(buckets.len(), 1);
        assert_eq!(buckets[0].count, 3);
        assert_eq!(buckets[0].aggregates.event_types, ["battle", "skirmish"]);
    }
}
