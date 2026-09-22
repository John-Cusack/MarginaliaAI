//! `scripts/bible_layout.py`: recover verse boundaries from stored text.
//!
//! Reads one chapter file, runs the edition parser plus the coverage guard,
//! and prints the regions (and any gaps) as JSON. The layout logic lives
//! here, in the binary — it ran once over a corpus, not per document.

use std::sync::LazyLock;

use marginalia_parse::pystr::{is_lower, is_upper};
use marginalia_text::chars::{is_space, strip};
use marginalia_text::normalize::PY_WS_CLASS;
use regex::Regex;

fn ws() -> String {
    format!("[{PY_WS_CLASS}]")
}

const VERSE_SEP: &str = "\n\n \n\n";
// The literals on the next lines read as a space but are U+00A0:
// the ESV numbers its verses with a non-breaking space.
const NBSP: &str = " ";

static ESV_MARK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(\d+) ").unwrap());
static LHB_GAP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        "\\A(?:{w}*(?:\\d+ )?\\d+ \\t{w}*|{w}*)(?:\\n)?\\z",
        w = ws()
    ))
    .unwrap()
});
static ESV_GAP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        "\\A(?:{w}*(?:\\[\\[)?{w}*(?:\\d+ {w}*)?(?:\\]\\])?{w}*)(?:\\n)?\\z",
        w = ws()
    ))
    .unwrap()
});
static LHB_VERSE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A(\d+) \t").unwrap());
static FOOTNOTE_DIGITS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d+)((?:\n)?\z)").unwrap());
static ESV_BLOCK_OPEN: LazyLock<Regex> = LazyLock::new(|| Regex::new("\\A\\t*\\d+ ").unwrap());

const SENTENCE_END: [char; 13] = [
    '.', '!', '?', ';', ',', ':', '”', '’', '"', '\'', ')', '-', '—',
];

/// Python string indices are character indices; regex reports bytes. This
/// is the bridge, built once per chapter.
struct CharText {
    chars: Vec<char>,
    byte_to_char: Vec<usize>,
}

impl CharText {
    fn new(text: &str) -> Self {
        let chars: Vec<char> = text.chars().collect();
        let mut byte_to_char = vec![0usize; text.len() + 1];
        for (char_index, (byte_index, ch)) in text.char_indices().enumerate() {
            for slot in &mut byte_to_char[byte_index..byte_index + ch.len_utf8()] {
                *slot = char_index;
            }
        }
        byte_to_char[text.len()] = chars.len();
        Self {
            chars,
            byte_to_char,
        }
    }

