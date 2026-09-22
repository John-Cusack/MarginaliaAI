//! `DocumentNodeRepo` over Postgres (`sqlx`).
//!
//! Python source:
//! `adapters/storage/postgres/repositories/nodes.py`. Table:
//! `core.document_nodes` (`id`, `document_id`, `parent_id`, `path` ltree,
//! `depth`, `position`, `node_type`, `title`, `char_start`, `char_end`,
//! `metadata`, `created_at`). There is no port trait for nodes; the method
//! shapes below are 1:1 with `PGDocumentNodeRepo`.
//!
//! `path` values bind as text with an explicit `::ltree` cast (`<@`, `@>`,
//! `nlevel`, `ORDER BY path` read the column natively), and come back via
//! `path::text` so decoding never depends on the driver's `ltree` support.

use std::collections::HashMap;

use marginalia_types::errors::{Error, Result};
use marginalia_types::nodes::{DocumentNode, DocumentNodeDraft};
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use super::{db_err, PgTx};

/// The repository. Holds a [`PgPool`]; tree writes take a [`PgTx`] opened
/// via [`PgTx::begin`].
pub struct PgDocumentNodeRepo {
    pool: PgPool,
}

impl PgDocumentNodeRepo {
    /// Serve the repository from an existing pool.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Insert a whole tree, resolving `parent_path` to `parent_id`.
    ///
    /// Drafts must arrive with every parent ahead of its children — which is
    /// what `build_node_tree` returns — so each row's parent id is already
    /// known by the time it is written. Rows come back in insertion order.
    pub async fn insert_many(
        &self,
        tx: &mut PgTx,
        document_id: Uuid,
        drafts: &[DocumentNodeDraft],
    ) -> Result<Vec<DocumentNode>> {
        // Pure parent resolution first (fail before writing anything), then
        // one `INSERT ... RETURNING` per row in draft order.
        let resolved = resolve_parent_ids(drafts)?;
        let mut nodes = Vec::with_capacity(drafts.len());
        for (draft, (node_id, parent_id)) in drafts.iter().zip(resolved.iter()) {
            // `uuid4()` in Python; same version/variant scheme here. `path`
            // binds as text with an explicit `::ltree` cast, mirroring the
            // `Ltree` column type's plain-string binding.
            let row: NodeRow = sqlx::query_as(
                "INSERT INTO core.document_nodes (id, document_id, parent_id, path, \
                 depth, position, node_type, title, char_start, char_end, metadata) \
                 VALUES ($1, $2, $3, CAST($4 AS ltree), $5, $6, $7, $8, $9, $10, $11) \
                 RETURNING id, document_id, parent_id, path::text AS path, depth, \
                 position, node_type, title, char_start, char_end, metadata, created_at",
            )
            .bind(node_id)
            .bind(document_id)
            .bind(parent_id)
            .bind(&draft.path)
            .bind(draft.depth as i32)
            .bind(draft.position as i32)
            .bind(&draft.node_type)
            .bind(draft.title.as_deref())
            .bind(draft.char_start as i32)
            .bind(draft.char_end as i32)
            .bind(serde_json::Value::Object(draft.metadata.clone()))
            .fetch_one(tx.exec())
            .await
            .map_err(db_err)?;
            nodes.push(node_from_row(row));
        }
        Ok(nodes)
    }

