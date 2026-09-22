//! `LemmaLookup::find` execution over Postgres (`sqlx`).
//!
//! Python source: `services/words/lookup.py` (execution of `find`).
//! Statement shapes, clause order, and note texts come from the pure
//! builders in [`crate::words`], which this module executes but never
//! redefines: [`occurrences_query`](crate::words::occurrences_query),
//! [`aggregate_queries`](crate::words::aggregate_queries),
//! [`known_books_query`](crate::words::known_books_query),
//! [`verse_map_is_loaded_query`](crate::words::verse_map_is_loaded_query),
//! [`fold_occurrences`](crate::words::fold_occurrences), and the `*_note`
//! constructors. The flow is identical, minus the leading `COUNT`: totals
//! and books derive from the aggregates (sum of surface counts; number of
//! book groups), then the map-loaded check → occurrence cap refusal
//! *before* enumerating → occurrences → partial/qere notes.

use super::db_err;
use crate::words::{
    aggregate_queries, fold_occurrences, known_books_query, map_empty_note, occurrences_query,
    over_limit_note, partials_note, qere_note, verse_map_is_loaded_query, AggregateCounts,
    AggregateKey, BookCount, BuiltQuery, HomographCount, LemmaQuery, LemmaResult, Mapping,
    MorphCount, PrefixCount, SurfaceCount, WhereParam, WordRow,
};
use marginalia_types::errors::Result;
use sqlx::{
    postgres::{PgRow, Postgres},
    FromRow, PgPool,
};

/// Reads `core.words`, `core.verse_map`, and `core.edition_books`.
pub struct PgLemmaLookup {
    pool: PgPool,
}

impl PgLemmaLookup {
    /// Serve lookups from an existing pool.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Whether there is a Hebrew-to-English map to consult at all.
    ///
    /// Coarse on purpose: it separates "migrated but never loaded" — the
    /// failure that actually happens, and the one that used to be silent —
    /// from a working map.
    pub async fn verse_map_is_loaded(&self) -> Result<bool> {
        let built = verse_map_is_loaded_query();
        // Single-column existence probe through the shared row binder (the
        // tuple is the `FromRow` shape; a scalar-only binder would duplicate
        // the `Text`/`Int` match with an unfireable `Int` arm).
        let found: Option<(Option<i32>,)> = bind_built_as(&built)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(found.is_some())
    }

    /// The OSIS book ids `core.words` actually holds, in canonical order.
    pub async fn known_books(&self, language: &str) -> Result<Vec<String>> {
        let built = known_books_query(language);
        let rows: Vec<(String, i32)> = bind_built_as(&built)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(rows.into_iter().map(|(osis_id, _)| osis_id).collect())
    }

