//! Entity service rules: create-or-update by match score.
//!
//! Python source: `packages/core/src/research_engine/services/entities/service.py`.
//!
//! Only the pure decision rules live here. Tiered resolution (`resolve`),
//! storage reads/writes, and mention queries stay behind the repository ports
//! (framework/DB-bound work, out of scope for this phase).

/// What `upsert` decided from the top resolution candidate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpsertDecision {
    /// A candidate scored above the update threshold: update it in place.
    Update,
    /// No candidate scored above the threshold (or none resolved at all):
    /// insert a new entity.
    Create,
}

/// Create-or-update from the top candidate's match score, if any.
///
/// Python tries an exact match first and updates only when
/// `candidates[0].match_score > 0.95` — strictly greater, so a score of
/// exactly 0.95 still creates. `None` (no candidates) creates.
pub fn upsert_decision(top_match_score: Option<f64>) -> UpsertDecision {
    match top_match_score {
        Some(score) if score > 0.95 => UpsertDecision::Update,
        _ => UpsertDecision::Create,
    }
}

/// Pair an entity with its aliases, mirroring `get_with_aliases`.
///
/// A missing entity carries no aliases: the Python `None` arm returns
/// `(None, [])`, dropping whatever alias list the caller may have fetched.
/// (The upsert path additionally inserts aliases with per-alias errors
/// suppressed — `contextlib.suppress(Exception)` — so a duplicate alias
/// never fails the upsert itself. There is no alias error to model here;
/// that suppression is a property of the storage call, kept at the call
/// site when the repository seam lands.)
pub fn with_aliases<Entity, Alias>(
    entity: Option<Entity>,
    aliases: Vec<Alias>,
) -> (Option<Entity>, Vec<Alias>) {
    match entity {
        None => (None, Vec::new()),
        Some(entity) => (Some(entity), aliases),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upsert_decision_no_candidate_creates() {
        assert_eq!(upsert_decision(None), UpsertDecision::Create);
    }

    #[test]
    fn upsert_decision_above_threshold_updates() {
        assert_eq!(upsert_decision(Some(0.950_000_1)), UpsertDecision::Update);
        assert_eq!(upsert_decision(Some(1.0)), UpsertDecision::Update);
    }

    #[test]
    fn upsert_decision_boundary_score_creates() {
        // Strictly-greater-than: 0.95 itself does not update.
        assert_eq!(upsert_decision(Some(0.95)), UpsertDecision::Create);
        assert_eq!(upsert_decision(Some(0.0)), UpsertDecision::Create);
        assert_eq!(upsert_decision(Some(-1.0)), UpsertDecision::Create);
    }

    #[test]
    fn with_aliases_none_drops_aliases() {
        let (entity, aliases): (Option<String>, Vec<String>) =
            with_aliases(None, vec!["alias".to_owned()]);
        assert_eq!(entity, None);
        assert!(aliases.is_empty());
    }

    #[test]
    fn with_aliases_some_keeps_aliases() {
        let (entity, aliases) = with_aliases(
            Some("entity".to_owned()),
            vec!["a".to_owned(), "b".to_owned()],
        );
        assert_eq!(entity, Some("entity".to_owned()));
        assert_eq!(aliases, ["a".to_owned(), "b".to_owned()]);
    }
}