    /// One node, by id.
    pub async fn get(&self, node_id: Uuid) -> Result<Option<DocumentNode>> {
        let row = sqlx::query_as::<_, NodeRow>(&node_select("id = $1"))
            .bind(node_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(row.map(node_from_row))
    }

    /// Every node of one document, parents before children.
    pub async fn get_tree(&self, document_id: Uuid) -> Result<Vec<DocumentNode>> {
        let rows = sqlx::query_as::<_, NodeRow>(&node_select("document_id = $1 ORDER BY path"))
            .bind(document_id)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(rows.into_iter().map(node_from_row).collect())
    }

    /// The tree down to `max_depth` — the cheap map an agent reads first.
    ///
    /// Depth-limiting is the whole point: a 900-page book's full node list
    /// is thousands of rows, while its parts and chapters are a few dozen.
    pub async fn get_outline(
        &self,
        document_id: Uuid,
        max_depth: Option<i64>,
    ) -> Result<Vec<DocumentNode>> {
        let predicate = match max_depth {
            Some(_) => "document_id = $1 AND depth <= $2 ORDER BY path",
            None => "document_id = $1 ORDER BY path",
        };
        let sql = node_select(predicate);
        let mut query = sqlx::query_as::<_, NodeRow>(&sql).bind(document_id);
        if let Some(depth) = max_depth {
            query = query.bind(depth as i32);
        }
        query
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)
            .map(|rows| rows.into_iter().map(node_from_row).collect())
    }

    /// A node and everything beneath it, via the ltree containment operator.
    ///
    /// `<@` is why `path` is an ltree with a GiST index: one index scan
    /// whatever the depth, where a `parent_id` walk would be one query per
    /// level.
    pub async fn get_subtree(&self, node_id: Uuid) -> Result<Vec<DocumentNode>> {
        let rows = sqlx::query_as::<_, NodeRow>(
            "SELECT c.id, c.document_id, c.parent_id, c.path::text AS path, c.depth, \
             c.position, c.node_type, c.title, c.char_start, c.char_end, \
             c.metadata, c.created_at \
             FROM core.document_nodes c \
             JOIN core.document_nodes n ON n.id = $1 \
             WHERE c.document_id = n.document_id AND c.path <@ n.path \
             ORDER BY c.path",
        )
        .bind(node_id)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(node_from_row).collect())
    }

    /// The chain from root down to `node_id`, inclusive.
    ///
    /// This is what turns a search hit into a citation a person can check:
    /// the titles along this chain are "Vol. II, Pt. 3, ch. 14".
    pub async fn get_ancestors(&self, node_id: Uuid) -> Result<Vec<DocumentNode>> {
        let rows = sqlx::query_as::<_, NodeRow>(
            "SELECT a.id, a.document_id, a.parent_id, a.path::text AS path, a.depth, \
             a.position, a.node_type, a.title, a.char_start, a.char_end, \
             a.metadata, a.created_at \
             FROM core.document_nodes a \
             JOIN core.document_nodes n ON n.id = $1 \
             WHERE a.document_id = n.document_id AND n.path <@ a.path \
             ORDER BY a.path",
        )
        .bind(node_id)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(node_from_row).collect())
    }

    /// `get_ancestors` for many nodes in one round trip.
    ///
    /// Search expands every hit, so the per-node form would be one query per
    /// result. Same self-join, keyed by the node asked about. Ordered by
    /// ancestor depth rather than by path: the chain is strictly nested, so
    /// the two agree, and depth is the cheaper and more obviously correct
    /// key.
    pub async fn get_ancestors_many(
        &self,
        node_ids: &[Uuid],
    ) -> Result<HashMap<Uuid, Vec<DocumentNode>>> {
        if node_ids.is_empty() {
            return Ok(HashMap::new());
        }
        let rows: Vec<AncestorsRow> = sqlx::query_as(
            "SELECT n.id AS anchor_id, a.id, a.document_id, a.parent_id, \
             a.path::text AS path, a.depth, a.position, a.node_type, a.title, \
             a.char_start, a.char_end, a.metadata, a.created_at \
             FROM core.document_nodes n \
             JOIN core.document_nodes a \
             ON a.document_id = n.document_id AND n.path <@ a.path \
             WHERE n.id = ANY($1) ORDER BY n.id, a.depth",
        )
        .bind(node_ids)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        let mut chains: HashMap<Uuid, Vec<DocumentNode>> = HashMap::new();
        for row in rows {
            chains
                .entry(row.anchor_id)
                .or_default()
                .push(node_from_row(row.node));
        }
        Ok(chains)
    }

    /// The deepest node whose span encloses `[char_start, char_end)`.
    ///
    /// Deepest, not first: every span is enclosed by the root, and the useful
    /// answer is the most specific one — the section a passage sits in, not
    /// the document it belongs to.
    pub async fn find_by_span(
        &self,
        document_id: Uuid,
        char_start: i64,
        char_end: i64,
    ) -> Result<Option<DocumentNode>> {
        let row = sqlx::query_as::<_, NodeRow>(&node_select(
            "document_id = $1 AND char_start <= $2 AND char_end >= $3 \
             ORDER BY depth DESC LIMIT 1",
        ))
        .bind(document_id)
        .bind(char_start as i32)
        .bind(char_end as i32)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(row.map(node_from_row))
    }

    /// Drop a document's whole tree. Re-parsing replaces it wholesale.
    pub async fn delete_for_document(&self, tx: &mut PgTx, document_id: Uuid) -> Result<u64> {
        sqlx::query("DELETE FROM core.document_nodes WHERE document_id = $1")
            .bind(document_id)
            .execute(tx.exec())
            .await
            .map_err(db_err)
            .map(|done| done.rows_affected())
    }

    /// Row count.
    pub async fn count(&self) -> Result<i64> {
        sqlx::query_scalar("SELECT count(*) FROM core.document_nodes")
            .fetch_one(&self.pool)
            .await
            .map_err(db_err)
    }
}