    /// Find every occurrence of a lemma, reported as citable references.
    ///
    /// Returns verse references and never character spans (see the
    /// [`crate::words`] docs for why a span would misaddress the quoting
    /// edition).
    pub async fn find(&self, query: &LemmaQuery) -> Result<LemmaResult> {
        // No separate `COUNT` query: the surface group doubles as the
        // existence probe (empty ⟺ zero matches), and `total`/`books`
        // derive from the aggregates exactly — the total is the sum of the
        // surface counts, the book count is the number of book groups.
        // Besides one fewer round trip, a leading `COUNT` would guard the
        // table first and leave the group fetch with no deterministic
        // fault (both read the same rows).
        let counts = self.aggregates(query).await?;
        let total: i64 = counts.by_surface.iter().map(|group| group.count).sum();
        let books = counts.by_book.len() as i64;
        if total == 0 {
            // The hint: which number in which lexicon missed, and how to
            // widen. (`LemmaResult::zero` builds exactly this shape; the
            // computed counts are empty too, so nothing is lost.)
            return Ok(LemmaResult::zero(query));
        }
        let mut result = LemmaResult {
            query: query.echo(),
            total,
            books,
            occurrences: Vec::new(),
            counts,
            notes: Vec::new(),
        };

        let map_loaded = self.verse_map_is_loaded().await?;
        if !map_loaded {
            // First in the list: every English reference below is withheld
            // because of this, and a caller that reads one note reads this.
            result.notes.push(map_empty_note().to_string());
        }

        if !query.include_occurrences {
            return Ok(result);
        }
        // Refuse rather than truncate past the cap — before enumerating, so
        // a corpus-dump request never pays for its rows.
        if result.total > crate::words::MAX_OCCURRENCES as i64 {
            result.notes.push(over_limit_note(result.total));
            return Ok(result);
        }

        result.occurrences = self.occurrences(query, map_loaded).await?;

        let mut trailing: Vec<String> = Vec::new();
        let partials = result
            .occurrences
            .iter()
            .filter(|occurrence| occurrence.english.mapping == Mapping::Partial)
            .count();
        if partials > 0 {
            trailing.push(partials_note(partials));
        }
        // `from_qere` is the one flag that predicts a *form* the target
        // edition may not print: WLC reads the qere where LHB prints the
        // ketiv, so the reference is right but the surface may differ.
        let qere_refs: Vec<String> = result
            .occurrences
            .iter()
            .filter(|occurrence| occurrence.from_qere)
            .map(|occurrence| occurrence.r#ref.clone())
            .collect();
        if !qere_refs.is_empty() {
            trailing.push(qere_note(&qere_refs));
        }
        result.notes.extend(trailing);
        Ok(result)
    }

    /// The counts a lexicographic survey actually reports, in Python dict
    /// order: surface, book, morph, prefixes, homograph.
    async fn aggregates(&self, query: &LemmaQuery) -> Result<AggregateCounts> {
        let mut counts = AggregateCounts::default();
        for aggregate in aggregate_queries(query) {
            if aggregate.key == AggregateKey::ByBook {
                // Ordered canonically by edition ordinal, not by count —
                // hence the extra `ordinal` column the other groups lack.
                let rows: Vec<(String, i64, i32)> =
                    bind_aggregate_book(&aggregate.sql, &aggregate.params)
                        .fetch_all(&self.pool)
                        .await
                        .map_err(db_err)?;
                counts.by_book = rows
                    .into_iter()
                    .map(|(book, count, _)| BookCount { book, count })
                    .collect();
                continue;
            }
            // The plain-group fetch, inline so the loop holds the one
            // fallible boundary for all four groups (a shared `group()`
            // helper would carry its own `?` needing a second fault for
            // the same fact).
            let rows: Vec<(String, i64)> = bind_aggregate(&aggregate.sql, &aggregate.params)
                .fetch_all(&self.pool)
                .await
                .map_err(db_err)?;
            if aggregate.key == AggregateKey::BySurface {
                counts.by_surface = rows
                    .into_iter()
                    .map(|(surface, count)| SurfaceCount { surface, count })
                    .collect();
            } else if aggregate.key == AggregateKey::ByMorph {
                counts.by_morph = rows
                    .into_iter()
                    .map(|(morph, count)| MorphCount { morph, count })
                    .collect();
            } else if aggregate.key == AggregateKey::ByPrefixes {
                counts.by_prefixes = rows
                    .into_iter()
                    .map(|(prefixes, count)| PrefixCount { prefixes, count })
                    .collect();
            } else {
                // ByBook is handled above; only ByHomograph reaches here.
                counts.by_homograph = rows
                    .into_iter()
                    .map(|(homograph, count)| HomographCount { homograph, count })
                    .collect();
            }
        }
        Ok(counts)
    }

    /// One row per word, ordered canonically, carrying no span at all —
    /// folded (partial halves joined onto their occurrence) on the way out.
    async fn occurrences(
        &self,
        query: &LemmaQuery,
        map_loaded: bool,
    ) -> Result<Vec<crate::words::Occurrence>> {
        let built = occurrences_query(query);
        let rows: Vec<OccurrenceRow> = bind_built_as(&built)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        let mut words: Vec<WordRow> = Vec::with_capacity(rows.len());
        for row in rows {
            words.push(row.into_word_row());
        }
        Ok(fold_occurrences(words, map_loaded))
    }
}

/// One row of the occurrences `SELECT`, exactly the columns
/// [`occurrences_query`](crate::words::occurrences_query) selects.
///
/// `chapter`/`verse`/`ordinal` are `integer` on the wire; `mapping_type` is
/// `NOT NULL` on a joined `verse_map` row (constrained to
/// `'full'`/`'partial'`) and `NULL` when the traditions agree and no row
/// joined. `surface`/`morph` are `NOT NULL`; `prefixes`/`homograph` ride
/// through nullable (the aggregates `COALESCE` them, the rows do not).
#[derive(Debug, FromRow)]
struct OccurrenceRow {
    #[sqlx(rename = "ref")]
    ref_: String,
    book: String,
    chapter: i32,
    verse: i32,
    surface: String,
    lemma: String,
    morph: String,
    prefixes: Option<String>,
    homograph: Option<String>,
    from_qere: bool,
    to_ref: Option<String>,
    to_part: Option<String>,
    from_part: Option<String>,
    mapping_type: Option<String>,
    /// Canonical book order; consumed by `ORDER BY`, decoded only so the
    /// column list stays statement-identical to the Python `SELECT`.
    #[sqlx(rename = "ordinal")]
    _ordinal: i32,
}

impl OccurrenceRow {
    fn into_word_row(self) -> WordRow {
        // Total over the reachable domain: `mapping_type` is `NULL` when no
        // `verse_map` row joined, and the `verse_map_type_known` CHECK admits
        // only `'full'`/`'partial'` otherwise — `from_mapping_type` cannot
        // fail on either, so there is no error arm to cover. If the CHECK
        // ever widens, this `expect` names the place to re-examine.
        let mapping = self.mapping_type.as_deref().map(|mapping_type| {
            Mapping::from_mapping_type(mapping_type)
                .expect("verse_map_type_known admits only full/partial")
        });
        WordRow {
            r#ref: self.ref_,
            book: self.book,
            chapter: self.chapter,
            verse: self.verse,
            surface: self.surface,
            lemma: self.lemma,
            morph: self.morph,
            prefixes: self.prefixes,
            homograph: self.homograph,
            from_qere: self.from_qere,
            to_ref: self.to_ref,
            to_part: self.to_part,
            from_part: self.from_part,
            mapping,
        }
    }
}

/// Bind a [`BuiltQuery`] decoded as rows of `T` — thin over
/// [`bind_where_as`], so the parameter match lives in exactly one place.
fn bind_built_as<'a, T>(
    built: &'a BuiltQuery,
) -> sqlx::query::QueryAs<'a, Postgres, T, sqlx::postgres::PgArguments>
where
    T: for<'r> sqlx::FromRow<'r, PgRow>,
{
    bind_where_as(&built.sql, &built.params)
}

