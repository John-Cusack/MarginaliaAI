//! The document parser the accelerator ships: Markdown.
//!
//! Python source: `modules/markdown.py`. The parser takes already-decoded
//! text plus the file name and returns an SDK `ParsedDocument`; file IO
//! (strict UTF-8, universal newlines) stays caller-side. All offsets are
//! character offsets, exactly as Python string indices are.

pub mod markdown;

/// `pathlib.Path(file_name).stem` as CPython 3.11-3.13 computes it: the
/// name before its last dot, unless that dot leads (`".bashrc"`) or ends the
/// name (`"notes."`, `".."`). Only the platform's separators split: a
/// backslash is part of a POSIX file name.
pub fn py_stem(file_name: &str) -> &str {
    #[cfg(windows)]
    let base = file_name.rsplit(['/', '\\']).next().unwrap_or(file_name);
    #[cfg(not(windows))]
    let base = file_name.rsplit('/').next().unwrap_or(file_name);
    match base.rfind('.') {
        Some(i) if i > 0 && i < base.len() - 1 => &base[..i],
        _ => base,
    }
}
