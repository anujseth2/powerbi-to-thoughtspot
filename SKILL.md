---
name: powerbi-to-thoughtspot
description: Migrate a Power BI dashboard/report into ThoughtSpot by extracting its semantic model and report layout and generating importable ThoughtSpot TML (tables, model/worksheet, answers, liveboards), plus a migration report flagging what converted cleanly versus what needs human review. Use this skill whenever the user mentions migrating, converting, porting, or moving a Power BI report, dashboard, .pbix, .pbip, semantic model, or DAX measures into ThoughtSpot — even if they don't use the word "skill" or "TML". Also trigger for "convert this Power BI file to ThoughtSpot", "turn my Power BI model into a worksheet", or any Power BI → ThoughtSpot mapping task.
---

# Power BI → ThoughtSpot migration

Convert a Power BI report into ThoughtSpot objects. The pipeline is always the same three stages — **parse the source → map concepts → emit TML** — followed by an honest **migration report** so the user knows exactly what to verify by hand.

The hard part is never the mechanical extraction; it is the judgment calls (DAX translation, visual-type choices). The skill keeps those visible instead of hiding them: anything uncertain is flagged for review rather than silently guessed.

## Inputs

The skill consumes a **Power BI Project (`.pbip`) folder**, not a binary `.pbix`. A `.pbip` stores everything as readable text:

- `*.SemanticModel/definition/` — TMDL files (one per table, plus `relationships.tmdl`, `model.tmdl`). This is the data model: tables, columns, data types, relationships, hierarchies, and DAX measures.
- `*.Report/definition/` — PBIR JSON (pages and visuals). This is the dashboard layout.

If the user only has a `.pbix`, tell them to convert it first (this is a one-time manual step, not something the skill does):
1. Open the `.pbix` in Power BI Desktop (free, Windows only).
2. File → Options → Preview features → enable **Power BI Project (.pbip) save option** and **Store semantic model using TMDL format**.
3. File → Save As → Power BI project (`.pbip`).

Alternatively, `pbi-tools extract <file.pbix>` produces a comparable folder headlessly.

## Workflow

Work the stages in order. Build the data model first and prove it before touching visuals — a model error makes every chart look wrong, so isolating it first saves time.

### Stage 0 — Locate and inventory
Find the `.SemanticModel` and `.Report` folders inside the project. Run the parser to get a structured inventory:

```bash
python scripts/parse_pbip.py <path-to-pbip-folder> --out /tmp/pbi_model.json
```

This emits a single JSON describing tables, columns, relationships, measures, pages, and visuals. Read it. If the parser flags anything it could not read (unusual TMDL constructs, an unrecognized report format), note those for the migration report — do not invent values for them.

### Stage 1 — Generate a first-cut TML (deterministic)
Run the converter on the parsed model. It emits the whole dependency graph and a
`mapping.json` recording every conversion decision and its status:

```bash
python scripts/generate_tml.py /tmp/pbi_model.json --out /tmp/tml_out \
    --connection "<TS_CONNECTION_NAME>" --db <DATABASE> --schema <SCHEMA> \
    --model-name "<Model name>"
```

