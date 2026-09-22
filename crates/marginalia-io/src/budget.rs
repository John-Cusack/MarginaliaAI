//! Sync budget-guard core ported from `adapters/llm/budget_guard.py`.
//!
//! The Python `BudgetGuard` wraps an `LLMPort` and refuses `complete` /
//! `structured` calls once spend in the rolling window reaches `limit_usd`.
//! Only the synchronous decision core ports here: the cached spend state, the
//! staleness check, the refuse-at-limit rule, and the exact refusal message.
//!
//! Why the async wrapper stays Python: `BudgetGuard.complete`,
//! `BudgetGuard.structured`, and `BudgetGuard.spent` are all `async` — they
//! await the inner port, `LLMCallLogRepo.total_cost_since`, and the clock
//! behind `asyncio` with a shared refresh cache. Porting that shell needs
//! async port traits plus `tokio` wiring, which is Phase 6/7 seam work with no
//! behavioral win; the sync core below is pure, total, and trivially
//! property-testable, so it ports first with zero risk.
//!
//! `__getattr__` passthrough is deliberately non-portable: Rust has no
//! attribute fallback, and a guard that silently forwards unknown methods
//! would hide unguarded LLM call paths. Callers use the inner adapter directly
//! for anything outside `complete` / `structured` (static dispatch replaces
//! the passthrough), so every LLM entry point is visibly guarded or visibly
//! not.

use crate::errors::Error;
use crate::errors::Result;

/// Cached spend state: the sync half of `BudgetGuard`.
#[derive(Debug, Clone)]
pub struct BudgetState {
    /// Refuse calls once window spend reaches this many USD.
    pub limit_usd: f64,
    /// Rolling window length in days.
    pub window_days: i64,
    /// Seconds a cached spend total stays valid.
    pub recheck_seconds: f64,
    /// Last queried spend in the window.
    pub spent: f64,
    /// Monotonic time of the last spend query; `NEG_INFINITY` until the first.
    pub checked_at: f64,
}

impl BudgetState {
    /// Fresh state: nothing spent, cache cold (`checked_at = NEG_INFINITY`
    /// forces the first `should_refresh` to true, like the Python
    /// `float("-inf")` initializer).
    pub fn new(limit_usd: f64, window_days: i64, recheck_seconds: f64) -> Self {
        Self {
            limit_usd,
            window_days,
            recheck_seconds,
            spent: 0.0,
            checked_at: f64::NEG_INFINITY,
        }
    }

    /// Mirror of the `spent()` staleness check:
    /// `now - self._checked_at >= self._recheck_seconds`.
    pub fn should_refresh(&self, now: f64) -> bool {
        now - self.checked_at >= self.recheck_seconds
    }

    /// Store a freshly queried total. Callers query the repo only when
    /// [`should_refresh`](Self::should_refresh) is true, so a batch
    /// extraction pays one aggregate per recheck interval, not one per call.
    pub fn refresh(&mut self, spent: f64, now: f64) {
        self.spent = spent;
        self.checked_at = now;
    }

    /// Mirror of `_enforce`: refuse at *or above* the limit (`>=`), before
    /// delegating to the inner adapter.
    pub fn enforce(&self, spent: f64) -> Result<()> {
        if spent >= self.limit_usd {
            return Err(Error::BudgetExceeded {
                spent,
                limit: self.limit_usd,
                window_days: self.window_days,
            });
        }
        Ok(())
    }
}