/// Encode shared `WHERE` params once, concretely.
///
/// A generic binder monomorphizes its `Text`/`Int` match per row type, and
/// each copy needs its own fault for the same arms. One concrete encoder
/// means one fault: a chapter-filtered find exercises `Int`, everything
/// exercises `Text`. Encoding `Text`/`Int` cannot fail; the `expect`
/// documents that instead of adding an arm.
fn encode_where_params(params: &[(String, WhereParam)]) -> sqlx::postgres::PgArguments {
    use sqlx::Arguments as _;
    let mut args = sqlx::postgres::PgArguments::default();
    for (_, param) in params {
        match param {
            WhereParam::Text(text) => args.add(text.as_str()),
            WhereParam::Int(value) => args.add(*value),
        }
        .expect("Text/Int binds cannot fail argument encoding");
    }
    args
}

/// Bind an aggregate statement's shared `WHERE` params.
fn bind_aggregate<'a>(
    sql: &'a str,
    params: &'a [(String, WhereParam)],
) -> sqlx::query::QueryAs<'a, Postgres, (String, i64), sqlx::postgres::PgArguments> {
    bind_where_as(sql, params)
}

/// Bind a by-book aggregate statement (which also selects the edition
/// ordinal) and its shared `WHERE` params.
fn bind_aggregate_book<'a>(
    sql: &'a str,
    params: &'a [(String, WhereParam)],
) -> sqlx::query::QueryAs<'a, Postgres, (String, i64, i32), sqlx::postgres::PgArguments> {
    bind_where_as(sql, params)
}

