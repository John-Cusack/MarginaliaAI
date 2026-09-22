//! Document-structure tree mirroring `domain/nodes.py`, including the pure
//! tree-building and lookup functions.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use uuid::Uuid;

use crate::errors::{Error, Result};
use crate::sdk::PassageDraft;

/// Label of the synthetic root every document tree carries.
pub const ROOT_PATH: &str = "r";

/// A node before it has been given an identity by the repository.
///
/// `parent_path` rather than `parent_id`: the builder works in paths, and the
/// repository resolves them to ids as it inserts parents before children.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DocumentNodeDraft {
    pub path: String,
    #[serde(default)]
    pub parent_path: Option<String>,
    pub depth: i64,
    pub position: i64,
    #[serde(default = "default_node_type")]
    pub node_type: String,
    #[serde(default)]
    pub title: Option<String>,
    pub char_start: i64,
    pub char_end: i64,
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

fn default_node_type() -> String {
    "section".to_owned()
}

impl DocumentNodeDraft {
    /// The `_span_is_well_formed` model validator.
    pub fn validate(&self) -> Result<()> {
        if self.char_start < 0 {
            return Err(Error::Validation(format!(
                "char_start must be non-negative, got {}",
                self.char_start
            )));
        }
        if self.char_end < self.char_start {
            return Err(Error::Validation(format!(
                "char_end ({}) precedes char_start ({})",
                self.char_end, self.char_start
            )));
        }
        Ok(())
    }
}

/// A stored node in a document's structural tree.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DocumentNode {
    pub id: Uuid,
    pub document_id: Uuid,
    #[serde(default)]
    pub parent_id: Option<Uuid>,
    pub path: String,
    pub depth: i64,
    pub position: i64,
    pub node_type: String,
    #[serde(default)]
    pub title: Option<String>,
    pub char_start: i64,
    pub char_end: i64,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    pub created_at: DateTime<Utc>,
}

/// One flat parser section: span plus optional level/heading/node_type.
/// All other keys land in the draft's metadata, exactly as in Python.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Section {
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub level: Option<i64>,
    #[serde(default)]
    pub heading: Option<String>,
    #[serde(default)]
    pub node_type: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// Turn a parser's flat section list into a containment tree.
///
/// Nesting is inferred from `level` with a stack: a section parents to the
/// nearest preceding section of a shallower level. Drafts return in insertion
/// order — every parent precedes its children — with parent spans widened to
/// enclose their descendants.
pub fn build_node_tree(
    sections: &[Section],
    text_length: i64,
    title: Option<&str>,
) -> Result<Vec<DocumentNodeDraft>> {
    let mut drafts = vec![DocumentNodeDraft {
        path: ROOT_PATH.to_owned(),
        parent_path: None,
        depth: 0,
        position: 0,
        node_type: "document".to_owned(),
        title: title.map(str::to_owned),
        char_start: 0,
        char_end: text_length,
        metadata: Map::new(),
    }];

    // Stack of (level, index into drafts) for candidate parents, root first.
    let mut stack: Vec<(i64, usize)> = vec![(0, 0)];
    let mut child_counts: std::collections::HashMap<String, i64> =
        std::collections::HashMap::from([(ROOT_PATH.to_owned(), 0)]);

    for section in sections {
        let (Some(start), Some(end)) = (section.char_start, section.char_end) else {
            // A node without a span cannot be addressed, cited, or re-anchored.
            // Skipping is right: the passage layer still covers the prose.
            continue;
        };
        // Python's `section.get("level") or 1`: missing *and* zero both mean 1.
        let level = match section.level {
            Some(0) | None => 1,
            Some(level) => level,
        };
        while stack.len() > 1 && stack.last().is_some_and(|(l, _)| *l >= level) {
            stack.pop();
        }
        let parent_idx = stack.last().expect("root is always on the stack").1;
        let parent_path = drafts[parent_idx].path.clone();
        let parent_depth = drafts[parent_idx].depth;
        let position = child_counts.get(&parent_path).copied().unwrap_or(0);
        child_counts.insert(parent_path.clone(), position + 1);
        let path = format!("{parent_path}.n{position}");
        child_counts.insert(path.clone(), 0);

        drafts.push(DocumentNodeDraft {
            path,
            parent_path: Some(parent_path),
            depth: parent_depth + 1,
            position,
            node_type: section
                .node_type
                .clone()
                .unwrap_or_else(|| "section".to_owned()),
            title: section.heading.clone(),
            char_start: start,
            char_end: end,
            metadata: section.extra.clone(),
        });
        stack.push((level, drafts.len() - 1));
    }

    widen_parents_to_cover_children(&mut drafts);
    for draft in &drafts {
        draft.validate()?;
    }
    Ok(drafts)
}

/// The innermost node whose span encloses `[char_start, char_end)`.
///
/// Deepest, because every span is enclosed by the root and only the most
/// specific answer is useful. A passage straddling two chapters resolves to
/// their common ancestor.
pub fn deepest_containing(
    nodes: &[DocumentNode],
    char_start: i64,
    char_end: i64,
) -> Option<&DocumentNode> {
    nodes
        .iter()
        .filter(|n| n.char_start <= char_start && char_end <= n.char_end)
        .max_by_key(|n| n.depth)
}

/// Resolve each passage's containing node, in memory.
///
/// Node ids do not exist while a chunker runs, so this happens after the tree
/// is written and before the passages are — no round trip per passage.
pub fn attach_nodes(drafts: Vec<PassageDraft>, nodes: &[DocumentNode]) -> Vec<PassageDraft> {
    if nodes.is_empty() {
        return drafts;
    }
    drafts
        .into_iter()
        .map(|mut draft| {
            draft.node_id =
                deepest_containing(nodes, draft.char_start, draft.char_end).map(|n| n.id);
            draft
        })
        .collect()
}

/// Extend each node's span to enclose its descendants', deepest first.
///
/// Walking in reverse means a child has already absorbed its own descendants
/// by the time its parent reads it, so one pass suffices.
fn widen_parents_to_cover_children(drafts: &mut [DocumentNodeDraft]) {
    let mut by_path: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (idx, draft) in drafts.iter().enumerate() {
        by_path.insert(draft.path.clone(), idx);
    }
    for idx in (0..drafts.len()).rev() {
        let parent_idx = drafts[idx]
            .parent_path
            .as_ref()
            .and_then(|p| by_path.get(p).copied());
        if let Some(p) = parent_idx {
            let (start, end) = (drafts[idx].char_start, drafts[idx].char_end);
            drafts[p].char_start = drafts[p].char_start.min(start);
            drafts[p].char_end = drafts[p].char_end.max(end);
        }
    }
}