/// Exact refusal message from `domain/provenance.py::BudgetExceeded`.
/// Single formatting site: [`Error::BudgetExceeded`]'s `Display` delegates
/// here so the string cannot drift between construction sites.
pub fn budget_exceeded_message(spent: f64, limit: f64, window_days: i64) -> String {
    format!(
        "LLM budget exceeded: ${:.2} spent in the last {}d against a ${:.2} limit. Raise RE_LLM_BUDGET_USD or wait for the window to roll over.",
        spent, window_days, limit
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Sync stand-in for the inner `LLMPort`.
    struct FakeLlm {
        completions: Vec<String>,
        structured_calls: Vec<String>,
    }

    impl FakeLlm {
        fn complete(&mut self, purpose: &str) -> &str {
            self.completions.push(purpose.to_owned());
            "ok"
        }

        fn structured(&mut self, purpose: &str) {
            self.structured_calls.push(purpose.to_owned());
        }
    }

    /// Sync mirror of `BudgetGuard`: refresh-if-stale, then
    /// enforce-before-delegate. The async Python awaits the repo here; the
    /// ordering under test — refresh, enforce, delegate — is identical.
    struct Guard {
        state: BudgetState,
        inner: FakeLlm,
        cost: f64,
        queries: usize,
    }

    impl Guard {
        fn new(cost: f64, limit: f64, recheck: f64) -> Self {
            Self {
                state: BudgetState::new(limit, 30, recheck),
                inner: FakeLlm {
                    completions: Vec::new(),
                    structured_calls: Vec::new(),
                },
                cost,
                queries: 0,
            }
        }

        fn spend(&mut self, now: f64) -> f64 {
            if self.state.should_refresh(now) {
                self.queries += 1;
                self.state.refresh(self.cost, now);
            }
            self.state.spent
        }

        fn complete(&mut self, now: f64) -> Result<String> {
            let spent = self.spend(now);
            self.state.enforce(spent)?;
            Ok(self.inner.complete("general").to_owned())
        }

        fn structured(&mut self, now: f64) -> Result<()> {
            let spent = self.spend(now);
            self.state.enforce(spent)?;
            self.inner.structured("extraction");
            Ok(())
        }
    }

    #[test]
    fn under_budget_passes_through() {
        let mut guard = Guard::new(3.0, 10.0, 30.0);
        assert_eq!(guard.complete(0.0).unwrap(), "ok");
        assert_eq!(guard.inner.completions, ["general"]);
    }

    /// Refusal-variant assertion without branches: `assert!(matches!(…))`
    /// leaves its false arm uncovered under the per-region bar, while a
    /// discriminant comparison pins the exact contract callers match on.
    fn assert_refused(err: &Error) {
        assert_eq!(
            std::mem::discriminant(err),
            std::mem::discriminant(&Error::BudgetExceeded {
                spent: 0.0,
                limit: 0.0,
                window_days: 0
            })
        );
    }

    #[test]
    fn structured_is_guarded_before_delegate() {
        // Extraction is the expensive path; guarding only `complete` would
        // miss it. Over budget, the inner adapter sees nothing.
        let mut guard = Guard::new(10.5, 10.0, 30.0);
        let err = guard.structured(0.0).unwrap_err();
        assert_refused(&err);
        assert!(guard.inner.structured_calls.is_empty());

        let mut guard = Guard::new(3.0, 10.0, 30.0);
        guard.structured(0.0).unwrap();
        assert_eq!(guard.inner.structured_calls, ["extraction"]);
    }

    #[test]
    fn over_budget_refuses_without_calling_provider() {
        let mut guard = Guard::new(10.5, 10.0, 30.0);
        let err = guard.complete(0.0).unwrap_err();
        // `enforce` builds only `Error::BudgetExceeded`, so a catch-all arm
        // matching it away would be dead code that coverage must still fire.
        // Assert the shape, then pin the refusal text verbatim (this also
        // covers `Error::BudgetExceeded`'s `Display`).
        assert_refused(&err);
        assert_eq!(
            err.to_string(),
            "LLM budget exceeded: $10.50 spent in the last 30d against a $10.00 limit. Raise RE_LLM_BUDGET_USD or wait for the window to roll over."
        );
        assert!(guard.inner.completions.is_empty());
    }

    #[test]
    fn exactly_at_limit_refuses() {
        let mut guard = Guard::new(10.0, 10.0, 30.0);
        assert_refused(&guard.complete(0.0).unwrap_err());
        assert!(guard.inner.completions.is_empty());
    }

    #[test]
    fn spend_is_not_requeried_per_call() {
        // A batch extraction issues thousands of calls; one aggregate per
        // call would cost more than the guard saves.
        let mut guard = Guard::new(1.0, 10.0, 30.0);
        for i in 0..50 {
            guard.complete(f64::from(i) * 0.01).unwrap();
        }
        assert_eq!(guard.queries, 1);
        assert_eq!(guard.inner.completions.len(), 50);
    }

    #[test]
    fn zero_recheck_refreshes_every_call() {
        let mut guard = Guard::new(1.0, 10.0, 0.0);
        guard.complete(0.0).unwrap();
        guard.cost = 99.0;
        assert_refused(&guard.complete(0.0).unwrap_err());
        assert_eq!(guard.inner.completions.len(), 1);
    }

    #[test]
    fn message_names_the_setting_to_change() {
        let msg = budget_exceeded_message(12.0, 10.0, 30);
        assert!(
            msg.contains("RE_LLM_BUDGET_USD"),
            "message must name the setting: {msg}"
        );
        assert_eq!(
            msg,
            "LLM budget exceeded: $12.00 spent in the last 30d against a \
             $10.00 limit. Raise RE_LLM_BUDGET_USD or wait for the window to roll over."
        );
    }
}
