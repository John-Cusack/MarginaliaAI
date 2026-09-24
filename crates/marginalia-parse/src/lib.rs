//! The document parser the accelerator ships: Markdown.
//!
//! Python source: `modules/markdown.py`. The parser takes already-decoded
//! text plus the file name and returns an SDK `ParsedDocument`; file IO
//! (strict UTF-8, universal newlines) stays caller-side. All offsets are
//! character offsets, exactly as Python string indices are.

pub mod markdown;

/// `pathlib.Path.name`'s stem: text before the last dot, except a leading
/// dot never starts a suffix (`Path(".bashrc").stem == ".bashrc"`).
pub fn py_stem(file_name: &str) -> &str {
    let base = file_name.rsplit(['/', '\\']).next().unwrap_or(file_name);
    match base.rfind('.') {
        Some(0) | None => base,
        Some(i) => &base[..i],
    }
}