    fn slice(&self, start: usize, end: usize) -> String {
        self.chars[start..end].iter().collect()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Region {
    kind: String,
    number: Option<u64>,
    title: Option<String>,
    start: usize,
    end: usize,
}

impl Region {
    fn verse(start: usize, end: usize, number: u64) -> Self {
        Region {
            kind: "verse".to_owned(),
            number: Some(number),
            title: None,
            start,
            end,
        }
    }

    fn to_json(&self) -> String {
        let number = self
            .number
            .map(|n| n.to_string())
            .unwrap_or_else(|| "null".to_owned());
        let title = self
            .title
            .as_ref()
            .map_or("null".to_owned(), |t| format!("{t:?}"));
        format!(
            "{{\"kind\":{:?},\"number\":{number},\"title\":{title},\"start\":{},\"end\":{}}}",
            self.kind, self.start, self.end
        )
    }
}

/// Drop the footnote numerals the ESV export glues onto a word.
fn strip_footnote_digits(s: &str) -> String {
    FOOTNOTE_DIGITS.replace(s, "$2").into_owned()
}

/// True only when every signal agrees this block is an editorial heading.
fn confident_heading(block: &str, following: Option<&str>) -> bool {
    let stripped = strip(block);
    if stripped.is_empty() || block.starts_with('\t') || ESV_MARK.is_match(block) {
        return false;
    }
    if !is_upper(stripped.chars().next().unwrap_or('\0')) {
        return false;
    }
    let cleaned = strip_footnote_digits(stripped);
    if cleaned
        .chars()
        .last()
        .is_some_and(|c| SENTENCE_END.contains(&c))
    {
        return false;
    }
    let tail = following.map(|f| strip(f).to_owned()).unwrap_or_default();
    tail.is_empty() || !is_lower(tail.chars().next().unwrap_or('\0'))
}

/// One region per verse, read back out of the renderer's own layout.
fn parse_lhb(text: &str, chart: &CharText, chapter: u64) -> Option<Vec<Region>> {
    let with_chapter = Regex::new(&format!("\\A{chapter} (\\d+) \\t")).unwrap();
    let mut regions = Vec::new();
    let mut pos = 0usize;
    let mut byte_pos = 0usize;
    for (index, block) in text.split(VERSE_SEP).enumerate() {
        let pattern = if index == 0 {
            with_chapter
                .captures(block)
                .and_then(|caps| caps.get(1))
                .or_else(|| LHB_VERSE.captures(block).and_then(|caps| caps.get(1)))?
        } else {
            LHB_VERSE.captures(block)?.get(1)?
        };
        let number: u64 = pattern.as_str().parse().ok()?;
        let match_end = pattern.end();
        // The match ends at the digits; the marker is digits + " \t".
        let body_start = match_end + " \t".len();
        let body: Vec<char> = block[body_start..].chars().collect();
        let trimmed = body.len() - body.iter().rev().take_while(|c| is_space(**c)).count();
        let start = chart.byte_to_char[byte_pos + body_start];
        regions.push(Region::verse(start, start + trimmed, number));
        pos += block.chars().count() + VERSE_SEP.chars().count();
        byte_pos += block.len() + VERSE_SEP.len();
    }
    let _ = pos;
    Some(regions)
}

/// Verse regions plus the headings confidently separated from them.
fn parse_esv(text: &str, chart: &CharText, chapter: u64) -> Option<Vec<Region>> {
    let blocks: Vec<&str> = text.split(VERSE_SEP).collect();
    let mut offsets = Vec::with_capacity(blocks.len());
    let mut pos = 0usize;
    for block in &blocks {
        offsets.push(pos);
        pos += block.chars().count() + VERSE_SEP.chars().count();
    }

    let marked: Vec<bool> = blocks.iter().map(|b| ESV_MARK.is_match(b)).collect();
    if !marked.iter().any(|m| *m) {
        return None;
    }
    let opening = marked.iter().position(|m| *m).unwrap_or(0);

    let mut heads: Vec<usize> = (0..opening)
        .filter(|i| !strip(blocks[*i]).is_empty())
        .collect();
    for (i, block) in blocks.iter().enumerate() {
        if i < opening || marked[i] || strip(block).is_empty() {
            continue;
        }
        let nxt = ((i + 1)..blocks.len()).find(|j| !strip(blocks[*j]).is_empty());
        let Some(nxt) = nxt else { continue };
        if !ESV_BLOCK_OPEN.is_match(blocks[nxt]) {
            continue;
        }
        if confident_heading(block, Some(blocks[nxt])) {
            heads.push(i);
        }
    }

    // (absolute end of marker, number), in document order.
    let mut marks: Vec<(usize, u64)> = Vec::new();
    for (i, block) in blocks.iter().enumerate() {
        if heads.contains(&i) {
            continue;
        }
        let base_byte: usize = blocks[..i].iter().map(|b| b.len() + VERSE_SEP.len()).sum();
        for caps in ESV_MARK.captures_iter(block) {
            let whole = caps.get(0).unwrap_or_else(|| caps.get(1).unwrap());
            let number: u64 = caps.get(1).unwrap().as_str().parse().ok()?;
            marks.push((chart.byte_to_char[base_byte + whole.end()], number));
        }
    }
    if marks.is_empty() || marks[0].1 != chapter {
        return None;
    }

    // John 8 opens with 7:53, printed at the head of the chapter rather
    // than the foot of the previous one: kept, and marked as foreign.
    let foreign = marks.len() > 2 && marks[1].1 > marks[2].1 && marks[2].1 == 1;
    let body = if foreign { &marks[2..] } else { &marks[1..] };

    let superscription = !foreign && marks.len() > 1 && marks[1].1 == 1;
    let numbers: Vec<u64> = if superscription || foreign {
        body.iter().map(|(_, n)| *n).collect()
    } else {
        std::iter::once(1)
            .chain(body.iter().map(|(_, n)| *n))
            .collect()
    };
    let mut sorted = numbers.clone();
    sorted.sort_unstable();
    sorted.dedup();
    if sorted != numbers || numbers.first() != Some(&1) {
        return None;
    }

    let mut head_starts: Vec<usize> = heads.iter().map(|h| offsets[*h]).collect();
    head_starts.sort_unstable();
    let mut regions: Vec<Region> = Vec::new();
    for (j, (mark_end, number)) in marks.iter().enumerate() {
        let mut stop = if j + 1 < marks.len() {
            let (nxt_end, nxt_num) = marks[j + 1];
            nxt_end - nxt_num.to_string().chars().count() - NBSP.chars().count()
        } else {
            chart.chars.len()
        };
        for h in &head_starts {
            if *mark_end < *h && *h < stop {
                stop = *h;
                break;
            }
        }
        // Poetry markers are written "\t2 \t", so the verse's own text
        // starts a tab after the marker ends.
        let body_text = chart.slice(*mark_end, stop);
        let leading = body_text.chars().take_while(|c| is_space(*c)).count();
        let trailing = body_text.chars().rev().take_while(|c| is_space(*c)).count();
        let mut begin = mark_end + leading;
        let mut end =
            mark_end + body_text.chars().count() - trailing.min(body_text.chars().count());
        if begin > end {
            begin = *mark_end;
            end = *mark_end;
        }
        if j == 0 && superscription {
            regions.push(Region {
                kind: "superscription".to_owned(),
                number: None,
                title: Some("superscription".to_owned()),
                start: begin,
                end,
            });
        } else if j == 0 && foreign {
            regions.push(Region {
                kind: "bracket".to_owned(),
                number: None,
                title: None,
                start: begin,
                end,
            });
        } else if j == 1 && foreign {
            regions.push(Region {
                kind: "foreign_verse".to_owned(),
                number: Some(*number),
                title: None,
                start: begin,
                end,
            });
        } else {
            regions.push(Region::verse(begin, end, if j == 0 { 1 } else { *number }));
        }
    }

    for h in heads.iter().copied() {
        let block = blocks[h];
        let lead = block.chars().take_while(|c| is_space(*c)).count();
        let stripped = strip(block);
        regions.push(Region {
            kind: "heading".to_owned(),
            number: None,
            title: Some(stripped.to_owned()),
            start: offsets[h] + lead,
            end: offsets[h] + lead + stripped.chars().count(),
        });
    }

    regions.sort_by_key(|r| r.start);
    Some(regions)
}

/// Verses of a chapter that carries no verse numbers at all.
fn parse_unmarked(text: &str) -> Vec<Region> {
    let mut regions = Vec::new();
    let mut pos = 0usize;
    for block in text.split("\n\n") {
        let stripped = strip(block);
        if !stripped.is_empty() {
            let lead = block.chars().take_while(|c| is_space(*c)).count();
            regions.push(Region::verse(
                pos + lead,
                pos + lead + stripped.chars().count(),
                regions.len() as u64 + 1,
            ));
        }
        pos += block.chars().count() + 2;
    }
    regions
}

/// Everything the parser did not claim, minus what is allowed to be there.
fn coverage_gaps(chart: &CharText, regions: &[Region], gap: &Regex) -> Vec<String> {
    let mut bad = Vec::new();
    let mut cursor = 0usize;
    let mut ordered: Vec<&Region> = regions.iter().collect();
    ordered.sort_by_key(|r| r.start);
    for region in ordered {
        if region.start < cursor {
            bad.push(format!("overlap at {}", region.start));
            continue;
        }
        let gap_text = chart.slice(cursor, region.start);
        if !gap.is_match(&gap_text) {
            bad.push(format!(
                "{:?}",
                gap_text.chars().take(60).collect::<String>()
            ));
        }
        cursor = region.end;
    }
    let tail = chart.slice(cursor, chart.chars.len());
    if !gap.is_match(&tail) {
        bad.push(format!("{:?}", tail.chars().take(60).collect::<String>()));
    }
    bad
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 5 {
        eprintln!("usage: bible_layout <lhb|esv|unmarked> <chapter> <text-file> <gap:lhb|esv>");
        std::process::exit(2);
    }
    let text = std::fs::read_to_string(&args[3]).unwrap_or_else(|err| {
        eprintln!("cannot read {}: {err}", args[3]);
        std::process::exit(1);
    });
    let chapter: u64 = args[2].parse().unwrap_or_else(|_| {
        eprintln!("bad chapter number");
        std::process::exit(2);
    });
    let chart = CharText::new(&text);
    let regions = match args[1].as_str() {
        "lhb" => parse_lhb(&text, &chart, chapter),
        "esv" => parse_esv(&text, &chart, chapter),
        "unmarked" => Some(parse_unmarked(&text)),
        _ => {
            eprintln!("unknown edition");
            std::process::exit(2);
        }
    };
    let gap = if args[4] == "esv" { &ESV_GAP } else { &LHB_GAP };
    match regions {
        None => println!("{{\"regions\":null}}"),
        Some(regions) => {
            let gaps = coverage_gaps(&chart, &regions, gap);
            let body: Vec<String> = regions.iter().map(Region::to_json).collect();
            let gaps_json: Vec<String> = gaps.iter().map(|g| format!("{g:?}")).collect();
            println!(
                "{{\"regions\":[{}],\"gaps\":[{}]}}",
                body.join(","),
                gaps_json.join(",")
            );
        }
    }
}
