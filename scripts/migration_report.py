#!/usr/bin/env python3
"""
migration_report.py - Build the Power BI -> ThoughtSpot migration report.

Combines the parsed model inventory (from parse_pbip.py) with the mapping
decisions Claude made during Stages 1-2, and writes a markdown report that tells
the user exactly what converted cleanly versus what needs manual attention.

Usage:
    python migration_report.py model.json --mapping mapping.json --out report.md
    python migration_report.py model.json --out report.md   # skeleton (no mapping yet)

mapping.json shape (all sections optional; statuses:
Migrated | Approximated | NEEDS REVIEW | Skipped):
{
  "project_name": "AdventureWorks Sales",
  "tables":        [{"name": "Sales", "status": "Migrated", "note": ""}],
  "relationships": [{"name": "Sales->Date", "status": "Migrated", "note": ""}],
  "measures":      [{"name": "Total Sales", "original_dax": "SUM(Sales[Amount])",
                     "ts_formula": "sum([Sales::Amount])",
                     "status": "Migrated", "note": ""}],
  "visuals":       [{"page": "Overview", "visual": "Sales by Category",
                     "ts_chart": "COLUMN", "status": "Migrated", "note": ""}],
  "pages":         [{"name": "Overview", "liveboard": "Sales Overview",
                     "status": "Migrated"}]
}
"""
import argparse
import datetime
import json

STATUSES = ["Migrated", "Approximated", "NEEDS REVIEW", "Skipped"]


def tally(items):
    counts = {s: 0 for s in STATUSES}
    for it in items:
        s = it.get("status", "")
        if s in counts:
            counts[s] += 1
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="model.json from parse_pbip.py")
    ap.add_argument("--mapping", help="mapping.json with status decisions")
    ap.add_argument("--out", default="migration_report.md")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as f:
        model = json.load(f)
    mapping = {}
    if args.mapping:
        with open(args.mapping, encoding="utf-8") as f:
            mapping = json.load(f)

    counts = model.get("counts", {})
    name = mapping.get("project_name") or model.get("source_folder", "Power BI project")
    today = datetime.date.today().isoformat()

    measures = mapping.get("measures", [])
    tables = mapping.get("tables", [])
    rels = mapping.get("relationships", [])
    visuals = mapping.get("visuals", [])
    pages = mapping.get("pages", [])

    L = []
    L.append(f"# Power BI -> ThoughtSpot migration report")
    L.append(f"**Source:** {name}  **Generated:** {today}\n")

    # Summary table.
    L.append("## Summary")
    L.append("| Object type | In Power BI | Migrated | Approximated | Needs review | Skipped |")
    L.append("|---|---|---|---|---|---|")

    def row(label, source_count, items):
        t = tally(items)
        return (f"| {label} | {source_count} | {t['Migrated']} | "
                f"{t['Approximated']} | {t['NEEDS REVIEW']} | {t['Skipped']} |")

    # Split measures into those FROM Power BI (carry an original_dax) and those the
    # converter ADDED via overrides (no source DAX -- e.g. parameter-driven SPLY/YoY).
    # Tallying both under "In Power BI" makes Migrated exceed the source count.
    src_meas = [m for m in measures if (m.get("original_dax") or "").strip()]
    added_meas = [m for m in measures if not (m.get("original_dax") or "").strip()]

    L.append(row("Tables", counts.get("tables", "?"), tables))
    L.append(row("Relationships", counts.get("relationships", "?"), rels))
    L.append(row("Measures & calc cols", len(src_meas), src_meas))
    L.append(row("Visuals", counts.get("visuals", "?"), visuals))
    L.append(row("Pages", counts.get("pages", "?"), pages))
    L.append(f"\n(Columns in source: {counts.get('columns', '?')})\n")
    if added_meas:
        at = tally(added_meas)
        L.append(f"_Plus {len(added_meas)} measure(s) the converter ADDED to rebuild "
                 f"time-intelligence Power BI computes natively (parameter-driven SPLY/YoY, "
                 f"month-of-year axis) — {at['Migrated']} migrated._\n")

    # Data model.
    L.append("## Data model")
    if tables:
        L.append("### Tables")
        L.append("| Table | Status | Note |")
        L.append("|---|---|---|")
        for t in tables:
            L.append(f"| {t.get('name','')} | {t.get('status','')} | {t.get('note','')} |")
    if rels:
        L.append("\n### Relationships -> joins")
        L.append("| Relationship | Status | Note |")
        L.append("|---|---|---|")
        for r in rels:
            L.append(f"| {r.get('name','')} | {r.get('status','')} | {r.get('note','')} |")
    if measures:
        L.append("\n### Measures -> formulas")
        L.append("| Measure | Original DAX | ThoughtSpot formula | Status | Note |")
        L.append("|---|---|---|---|---|")
        for m in measures:
            dax = (m.get("original_dax", "") or "").replace("|", "\\|")
            tsf = (m.get("ts_formula", "") or "").replace("|", "\\|")
            L.append(f"| {m.get('name','')} | `{dax}` | `{tsf}` | "
                     f"{m.get('status','')} | {m.get('note','')} |")

    # Report / visuals.
    L.append("\n## Report / visuals")
    if visuals:
        L.append("| Page | Visual | ThoughtSpot chart | Status | Note |")
        L.append("|---|---|---|---|---|")
        for v in visuals:
            L.append(f"| {v.get('page','')} | {v.get('visual','')} | "
                     f"{v.get('ts_chart','')} | {v.get('status','')} | {v.get('note','')} |")
    if pages:
        L.append("\n### Pages -> liveboards")
        L.append("| Page | Liveboard | Status |")
        L.append("|---|---|---|")
        for p in pages:
            L.append(f"| {p.get('name','')} | {p.get('liveboard','')} | {p.get('status','')} |")

    # Verification checklist.
    L.append("\n## Verification checklist (do these in ThoughtSpot)")
    L.append("1. Pick one known total in Power BI (e.g. a card showing Total Sales) "
             "and confirm the SAME number appears in ThoughtSpot. This single check "
             "validates tables + joins + formula end to end.")
    L.append("2. Spot-check 2-3 more measures against their Power BI values.")
    L.append("3. Confirm each page's visuals are all present on the liveboard.")
    L.append("4. Confirm slicers / filters carried over.")

    # Needs-attention list, highest impact first.
    flagged = []
    for bucket, items, key in [
        ("Measure", measures, "name"),
        ("Relationship", rels, "name"),
        ("Table", tables, "name"),
        ("Visual", visuals, "visual"),
    ]:
        for it in items:
            if it.get("status") in ("NEEDS REVIEW", "Approximated", "Skipped"):
                flagged.append(f"- **{bucket}: {it.get(key,'')}** "
                               f"({it.get('status')}) - {it.get('note','')}")
    L.append("\n## Items needing manual attention")
    if flagged:
        L.extend(flagged)
    else:
        L.append("- None flagged. Still run the verification checklist above.")

    # Parser warnings.
    warnings = model.get("warnings", [])
    if warnings:
        L.append("\n## Parser warnings (extraction-level)")
        for w in warnings:
            L.append(f"- {w}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
