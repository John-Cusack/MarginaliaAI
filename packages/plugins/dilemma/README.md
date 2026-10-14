# Dilemma Finder data

A workspace-only pack holding what the Dilemma Finder knows about its texts,
and the readers that load them:

- **`editions/`** — the editions catalog, one entry per edition and shared by
  every profile: what it is, which work it is an edition of, where it is and
  on what terms, its known shape, or why it is blocked and how to get it.
- **`profiles/<key>/`** — one reader's view of the library: roles and tiers
  over time (`profile.yaml`), the slices to load (`manifest.yaml`), and
  optionally entities, topics, typed hatches, vouch seeds and gold. A blind
  profile ships roles, a manifest and entities only; `guard.py` enforces it.
- **`hatch_types.yaml`** — the generic escape-hatch types every hatch instance
  names.
- **`sources/`** — one reader per source format: Scriptures in JSON, USFM,
  Tanzil, Wikisource, and pages the operator saved by hand.

Core holds the structure (acquisitions, profiles, roles, runs, marks, the
claim layer to come); this pack holds what it means. It talks to core only
through SDK clients. See `docs/design/decision-003-dilemma-finder-v2.md`.

## Use

```bash
research-engine plugin enable dilemma
research-engine dilemma profile load lds
research-engine dilemma ingest lds --slice m1
research-engine dilemma coverage kjv
research-engine dilemma blocked --profile lds
```

Raw artifacts are kept by SHA-256 under
`~/.research-engine/plugin-data/dilemma/raw/`, QA samples under `…/qa/`.
Neither is ever inside a repository: several sources are in copyright and held
for private research only.

## Sites that forbid robots

The Church's terms of use, which also govern the Joseph Smith Papers, forbid
any automated access but allow personal downloads. Those editions are
`manual`: open the URL in the catalog, save the page as HTML (for a Joseph
Smith Papers document, "View entire transcript" first) into
`~/.research-engine/plugin-data/dilemma/inbox/<edition_key>/`, and run the
ingest again. Both hosts are absent from `plugin.yaml`'s allowlist, so the
pack cannot fetch them.

Everything else is fetched politely: robots.txt is obeyed, one request per
second per host, with a descriptive User-Agent. Set `RE_DILEMMA_CONTACT` to add
a contact to it; none is sent by default. A URL already fetched is served from
the raw store.

## The blind rule

Nothing that points a blind profile at its answer — a locus, a hatch instance,
a vouch seed, a gold pair, a topic — may live in this pack. The sealed
evaluation file lives outside every repository
(`~/.research-engine/plugin-data/dilemma/sealed/`, or `RE_DILEMMA_SEALED`);
`tests/test_blind_guard.py` checks the pack against it when it is present,
and structurally everywhere.

## Adding a reader

One module in `dilemma/sources/`. A reader declares the edition keys it
handles and its parser identity, fetches (or takes inbox files) in `acquire`,
and parses in `parse` — units verbatim, never normalized beyond Unicode NFC.
Registering it needs no change to `plugin.yaml`.

Not published: `Private :: Do Not Upload`.