/// Positional binder for decoded queries: thin over the concrete encoder,
/// so no per-row-type copy of the match exists to cover.
fn bind_where_as<'a, T>(
    sql: &'a str,
    params: &'a [(String, WhereParam)],
) -> sqlx::query::QueryAs<'a, Postgres, T, sqlx::postgres::PgArguments>
where
    T: for<'r> sqlx::FromRow<'r, PgRow>,
{
    sqlx::query_as_with(sql, encode_where_params(params))
}
#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors the `LemmaLookup` surface used by callers (`find` plus the two
    // helpers the execution calls out to).
    #[test]
    fn lookup_exposes_expected_methods() {
        let _ = PgLemmaLookup::find;
        let _ = PgLemmaLookup::known_books;
        let _ = PgLemmaLookup::verse_map_is_loaded;
        let _ = PgLemmaLookup::new;
    }

    #[test]
    fn occurrence_row_decodes_mapping_type() {
        // `mapping_type` is `NOT NULL` on a joined row: a mapped verse always
        // carries its outcome, and an unknown value is a storage invariant
        // violation rather than a fourth outcome.
        let row = OccurrenceRow {
            ref_: "Ps.36.6".to_string(),
            book: "Ps".to_string(),
            chapter: 36,
            verse: 6,
            surface: "s".to_string(),
            lemma: "l".to_string(),
            morph: "m".to_string(),
            prefixes: None,
            homograph: None,
            from_qere: false,
            to_ref: Some("Ps.36.5".to_string()),
            to_part: None,
            from_part: None,
            mapping_type: Some("full".to_string()),
            _ordinal: 19,
        };
        let word = row.into_word_row();
        assert_eq!(word.mapping, Some(Mapping::Full));
    }

    // A `mapping_type` outside the `verse_map_type_known` CHECK cannot arrive
    // through the database (Postgres refuses the write), so the violation
    // panics with the invariant message instead of returning a mappable
    // error — there is no `Err` arm for coverage to waive.
    #[test]
    #[should_panic(expected = "verse_map_type_known admits only full/partial")]
    fn occurrence_row_rejects_unknown_mapping_type() {
        let row = OccurrenceRow {
            ref_: "Gen.1.1".to_string(),
            book: "Gen".to_string(),
            chapter: 1,
            verse: 1,
            surface: "s".to_string(),
            lemma: "l".to_string(),
            morph: "m".to_string(),
            prefixes: None,
            homograph: None,
            from_qere: false,
            to_ref: Some("Gen.1.1".to_string()),
            to_part: None,
            from_part: None,
            mapping_type: Some("sideways".to_string()),
            _ordinal: 1,
        };
        let _ = row.into_word_row();
    }

    #[test]
    fn unmapped_row_carries_no_mapping() {
        // No joined row (the traditions agree): `mapping_type` is NULL and
        // the fold reports `same` when the map is loaded, `unmapped` when it
        // is not — decided downstream, not here.
        let row = OccurrenceRow {
            ref_: "Gen.18.19".to_string(),
            book: "Gen".to_string(),
            chapter: 18,
            verse: 19,
            surface: "s".to_string(),
            lemma: "l".to_string(),
            morph: "m".to_string(),
            prefixes: None,
            homograph: None,
            from_qere: false,
            to_ref: None,
            to_part: None,
            from_part: None,
            mapping_type: None,
            _ordinal: 1,
        };
        let word = row.into_word_row();
        assert_eq!(word.mapping, None);
    }
}