What it does deterministically:
- **Each table** → a Table TML (columns, data types, role; bound to the connection).
- **Each relationship** → a join in the **Model TML** (`model_tables[].joins[]` with the matching key columns, a `type`, and `cardinality` from the Power BI relationship). The default join type is `LEFT_OUTER` (keeps fact rows, matching Power BI's relationship behavior); override with `--join-type`.
- **Each DAX measure / calculated column** → it translates the safe, unambiguous subset (simple aggregates, arithmetic, `DIVIDE`, `IF`, operators) into a **formula**. Anything with `CALCULATE`, time intelligence, iterators (`SUMX`/`RANKX`), or filter removal (`ALL`) is **not** translated: it is flagged `NEEDS REVIEW` in `mapping.json` with the original DAX preserved, and no formula is emitted for it (so the import stays valid).

### Stage 2 — Translate the flagged measures, then regenerate
This is the judgment-heavy step. Read `mapping.json`: for each measure with status `NEEDS REVIEW` or `Approximated`, translate the original DAX by hand using `references/dax_to_thoughtspot.md`. Put your translations in an `overrides.json` (same shape as `mapping.json`: `measures[].ts_formula` + `status` + `note`; `visuals[].ts_chart` to override a chart type), then regenerate so your formulas land in the TML:

```bash
python scripts/generate_tml.py /tmp/pbi_model.json --out /tmp/tml_out \
    --connection "<TS_CONNECTION_NAME>" --db <DATABASE> --schema <SCHEMA> \
    --model-name "<Model name>" --overrides /tmp/overrides.json
```

Supplied overrides always win over the built-in translation. Leave a measure out of `overrides.json` to keep it flagged for a human.

The converter also maps each **visual → Answer** (chart type via `references/visual_mapping.md`, field wells → columns) and each **report page → Liveboard**. Note: slicers and page/visual **filters are not auto-applied** yet (Power BI field-well extraction is coarse); they are surfaced for manual attention. Spot-check the generated answers and add filters by hand in ThoughtSpot.

### Stage 3 — Inspect the output
The `.tml` files in the output folder are MODEL TML (not the deprecated Worksheet form). `references/tml_templates.md` documents the exact shape the converter emits, so you can read and hand-tweak a file before import. The objects resolve to each other by name within a single import batch (Stage 4); pass `--model-fqn`/`--connection-fqn` if you already have those GUIDs and want them stamped in.

### Stage 4 — Import into ThoughtSpot
Import all related objects in a **single** API call so they resolve immediately:

```
POST /api/rest/2.0/metadata/tml/import
```

Requires `DATAMANAGEMENT` or `ADMINISTRATION` privilege. If only a worksheet is imported alone it may take a few minutes to become usable, so always bundle dependents. (Do not perform the import automatically if you lack confirmed credentials/permission from the user — generate the files and tell them how to import.)

### Stage 5 — Produce the migration report (always)
This is a required deliverable, not an optional extra. See the format below.

```bash
python scripts/migration_report.py /tmp/pbi_model.json --mapping /tmp/tml_out/mapping.json --out migration_report.md
```

(`generate_tml.py` already wrote `mapping.json`; if you supplied `--overrides` in Stage 2, re-run the converter first so the report reflects your final decisions.)

## Migration report format

ALWAYS produce a report with this structure so the user can verify efficiently:

```markdown
# Power BI → ThoughtSpot migration report
**Source:** <project name>   **Generated:** <date>

## Summary
| Object type | In Power BI | Migrated | Approximated | Needs review | Skipped |
| Tables / Columns / Relationships / Measures / Visuals / Pages | ... |

## Data model
- Tables & columns: status per table
- Relationships → joins: each one, with status
- Measures → formulas: a row per measure → | Measure | Original DAX | ThoughtSpot formula | Status |
  Status ∈ {Migrated, Approximated, NEEDS REVIEW, Skipped} with a one-line reason for anything not "Migrated".

## Report / visuals
- Per page → liveboard, and per visual → answer (chart type chosen, fields mapped, filters)
- Anything that couldn't be mapped, flagged with why.

## Verification checklist (do these in ThoughtSpot)
1. Pick one known total in Power BI (e.g. a card showing Total Sales) and confirm the SAME number appears in ThoughtSpot. This single check validates tables + joins + formula end to end.
2. Spot-check 2–3 more measures.
3. Confirm each page's visuals are all present on the liveboard.
4. Confirm slicers/filters carried over.

## Items needing manual attention
- Bulleted list, highest-impact first.
```

Be honest in the report. A measure you are unsure about must say `NEEDS REVIEW`, not `Migrated`. The report's value is that it tells the user exactly where to look.

## How the user verifies (three-window loop)
Tell the user to compare side by side:
1. **Power BI Desktop** — open the original `.pbix`/`.pbip`; use Report view (the dashboard), Model view (relationships), and the measure DAX.
2. **VS Code on the `.pbip` folder** — what was actually extracted (use this to localize a bug to extraction vs mapping).
3. **ThoughtSpot web app** — Data (model/joins/formulas), Liveboards (the dashboard), Answers (each visual).

Start verification with the "one number must match" smoke test from the checklist.

## Reference files
- `references/dax_to_thoughtspot.md` — DAX → ThoughtSpot formula translation dictionary and the "needs review" triggers. Read before translating any measure.
- `references/visual_mapping.md` — Power BI visual type → ThoughtSpot chart/answer type, and field-well mapping. Read before mapping any visual.
- `references/tml_templates.md` — TML YAML skeletons for table, model/worksheet, answer, and liveboard. Read before generating TML.

## Scripts
All three are stdlib-only and chain together (`parse → generate → report`):
- `scripts/parse_pbip.py` — deterministic extractor: TMDL + PBIR → one JSON inventory.
- `scripts/generate_tml.py` — converts that inventory into Table/Model/Answer/Liveboard TML (verified MODEL TML format) and writes a `mapping.json` of every decision + status. Translates the safe DAX subset; flags the rest. Accepts `--overrides` for your hand-translated formulas/chart types.
- `scripts/migration_report.py` — assembles the migration report from the parsed model + the `mapping.json` that `generate_tml.py` produced.

Tests: `tests/make_fixture.py` writes a tiny synthetic `.pbip`; `tests/test_generate_tml.py` covers the YAML emitter (round-trip safety) and the DAX translator. Run `python tests/test_generate_tml.py`.

## Scope notes
- File-based migration needs no paid Power BI tenant — Power BI Desktop (free) plus sample `.pbix` files is enough to build and test.
- Validate end to end against Microsoft's `Adventure Works DW 2020` sample first: it is DAX-rich and exercises multiple date relationships and bridge tables.
- Do not attempt to migrate row-level security, incremental refresh, or Power Query (M) transformations as data logic — flag them for manual handling.
