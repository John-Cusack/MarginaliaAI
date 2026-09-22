//! `scripts/wlc_extract.py`: extract the Westminster Leningrad Codex.
//!
//! Parses morphhb OSIS XML (stdlib `ElementTree` semantics — comments and
//! PIs vanish, surrounding runs merging) into chapters, renders them in the
//! LHB layout, and runs the completeness guards. The extraction logic lives
//! here, in the binary — it ran once over a corpus, not per document.
//!
//! Modes: `wlc_extract --wlc-dir DIR` dumps chapters as JSON; `--render
//! BOOK CHAPTER` prints one rendered chapter; `--check BOOK CHAPTER`
//! prints the guard findings for one OSIS file.

use std::collections::HashMap;
use std::sync::LazyLock;

use marginalia_parse::xml::{parse_xml, Element, TextModel, XmlNode};
use marginalia_text::chars::{is_space, strip};
use regex::Regex;

const OSIS_NS: &str = "http://www.bibletechnologies.net/2003/OSIS/namespace";
const VERSE_SEP: &str = "\n\n \n\n";

static HEBREW_LETTER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new("[\u{05d0}-\u{05ea}]").unwrap());

fn tag(element: &Element) -> String {
    element.tag().replace(&format!("{{{OSIS_NS}}}"), "")
}

fn child_text(element: &Element) -> &str {
    element.text()
}

/// A child element's position among ALL children: inter-element whitespace
/// is a text node, so the element's rank among elements is not its index.
fn child_index(parent: &Element, child: &Element) -> usize {
    parent
        .children
        .iter()
        .position(|node| match node {
            XmlNode::Element(el) => std::ptr::eq(el.as_ref(), child),
            _ => false,
        })
        .unwrap_or(0)
}

