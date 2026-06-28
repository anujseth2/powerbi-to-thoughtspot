# Power BI → ThoughtSpot migration skill

Convert a Power BI report into ThoughtSpot objects: extract the semantic model and
report layout from a Power BI Project (`.pbip`), generate importable ThoughtSpot
**TML** (Tables, Model, Answers, Liveboards), and produce a **migration report** that
states exactly what converted cleanly versus what needs a human.

Built as a Claude Code skill (`SKILL.md`), but the scripts run standalone. Stdlib-only
and portable — no third-party Python dependencies.

## Pipeline

```bash
# 1. Parse the .pbip folder -> one JSON inventory (TMDL model + PBIR report)
python scripts/parse_pbip.py <path-to-.pbip> --out model.json

# 2. Generate TML (Tables / Model / Answers / Liveboards) + a mapping.json of decisions
python scripts/generate_tml.py model.json --out tml_out/ \
    --connection "<TS connection>" --db <DATABASE> --schema <SCHEMA> \
    --model-name "<Model name>"

# 3. Migration report (what migrated / approximated / needs review)
python scripts/migration_report.py model.json --mapping tml_out/mapping.json --out report.md
```

Import the generated `.tml` files together via `POST /api/rest/2.0/metadata/tml/import`
(validate first with `import_policy: VALIDATE_ONLY`).

## What converts

- **Data model** (deterministic): tables, columns, data types, role inference, and
  relationships → Model joins with inline `on` / `type` / `cardinality`. Targets the
  modern MODEL TML (not the deprecated Worksheet form).
- **DAX measures** (`references/dax_to_thoughtspot.md`): the safe subset (aggregations,
  arithmetic, `IF`, `DIVIDE`, operators), `CALCULATE(<agg>, FILTER/cond)` → `sum_if`,
  recursive inlining of measure/calc-column references, and `diff_days` for date math.
- **Visuals** (`references/visual_mapping.md`): each visual → an Answer (chart type +
  field wells), each page → a Liveboard.

## What it flags (never silently downgrades)

Anything it can't faithfully translate is marked **NEEDS REVIEW** in the migration
report with the exact reason, rather than emitting confidently-wrong output:

- Time-intelligence (`SAMEPERIODLASTYEAR`, YoY/SPLY, `TOTALYTD`) — rebuild with
  ThoughtSpot's native period comparison.
- Point-in-time measures (`CALCULATE` + `ALL` + `MAX`), iterators, filter removal.
- Chart types whose measure requirement isn't met by the surviving fields (e.g. a
  combo that needs 2 measures) — flagged, the source type kept.

## Options

- `--overrides overrides.json` — supply hand-translated `ts_formula` / `ts_chart`; they win.
- name-mapping override (`connection` / `table_map` / `column_map`) — bind to existing
  physical tables.
- `--lower-db-table` — lowercase `db_table` (Databricks folds unquoted names).

## Tests

```bash
python tests/make_fixture.py        # writes a tiny synthetic .pbip
python tests/test_generate_tml.py   # YAML emitter round-trip + DAX translation
```
