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

    # Split measures into those FROM Power BI (carry an original_dax) and those the
    # converter ADDED via overrides (no source DAX, e.g. parameter-driven SPLY/YoY).
    src_meas = [m for m in measures if (m.get("original_dax") or "").strip()]
    added_meas = [m for m in measures if not (m.get("original_dax") or "").strip()]

    def esc(s):
        return (s or "").replace("|", "\\|")

    L = []
    L.append("# Power BI -> ThoughtSpot migration report")
    L.append(f"**Source:** {name}  ·  **Generated:** {today}")

    # ---- Summary ----
    L.append("\n## Summary\n")
    L.append("| Object | In Power BI | Migrated | Approximated | Needs review | Skipped |")
    L.append("|:--|--:|--:|--:|--:|--:|")

    def row(label, source_count, items):
        t = tally(items)
        return (f"| {label} | {source_count} | {t['Migrated']} | "
                f"{t['Approximated']} | {t['NEEDS REVIEW']} | {t['Skipped']} |")

    L.append(row("Tables", counts.get("tables", "?"), tables))
    L.append(row("Relationships", counts.get("relationships", "?"), rels))
    L.append(row("Measures & calc cols", len(src_meas), src_meas))
    L.append(row("Visuals", counts.get("visuals", "?"), visuals))
    L.append(row("Pages", counts.get("pages", "?"), pages))
    if added_meas:
        at = tally(added_meas)
        L.append(f"\n> **+{len(added_meas)} measures the converter added** to rebuild "
                 f"time-intelligence Power BI computes natively (parameter-driven SPLY/YoY, "
                 f"month-of-year axis). All {at['Migrated']} migrated.")
    L.append(f"\n_Source has {counts.get('columns', '?')} columns._")

    # ---- Needs attention (ONLY real review items; skipped decorations are noise) ----
    L.append("\n## Needs your attention\n")
    attn = []
    for it in src_meas + added_meas:
        if it.get("status") in ("NEEDS REVIEW", "Approximated"):
            attn.append(f"- **{it.get('name','')}** ({it.get('status')}): {it.get('note','') or 'verify vs Power BI'}")
    for v in visuals:
        if v.get("status") == "NEEDS REVIEW":
            attn.append(f"- **{v.get('visual','')}** ({v.get('status')}): {v.get('note','')}")
    for p in pages:
        if p.get("status") == "NEEDS REVIEW":
            attn.append(f"- **Page: {p.get('name','')}**: not migrated as a tab ({p.get('note','') or 'see visuals'})")
    if attn:
        L.append("Everything else migrated automatically. Only these need a look:\n")
        L.extend(attn)
    else:
        L.append("Nothing flagged. Still run the verification checklist below.")

    # ---- Data model (summarised, not row-per-GUID) ----
    L.append("\n## Data model\n")
    mig_tables = [t.get("name", "") for t in tables if t.get("status") == "Migrated"]
    skip_tables = [t for t in tables if t.get("status") == "Skipped"]
    if mig_tables:
        L.append(f"**Tables migrated ({len(mig_tables)}):** " + ", ".join(mig_tables) + ".")
    if skip_tables:
        L.append(f"\n**Skipped ({len(skip_tables)}):** Power BI auto date tables (internal), not migrated.")
    if rels:
        rt = tally(rels)
        bidir = sum(1 for r in rels if "bidirectional" in (r.get("note", "") or ""))
        extra = f" {bidir} had a Power BI bidirectional cross-filter (verify)." if bidir else ""
        L.append(f"\n**Relationships:** {rt['Migrated']} migrated as joins (LEFT_OUTER, MANY_TO_ONE).{extra}")

    # ---- Measures (no note column; DAX -> formula is the value) ----
    if src_meas:
        L.append("\n## Measures & calculated columns (from Power BI)\n")
        L.append("| Measure | Status | Power BI DAX | ThoughtSpot formula |")
        L.append("|:--|:--|:--|:--|")
        for m in src_meas:
            L.append(f"| {m.get('name','')} | {m.get('status','')} | "
                     f"`{esc(m.get('original_dax',''))}` | {('`'+esc(m.get('ts_formula',''))+'`') if (m.get('ts_formula') or '').strip() else '(flagged, none)'} |")
    if added_meas:
        L.append("\n## Measures added by the converter\n")
        L.append("Rebuilt because Power BI computes these natively but ThoughtSpot has no direct formula.\n")
        L.append("| Measure | ThoughtSpot formula |")
        L.append("|:--|:--|")
        for m in added_meas:
            L.append(f"| {m.get('name','')} | `{esc(m.get('ts_formula',''))}` |")

    # ---- Visuals (list the migrated; collapse skipped decorations to a count) ----
    L.append("\n## Visuals\n")
    mig_v = [v for v in visuals if v.get("status") == "Migrated"]
    skip_v = [v for v in visuals if v.get("status") == "Skipped"]
    rev_v = [v for v in visuals if v.get("status") == "NEEDS REVIEW"]
    if mig_v:
        L.append("| Page | Visual | ThoughtSpot chart |")
        L.append("|:--|:--|:--|")
        for v in mig_v:
            L.append(f"| {v.get('page','')} | {v.get('visual','')} | {v.get('ts_chart','')} |")
    notes = []
    if skip_v:
        notes.append(f"{len(skip_v)} non-data objects skipped (shapes, buttons, text boxes)")
    if rev_v:
        notes.append(f"{len(rev_v)} flagged (see Needs your attention)")
    if notes:
        L.append("\n_" + "; ".join(notes) + "._")
    mig_pages = [p.get("name", "") for p in pages if p.get("status") == "Migrated"]
    if mig_pages:
        L.append(f"\n**Pages -> liveboard tabs:** " + ", ".join(mig_pages) + " (one liveboard).")

    # ---- Verification ----
    L.append("\n## Verify in ThoughtSpot")
    L.append("1. Match one known Power BI total against ThoughtSpot (validates tables + joins + formulas end to end).")
    L.append("2. Spot-check 2-3 more measures.")
    L.append("3. Confirm each tab's visuals are present.")
    L.append("4. Confirm filters carried over.")

    warnings = model.get("warnings", [])
    if warnings:
        L.append("\n## Parser warnings")
        for w in warnings:
            L.append(f"- {w}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