/// One `core.document_nodes` row, decoded by column name.
///
/// `path` always arrives via `path::text AS path`: decoding the `ltree`
/// column directly would depend on driver support for the type, and the
/// text form is exactly the `str(row.path)` the Python mapping reads.
#[derive(Debug, Clone, FromRow)]
pub(crate) struct NodeRow {
    pub id: Uuid,
    pub document_id: Uuid,
    pub parent_id: Option<Uuid>,
    pub path: String,
    pub depth: i32,
    pub position: i32,
    pub node_type: String,
    pub title: Option<String>,
    pub char_start: i32,
    pub char_end: i32,
    pub metadata: Option<serde_json::Value>,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

/// One row of the ancestors-many self-join: the anchor plus the ancestor.
#[derive(Debug, Clone, FromRow)]
struct AncestorsRow {
    pub anchor_id: Uuid,
    #[sqlx(flatten)]
    pub node: NodeRow,
}

/// Map a row to the domain type (`metadata NULL` reads as `{}`).
pub(crate) fn node_from_row(row: NodeRow) -> DocumentNode {
    DocumentNode {
        id: row.id,
        document_id: row.document_id,
        parent_id: row.parent_id,
        path: row.path,
        depth: i64::from(row.depth),
        position: i64::from(row.position),
        node_type: row.node_type,
        title: row.title,
        char_start: i64::from(row.char_start),
        char_end: i64::from(row.char_end),
        metadata: match row.metadata {
            Some(serde_json::Value::Object(map)) => map,
            _ => serde_json::Map::new(),
        },
        created_at: row.created_at,
    }
}

/// `SELECT <all node columns> FROM core.document_nodes WHERE <predicate>`.
fn node_select(predicate: &str) -> String {
    format!(
        "SELECT id, document_id, parent_id, path::text AS path, depth, position, \
         node_type, title, char_start, char_end, metadata, created_at \
         FROM core.document_nodes WHERE {predicate}"
    )
}

/// Resolve each draft's `parent_path` to its `parent_id`, minting node ids.
///
/// Pure so the tree-order contract is unit-testable without a database:
/// every parent must precede its children, and a draft naming a parent that
/// has not been inserted refuses with the byte-identical Python message.
/// (Byte-identical for real paths: ltree labels admit no quotes, so the
/// Python `{!r}` single-quote rendering and this literal rendering agree.
/// A path carrying a quote would render differently — and would already have
/// failed the ltree cast.)
pub(crate) fn resolve_parent_ids(
    drafts: &[DocumentNodeDraft],
) -> Result<Vec<(Uuid, Option<Uuid>)>> {
    let mut ids_by_path: HashMap<&str, Uuid> = HashMap::new();
    let mut resolved = Vec::with_capacity(drafts.len());
    for draft in drafts {
        let node_id = Uuid::new_v4();
        let parent_id = match &draft.parent_path {
            None => None,
            Some(parent_path) => match ids_by_path.get(parent_path.as_str()) {
                Some(parent_id) => Some(*parent_id),
                None => {
                    return Err(Error::Validation(format!(
                        "Node '{}' names parent '{}', which has not been inserted. \
                         Drafts must be in tree order.",
                        draft.path, parent_path,
                    )));
                }
            },
        };
        ids_by_path.insert(draft.path.as_str(), node_id);
        resolved.push((node_id, parent_id));
    }
    Ok(resolved)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors `tests/unit/adapters/test_repository_surface.py` for
    // `PGDocumentNodeRepo`.
    #[test]
    fn repository_exposes_expected_methods() {
        let _ = PgDocumentNodeRepo::insert_many;
        let _ = PgDocumentNodeRepo::get;
        let _ = PgDocumentNodeRepo::get_tree;
        let _ = PgDocumentNodeRepo::get_outline;
        let _ = PgDocumentNodeRepo::get_subtree;
        let _ = PgDocumentNodeRepo::get_ancestors;
        let _ = PgDocumentNodeRepo::get_ancestors_many;
        let _ = PgDocumentNodeRepo::find_by_span;
        let _ = PgDocumentNodeRepo::delete_for_document;
        let _ = PgDocumentNodeRepo::count;
        let _ = PgDocumentNodeRepo::new;
    }

    fn draft(path: &str, parent: Option<&str>) -> DocumentNodeDraft {
        DocumentNodeDraft {
            path: path.to_string(),
            parent_path: parent.map(str::to_string),
            depth: 0,
            position: 0,
            node_type: "section".to_string(),
            title: None,
            char_start: 0,
            char_end: 10,
            metadata: serde_json::Map::new(),
        }
    }

    #[test]
    fn parent_resolution_links_tree_order() {
        let drafts = vec![
            draft("r", None),
            draft("r.a", Some("r")),
            draft("r.a.b", Some("r.a")),
        ];
        let resolved = resolve_parent_ids(&drafts).expect("tree order resolves");
        assert_eq!(resolved[0].1, None);
        assert_eq!(resolved[1].1, Some(resolved[0].0));
        assert_eq!(resolved[2].1, Some(resolved[1].0));
        // Ids are unique per row.
        assert_ne!(resolved[0].0, resolved[1].0);
    }
    #[test]
    fn child_before_parent_refuses_byte_identically() {
        // The Python `ValueError(f"Node {path!r} names parent ...")`: the
        // payload is byte-identical, and the `Validation` Display prefix
        // (`data validation failed: `) pins the variant — no `match` arm
        // needed for the same fact.
        let drafts = vec![draft("r.a", Some("r"))];
        let err = resolve_parent_ids(&drafts).expect_err("must refuse");
        assert_eq!(
            err.to_string(),
            "data validation failed: Node 'r.a' names parent 'r', which has not \
             been inserted. Drafts must be in tree order."
        );
    }

    #[test]
    fn empty_tree_resolves_to_nothing() {
        assert!(resolve_parent_ids(&[]).expect("empty").is_empty());
    }

    #[test]
    fn row_mapping_carries_path_as_text() {
        let row = NodeRow {
            id: Uuid::new_v4(),
            document_id: Uuid::new_v4(),
            parent_id: None,
            path: "r.a".to_string(),
            depth: 1,
            position: 0,
            node_type: "section".to_string(),
            title: Some("A".to_string()),
            char_start: 0,
            char_end: 10,
            metadata: None,
            created_at: chrono::Utc::now(),
        };
        let node = node_from_row(row);
        assert_eq!(node.path, "r.a");
        assert_eq!(node.depth, 1);
        assert!(node.metadata.is_empty());
    }
}