/// Python `repr` of a string: single quotes, backslash escapes.
fn py_repr(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for c in s.chars() {
        match c {
            '\'' => out.push_str("\\'"),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('\'');
    out
}

/// One space where the source separates two elements, nothing where it
/// does not. A tail carrying real text is corruption, and fails loudly.
fn sep_after(parent: &Element, index: usize) -> Result<String, String> {
    let tail = parent.tail_after(index);
    if tail.is_empty() {
        return Ok(String::new());
    }
    if !strip(tail).is_empty() {
        return Err(format!("unexpected text content in tail: {tail:?}"));
    }
    Ok(" ".to_owned())
}

fn word_text(element: &Element) -> String {
    element.itertext().replace('/', "")
}

#[derive(Debug, Clone, PartialEq)]
struct Token {
    text: String,
    sep: String,
    ketiv: bool,
    words: Vec<WordHit>,
    marks: Vec<MarkHit>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Word {
    verse: u64,
    offset: i64,
    length: i64,
    surface: String,
    lemma: String,
    morph: String,
    qere: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Mark {
    verse: u64,
    offset: i64,
    length: i64,
    text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Kq {
    verse: u64,
    ketiv: String,
    qere: String,
}

/// `(offset within the run, surface, lemma, morph, from a qere)`.
type WordHit = (i64, String, String, String, bool);
/// `(offset within the run, text)`.
type MarkHit = (i64, String);
/// `(verse, start, end)` into the rendered chapter.
type VerseSpan = (u64, usize, usize);

#[derive(Debug, Clone, PartialEq, Eq)]
struct Chapter {
    book: String,
    number: u64,
    verses: Vec<(u64, String)>,
    kq: Vec<Kq>,
    words: Vec<Word>,
    marks: Vec<Mark>,
}

/// `render_run`, additionally reporting where each word landed in the run.
fn render_run_words(
    children: &[&Element],
    parent: &Element,
) -> Result<(String, Vec<WordHit>, Vec<MarkHit>), String> {
    let mut out = String::new();
    let mut words = Vec::new();
    let mut marks = Vec::new();
    let mut pos: i64 = 0;
    for (i, child) in children.iter().enumerate() {
        if tag(child) == "w" {
            let piece = word_text(child);
            words.push((
                pos,
                piece.clone(),
                child.attr("lemma").unwrap_or("").to_owned(),
                child.attr("morph").unwrap_or("").to_owned(),
                true,
            ));
            out.push_str(&piece);
            pos += piece.chars().count() as i64;
        } else {
            let piece = child_text(child).to_owned();
            if !piece.is_empty() {
                marks.push((pos, piece.clone()));
            }
            out.push_str(&piece);
            pos += piece.chars().count() as i64;
        }
        if i < children.len().saturating_sub(1) {
            let sep = sep_after(parent, child_index(parent, child))?;
            out.push_str(&sep);
            pos += sep.chars().count() as i64;
        }
    }
    Ok((out, words, marks))
}

fn leading_ws(s: &str) -> i64 {
    s.chars().take_while(|c| is_space(*c)).count() as i64
}

/// Canonical text of one verse, its ketiv/qere pairs, its words, its marks.
type VerseParse = (String, Vec<(String, String)>, Vec<Word>, Vec<Mark>);

fn verse_parse(verse: &Element) -> Result<VerseParse, String> {
    let mut tokens: Vec<Token> = Vec::new();
    let mut pairs: Vec<(String, String)> = Vec::new();

    let verse_children: Vec<&Element> = verse.child_elements().collect();
    for child in verse_children.iter() {
        let typ = child.attr("type");
        let sep = sep_after(verse, child_index(verse, child))?;

        if tag(child) == "w" {
            let surface = word_text(child);
            tokens.push(Token {
                text: surface.clone(),
                sep,
                ketiv: typ == Some("x-ketiv"),
                words: vec![(
                    0,
                    surface,
                    child.attr("lemma").unwrap_or("").to_owned(),
                    child.attr("morph").unwrap_or("").to_owned(),
                    false,
                )],
                marks: Vec::new(),
            });
        } else if tag(child) == "seg" {
            // A `<seg>` directly under a verse is always a scribal mark;
            // the letter-bearing segs live under `<w>` and reach the text
            // through `word_text` instead.
            let text = child_text(child).to_owned();
            tokens.push(Token {
                text: text.clone(),
                sep,
                ketiv: child.attr("subType") == Some("x-ketiv"),
                words: Vec::new(),
                marks: if text.is_empty() {
                    Vec::new()
                } else {
                    vec![(0, text)]
                },
            });
        } else if tag(child) == "note" {
            if typ != Some("variant") {
                // Apparatus, not text.
                continue;
            }
            let rdg = child.child_elements().find(|el| {
                el.tag() == format!("{{{OSIS_NS}}}rdg") && el.attr("type") == Some("x-qere")
            });
            let (qere, qere_words, qere_marks) = match rdg {
                Some(rdg) => {
                    let kids: Vec<&Element> = rdg.child_elements().collect();
                    render_run_words(&kids, rdg)?
                }
                None => (String::new(), Vec::new(), Vec::new()),
            };

            // The ketiv may run several words, so take every trailing one.
            let mut ketiv_parts: Vec<Token> = Vec::new();
            while tokens.last().is_some_and(|tok| tok.ketiv) {
                match tokens.pop() {
                    Some(tok) => ketiv_parts.insert(0, tok),
                    // Unreachable: guarded by the loop condition.
                    None => break,
                }
            }
            let mut ketiv = String::new();
            for (i, part) in ketiv_parts.iter().enumerate() {
                ketiv.push_str(&part.text);
                if i < ketiv_parts.len().saturating_sub(1) {
                    ketiv.push_str(&part.sep);
                }
            }

            if !qere.is_empty() {
                tokens.push(Token {
                    text: qere.clone(),
                    sep,
                    ketiv: false,
                    words: qere_words,
                    marks: qere_marks,
                });
            } else if tokens
                .last()
                .is_some_and(|tok| tok.sep.is_empty() && tok.text == "־")
            {
                // Ketiv velo qere: the word is written but not read, and a
                // hyphenated orphan maqqef goes with it.
                tokens.pop();
            }
            if !ketiv.is_empty() {
                pairs.push((ketiv, qere));
            }
        } else {
            return Err(format!("unexpected element in verse: {}", tag(child)));
        }
    }

    let mut raw = String::new();
    for (i, tok) in tokens.iter().enumerate() {
        raw.push_str(&tok.text);
        if i < tokens.len().saturating_sub(1) {
            raw.push_str(&tok.sep);
        }
    }
    // Offsets come off the same string the text is built from, shifted by
    // whatever stripping removes, so they cannot drift from it.
    let shift = leading_ws(&raw);
    let mut words = Vec::new();
    let mut marks = Vec::new();
    let mut pos: i64 = 0;
    for (i, tok) in tokens.iter().enumerate() {
        for (offset, surface, lemma, morph, from_qere) in &tok.words {
            words.push(Word {
                verse: 0,
                offset: pos + offset - shift,
                length: surface.chars().count() as i64,
                surface: surface.clone(),
                lemma: lemma.clone(),
                morph: morph.clone(),
                qere: *from_qere,
            });
        }
        for (offset, text) in &tok.marks {
            marks.push(Mark {
                verse: 0,
                offset: pos + offset - shift,
                length: text.chars().count() as i64,
                text: text.clone(),
            });
        }
        pos += tok.text.chars().count() as i64
            + if i < tokens.len().saturating_sub(1) {
                tok.sep.chars().count() as i64
            } else {
                0
            };
    }
    Ok((strip(&raw).to_owned(), pairs, words, marks))
}

fn parse_book(path: &str) -> Result<Vec<Chapter>, String> {
    let raw = std::fs::read(path).map_err(|err| format!("cannot read {path}: {err}"))?;
    let document = parse_xml(&raw, TextModel::Et).map_err(|err| err.to_string())?;
    let mut chapters = Vec::new();
    for chap_el in document
        .root
        .findall_descendants(&format!("{{{OSIS_NS}}}chapter"))
    {
        let osis = chap_el.attr("osisID").unwrap_or("");
        let (book, num) = osis
            .rsplit_once('.')
            .ok_or_else(|| format!("bad chapter osisID: {osis:?}"))?;
        let mut chapter = Chapter {
            book: book.to_owned(),
            number: num
                .parse::<u64>()
                .map_err(|_| format!("bad chapter number: {num:?}"))?,
            verses: Vec::new(),
            kq: Vec::new(),
            words: Vec::new(),
            marks: Vec::new(),
        };
        for verse_el in chap_el.findall_descendants(&format!("{{{OSIS_NS}}}verse")) {
            let v_osis = verse_el.attr("osisID").unwrap_or("");
            let verse_no: u64 = v_osis
                .rsplit_once('.')
                .ok_or_else(|| format!("bad verse osisID: {v_osis:?}"))?
                .1
                .parse::<u64>()
                .map_err(|_| format!("bad verse number: {v_osis:?}"))?;
            let (text, pairs, mut verse_words, mut verse_marks) = verse_parse(verse_el)?;
            chapter.verses.push((verse_no, text));
            chapter.kq.extend(pairs.into_iter().map(|(ketiv, qere)| Kq {
                verse: verse_no,
                ketiv,
                qere,
            }));
            for word in verse_words.iter_mut() {
                word.verse = verse_no;
            }
            chapter.words.extend(verse_words);
            for mark in verse_marks.iter_mut() {
                mark.verse = verse_no;
            }
            chapter.marks.extend(verse_marks);
        }
        chapters.push(chapter);
    }
    Ok(chapters)
}

/// Lay a chapter out as LHB lays one out, and say where each verse landed.
fn render_chapter_with_spans(chapter: &Chapter) -> Result<(String, Vec<VerseSpan>), String> {
    let mut parts: Vec<String> = Vec::new();
    let mut spans = Vec::new();
    let mut pos = 0usize;
    for (i, (verse_no, text)) in chapter.verses.iter().enumerate() {
        if text.is_empty() {
            return Err(format!(
                "empty verse {} {}:{verse_no}",
                chapter.book, chapter.number
            ));
        }
        if i > 0 {
            parts.push(VERSE_SEP.to_owned());
            pos += VERSE_SEP.chars().count();
        }
        let mut head = if i == 0 {
            format!("{} ", chapter.number)
        } else {
            String::new()
        };
        head.push_str(&format!("{verse_no} \t"));
        parts.push(head.clone());
        pos += head.chars().count();
        spans.push((*verse_no, pos, pos + text.chars().count()));
        // The trailing space is LHB's; only the last one is stripped below,
        // past the last verse's end, so no span moves.
        parts.push(format!("{text} "));
        pos += text.chars().count() + 1;
    }
    let text: String = parts.concat().trim_end_matches(' ').to_owned();
    Ok((text, spans))
}

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A Python slice over characters, negative indices wrapping and all ends
/// clamped — the indexing `misplaced_words` reads through.
fn py_slice(chars: &[char], start: i64, end: i64) -> String {
    let len = chars.len() as i64;
    let resolve = |index: i64| {
        (if index < 0 {
            (len + index).max(0)
        } else {
            index.min(len)
        }) as usize
    };
    let (start, end) = (resolve(start), resolve(end));
    if start >= end {
        return String::new();
    }
    chars[start..end].iter().collect()
}

/// Words whose recorded span does not quote them.
fn misplaced_words(text: &str, words: &[Word]) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    words
        .iter()
        .filter(|w| py_slice(&chars, w.offset, w.offset + w.length) != w.surface)
        .map(|w| {
            format!(
                "{}:{} expected {} got {}",
                w.verse,
                w.offset,
                py_repr(&w.surface),
                py_repr(&py_slice(&chars, w.offset, w.offset + w.length))
            )
        })
        .collect()
}

/// Stretches of text no word and no mark claims that still hold a letter.
fn unclaimed_letters(text: &str, words: &[Word], marks: &[Mark]) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut covered = vec![false; chars.len()];
    for (start, len) in words
        .iter()
        .map(|w| (w.offset, w.length))
        .chain(marks.iter().map(|m| (m.offset, m.length)))
    {
        let mut i = start.max(0);
        let stop = (start + len).min(chars.len() as i64);
        while i < stop {
            covered[i as usize] = true;
            i += 1;
        }
    }
    let mut gaps = Vec::new();
    let mut run = String::new();
    for (i, ch) in chars.iter().enumerate() {
        if covered[i] {
            if !run.is_empty() {
                gaps.push(std::mem::take(&mut run));
            }
        } else {
            run.push(*ch);
        }
    }
    if !run.is_empty() {
        gaps.push(run);
    }
    gaps.into_iter()
        .filter(|gap| HEBREW_LETTER.is_match(gap))
        .collect()
}

/// The chapter text with every word and mark rebased onto the spans
/// `render_chapter_with_spans` computes: verse-relative offsets become
/// chapter offsets, by position rather than by search.
fn render_chapter_with_words(chapter: &Chapter) -> Result<(String, Vec<Word>, Vec<Mark>), String> {
    let (text, spans) = render_chapter_with_spans(chapter)?;
    let mut starts: HashMap<u64, usize> = HashMap::new();
    for (verse, start, _) in spans {
        starts.insert(verse, start);
    }
    let mut words = Vec::with_capacity(chapter.words.len());
    for word in &chapter.words {
        let base = starts
            .get(&word.verse)
            .ok_or_else(|| format!("verse {} has no span", word.verse))?;
        words.push(Word {
            verse: word.verse,
            offset: *base as i64 + word.offset,
            length: word.length,
            surface: word.surface.clone(),
            lemma: word.lemma.clone(),
            morph: word.morph.clone(),
            qere: word.qere,
        });
    }
    let mut marks = Vec::with_capacity(chapter.marks.len());
    for mark in &chapter.marks {
        let base = starts
            .get(&mark.verse)
            .ok_or_else(|| format!("verse {} has no span", mark.verse))?;
        marks.push(Mark {
            verse: mark.verse,
            offset: *base as i64 + mark.offset,
            length: mark.length,
            text: mark.text.clone(),
        });
    }
    Ok((text, words, marks))
}

fn chapter_json(chapter: &Chapter) -> String {
    let verses: Vec<String> = chapter
        .verses
        .iter()
        .map(|(n, text)| format!("[{n},{}]", json_escape(text)))
        .collect();
    let kq: Vec<String> = chapter
        .kq
        .iter()
        .map(|pair| {
            format!(
                "{{\"verse\":{},\"ketiv\":{},\"qere\":{}}}",
                pair.verse,
                json_escape(&pair.ketiv),
                json_escape(&pair.qere)
            )
        })
        .collect();
    let words: Vec<String> = chapter
        .words
        .iter()
        .map(|w| {
            format!(
                "{{\"verse\":{},\"offset\":{},\"length\":{},\"surface\":{},\"lemma\":{},\"morph\":{},\"qere\":{}}}",
                w.verse,
                w.offset,
                w.length,
                json_escape(&w.surface),
                json_escape(&w.lemma),
                json_escape(&w.morph),
                w.qere
            )
        })
        .collect();
    let marks: Vec<String> = chapter
        .marks
        .iter()
        .map(|m| {
            format!(
                "{{\"verse\":{},\"offset\":{},\"length\":{},\"text\":{}}}",
                m.verse,
                m.offset,
                m.length,
                json_escape(&m.text)
            )
        })
        .collect();
    format!(
        "{{\"book\":{},\"number\":{},\"verses\":[{}],\"kq\":[{}],\"words\":[{}],\"marks\":[{}]}}",
        json_escape(&chapter.book),
        chapter.number,
        verses.join(","),
        kq.join(","),
        words.join(","),
        marks.join(",")
    )
}

fn chapter_by_name(wlc_dir: &str, book: &str, number: u64) -> Result<Chapter, String> {
    for chapters in load_all(wlc_dir)? {
        for chapter in chapters {
            if chapter.book == book && chapter.number == number {
                return Ok(chapter);
            }
        }
    }
    Err(format!("no such chapter: {book}.{number}"))
}

fn load_all(wlc_dir: &str) -> Result<Vec<Vec<Chapter>>, String> {
    let mut paths: Vec<String> = std::fs::read_dir(wlc_dir)
        .map_err(|err| format!("cannot read {wlc_dir}: {err}"))?
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path().to_string_lossy().into_owned())
        .filter(|path| path.ends_with(".xml") && !path.ends_with("VerseMap.xml"))
        .collect();
    paths.sort();
    paths.into_iter().map(|path| parse_book(&path)).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: wlc_extract --wlc-dir DIR [--render BOOK CHAPTER | --check FILE]");
        std::process::exit(2);
    }
    let dir = args
        .iter()
        .position(|a| a == "--wlc-dir")
        .and_then(|i| args.get(i + 1).cloned())
        .unwrap_or_else(|| {
            eprintln!("--wlc-dir is required");
            std::process::exit(2);
        });
    if let Some(i) = args.iter().position(|a| a == "--render") {
        let (book, number) = (args[i + 1].clone(), args[i + 2].parse::<u64>().unwrap());
        let chapter = chapter_by_name(&dir, &book, number).unwrap_or_else(|err| {
            eprintln!("{err}");
            std::process::exit(1);
        });
        let (text, spans) = render_chapter_with_spans(&chapter).unwrap_or_else(|err| {
            eprintln!("{err}");
            std::process::exit(1);
        });
        println!("{}", json_escape(&text));
        let spans_json: Vec<String> = spans
            .iter()
            .map(|(v, s, e)| format!("[{v},{s},{e}]"))
            .collect();
        println!("[{}]", spans_json.join(","));
        return;
    }
    if let Some(i) = args.iter().position(|a| a == "--check") {
        let chapters = parse_book(&args[i + 1]).unwrap_or_else(|err| {
            eprintln!("{err}");
            std::process::exit(1);
        });
        for chapter in &chapters {
            let (text, words, marks) = render_chapter_with_words(chapter).unwrap_or_else(|err| {
                eprintln!("{err}");
                std::process::exit(1);
            });
            let misplaced = misplaced_words(&text, &words);
            let unclaimed = unclaimed_letters(&text, &words, &marks);
            let misplaced_json = misplaced
                .iter()
                .map(|m| json_escape(m))
                .collect::<Vec<_>>()
                .join(",");
            let unclaimed_json = unclaimed
                .iter()
                .map(|g| json_escape(g))
                .collect::<Vec<_>>()
                .join(",");
            println!(
                "{{\"book\":{},\"number\":{},\"misplaced\":[{misplaced_json}],\"unclaimed\":[{unclaimed_json}]}}",
                json_escape(&chapter.book),
                chapter.number,
            );
        }
        return;
    }
    let all = load_all(&dir).unwrap_or_else(|err| {
        eprintln!("{err}");
        std::process::exit(1);
    });
    let body: Vec<String> = all
        .iter()
        .flat_map(|chapters| chapters.iter().map(chapter_json))
        .collect();
    println!("[{}]", body.join(","));
}
