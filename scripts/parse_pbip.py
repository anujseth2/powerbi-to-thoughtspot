#!/usr/bin/env python3
"""
parse_pbip.py - Extract a structured inventory from a Power BI Project (.pbip) folder.

Reads the TMDL semantic model (tables, columns, measures, relationships) and the
report layout (PBIR JSON, with a fallback to the legacy report.json), and writes
a single JSON inventory consumed by the rest of the migration skill.

Stdlib only. This is a v1 extractor: anything it cannot confidently parse is added
to `warnings` rather than guessed, so the migration report can surface it.

Usage:
    python parse_pbip.py <path-to-pbip-folder> --out model.json
"""
import argparse
import json
import os
import re
import sys


def _strip_quotes(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1]
    return name


def _indent(line: str) -> int:
    """Indentation depth measured in leading tabs (TMDL uses tabs)."""
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        else:
            break
    return n


def _split_ref(ref: str):
    """Split a 'Table.Column' (with optional quoting) into (table, column)."""
    ref = ref.strip()
    # Handle quoted table and/or column.
    m = re.match(r"^('(?:[^']*)'|[^.]+)\.('(?:[^']*)'|.+)$", ref)
    if not m:
        return (None, _strip_quotes(ref))
    return (_strip_quotes(m.group(1)), _strip_quotes(m.group(2)))


def parse_table_tmdl(text: str, warnings: list):
    """Parse a single table .tmdl file into a dict."""
    lines = text.splitlines()
    table = {"name": None, "columns": [], "measures": [], "hierarchies": []}

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()
        depth = _indent(raw)
        if not line or line.startswith("/*") or line.startswith("//"):
            i += 1
            continue

        # Top-level table declaration.
        if depth == 0 and line.startswith("table "):
            table["name"] = _strip_quotes(line[len("table "):])
            i += 1
            continue

        # Column (block form):  column Name  /  column Name = <calc expr>
        if line.startswith("column "):
            rest = line[len("column "):]
            col = {"name": None, "dataType": None, "summarizeBy": None,
                   "sourceColumn": None, "calculated": False, "expression": None}
            if "=" in rest:
                nm, expr = rest.split("=", 1)
                col["name"] = _strip_quotes(nm)
                col["calculated"] = True
                col["expression"] = expr.strip()
            else:
                col["name"] = _strip_quotes(rest)
            # Read indented properties.
            j = i + 1
            while j < n:
                p = lines[j]
                if not p.strip():
                    j += 1
                    continue
                if _indent(p) <= depth:
                    break
                ps = p.strip()
                if ps.startswith("dataType:"):
                    col["dataType"] = ps.split(":", 1)[1].strip()
                elif ps.startswith("summarizeBy:"):
                    col["summarizeBy"] = ps.split(":", 1)[1].strip()
                elif ps.startswith("sourceColumn:"):
                    col["sourceColumn"] = ps.split(":", 1)[1].strip()
                j += 1
            table["columns"].append(col)
            i = j
            continue

        # Measure:  measure Name = <expr>  (expr may span indented continuation lines)
        if line.startswith("measure "):
            rest = line[len("measure "):]
            if "=" not in rest:
                warnings.append(f"Measure without '=' skipped: {line[:60]}")
                i += 1
                continue
            nm, expr_head = rest.split("=", 1)
            measure = {"name": _strip_quotes(nm),
                       "expression": expr_head.strip(),
                       "formatString": None}
            # Gather continuation lines (deeper indented) that are part of the DAX
            # expression, stopping at properties or the next sibling object.
            prop_keys = ("formatString:", "displayFolder:", "description:",
                         "isHidden:", "lineageTag:", "annotation ", "changedProperty",
                         "formatStringDefinition")
            j = i + 1
            expr_parts = [measure["expression"]] if measure["expression"] else []
            while j < n:
                c = lines[j]
                if not c.strip():
                    j += 1
                    continue
                if _indent(c) <= depth:
                    break
                cs = c.strip()
                if cs.startswith("formatString:"):
                    measure["formatString"] = cs.split(":", 1)[1].strip()
                    j += 1
                    continue
                if any(cs.startswith(k) for k in prop_keys):
                    j += 1
                    continue
                expr_parts.append(cs)
                j += 1
            measure["expression"] = " ".join(p for p in expr_parts if p).strip()
            table["measures"].append(measure)
            i = j
            continue

        # Hierarchy: capture name + level names for drill mapping.
        if line.startswith("hierarchy "):
            h = {"name": _strip_quotes(line[len("hierarchy "):]), "levels": []}
            j = i + 1
            while j < n:
                c = lines[j]
                if not c.strip():
                    j += 1
                    continue
                if _indent(c) <= depth:
                    break
                cs = c.strip()
                if cs.startswith("level "):
                    h["levels"].append(_strip_quotes(cs[len("level "):]))
                j += 1
            table["hierarchies"].append(h)
            i = j
            continue

        i += 1

    if table["name"] is None:
        warnings.append("A table .tmdl file had no 'table' declaration.")
    return table


def parse_relationships_tmdl(text: str, warnings: list):
    rels = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("relationship "):
            if cur:
                rels.append(cur)
            cur = {"name": line[len("relationship "):].strip(),
                   "fromTable": None, "fromColumn": None,
                   "toTable": None, "toColumn": None,
                   "crossFilter": None, "fromCardinality": None,
                   "toCardinality": None}
        elif cur is not None and line.startswith("fromColumn:"):
            t, c = _split_ref(line.split(":", 1)[1])
            cur["fromTable"], cur["fromColumn"] = t, c
        elif cur is not None and line.startswith("toColumn:"):
            t, c = _split_ref(line.split(":", 1)[1])
            cur["toTable"], cur["toColumn"] = t, c
        elif cur is not None and line.startswith("crossFilteringBehavior:"):
            cur["crossFilter"] = line.split(":", 1)[1].strip()
        elif cur is not None and line.startswith("fromCardinality:"):
            cur["fromCardinality"] = line.split(":", 1)[1].strip()
        elif cur is not None and line.startswith("toCardinality:"):
            cur["toCardinality"] = line.split(":", 1)[1].strip()
    if cur:
        rels.append(cur)
    return rels


def parse_semantic_model(sm_dir: str, warnings: list):
    tables, relationships = [], []
    definition = os.path.join(sm_dir, "definition")
    if not os.path.isdir(definition):
        warnings.append(f"No 'definition' folder under {sm_dir}; is this TMDL format?")
        return tables, relationships

    tables_dir = os.path.join(definition, "tables")
    if os.path.isdir(tables_dir):
        for fn in sorted(os.listdir(tables_dir)):
            if fn.endswith(".tmdl"):
                with open(os.path.join(tables_dir, fn), encoding="utf-8") as f:
                    tables.append(parse_table_tmdl(f.read(), warnings))
    else:
        warnings.append("No tables/ folder found in semantic model definition.")

    rel_path = os.path.join(definition, "relationships.tmdl")
    if os.path.isfile(rel_path):
        with open(rel_path, encoding="utf-8") as f:
            relationships = parse_relationships_tmdl(f.read(), warnings)
    return tables, relationships


# Power BI QueryAggregateFunction codes -> ThoughtSpot aggregation word.
_AGG_FUNC = {0: "sum", 1: "average", 2: "min", 3: "max", 4: "count", 5: "count"}


def _field_ref(field):
    """A PBIR projection 'field' dict -> (name, source entity, kind, agg).

    Reads the real Property out of Column / Measure / Aggregation(of a Column),
    rather than trusting queryRef -- which is often a generic 'select'/'select1'
    placeholder (drops the column) or 'Sum(Table.Col)' (mangles it). For an inline
    Aggregation, also returns the aggregation word (e.g. 'sum' for Sum(BadHires))."""
    if not isinstance(field, dict):
        return None, None, None, None
    for kind in ("Column", "Measure"):
        if kind in field:
            inner = field[kind]
            ent = (((inner.get("Expression") or {}).get("SourceRef") or {}).get("Entity"))
            return inner.get("Property"), ent, kind.lower(), None
    if "Aggregation" in field:                      # inline aggregation of a column, e.g. Sum(BadHires)
        agg = field["Aggregation"]
        col = (agg.get("Expression") or {}).get("Column") or {}
        ent = ((col.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
        return col.get("Property"), ent, "aggregation", _AGG_FUNC.get(agg.get("Function"), "sum")
    return _deep_field_name(field), None, None, None


def _projection_fields(query_state):
    """Pull field names out of a PBIR visual's query projections."""
    fields = []
    if not isinstance(query_state, dict):
        return fields
    for role, proj in query_state.items():
        if not isinstance(proj, dict):
            continue
        for item in proj.get("projections", []):
            name, entity, kind, agg = _field_ref(item.get("field", item))
            name = name or item.get("queryRef")
            fields.append({"role": role, "field": name, "entity": entity, "kind": kind, "agg": agg})
    return fields


def _deep_field_name(obj):
    if isinstance(obj, dict):
        for k in ("Property", "queryRef", "displayName"):
            if k in obj and isinstance(obj[k], str):
                return obj[k]
        for v in obj.values():
            r = _deep_field_name(v)
            if r:
                return r
    return None


def parse_report_pbir(report_dir: str, warnings: list):
    """Parse the modern PBIR folder format."""
    pages = []
    definition = os.path.join(report_dir, "definition")
    pages_dir = os.path.join(definition, "pages")
    if not os.path.isdir(pages_dir):
        return None  # signal: not PBIR
    for page_id in sorted(os.listdir(pages_dir)):
        pdir = os.path.join(pages_dir, page_id)
        if not os.path.isdir(pdir):
            continue
        page = {"id": page_id, "name": page_id, "visuals": []}
        pjson = os.path.join(pdir, "page.json")
        if os.path.isfile(pjson):
            try:
                with open(pjson, encoding="utf-8") as f:
                    pd = json.load(f)
                page["name"] = pd.get("displayName", page_id)
            except Exception as e:
                warnings.append(f"Could not read {pjson}: {e}")
        vdir = os.path.join(pdir, "visuals")
        if os.path.isdir(vdir):
            for vid in sorted(os.listdir(vdir)):
                vjson = os.path.join(vdir, vid, "visual.json")
                if not os.path.isfile(vjson):
                    continue
                try:
                    with open(vjson, encoding="utf-8") as f:
                        vd = json.load(f)
                    visual_obj = vd.get("visual", {})
                    vtype = visual_obj.get("visualType", "unknown")
                    fields = _projection_fields(visual_obj.get("query", {})
                                                .get("queryState", {}))
                    page["visuals"].append(
                        {"id": vid, "type": vtype, "fields": fields})
                except Exception as e:
                    warnings.append(f"Could not parse visual {vid}: {e}")
                    page["visuals"].append(
                        {"id": vid, "type": "unparsed", "fields": []})
        pages.append(page)
    return pages


def parse_report_legacy(report_dir: str, warnings: list):
    """Fallback: legacy single report.json with embedded config strings."""
    rjson = os.path.join(report_dir, "report.json")
    if not os.path.isfile(rjson):
        return None
    pages = []
    try:
        with open(rjson, encoding="utf-8") as f:
            rd = json.load(f)
    except Exception as e:
        warnings.append(f"Could not read legacy report.json: {e}")
        return pages
    for sec in rd.get("sections", []):
        page = {"id": sec.get("name", ""),
                "name": sec.get("displayName", sec.get("name", "")),
                "visuals": []}
        for vc in sec.get("visualContainers", []):
            vtype = "unknown"
            try:
                cfg = json.loads(vc.get("config", "{}"))
                vtype = (cfg.get("singleVisual", {}).get("visualType")
                         or "unknown")
            except Exception:
                warnings.append("Could not parse a legacy visualContainer config.")
            page["visuals"].append({"id": vtype, "type": vtype, "fields": []})
        pages.append(page)
    return pages


def find_subdir(root: str, suffix: str):
    if os.path.basename(root).endswith(suffix):
        return root
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if os.path.isdir(full) and entry.endswith(suffix):
            return full
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbip", help="Path to the .pbip project folder")
    ap.add_argument("--out", default="model.json")
    args = ap.parse_args()

    root = args.pbip
    if not os.path.isdir(root):
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    warnings = []
    sm_dir = find_subdir(root, ".SemanticModel")
    report_dir = find_subdir(root, ".Report")

    if not sm_dir:
        warnings.append("No *.SemanticModel folder found.")
        tables, relationships = [], []
    else:
        tables, relationships = parse_semantic_model(sm_dir, warnings)

    pages = []
    if report_dir:
        pages = parse_report_pbir(report_dir, warnings)
        if pages is None:
            pages = parse_report_legacy(report_dir, warnings) or []
            if not pages:
                warnings.append("Report folder found but no PBIR pages or legacy "
                                "report.json could be parsed.")
    else:
        warnings.append("No *.Report folder found.")

    out = {
        "source_folder": os.path.abspath(root),
        "tables": tables,
        "relationships": relationships,
        "pages": pages,
        "counts": {
            "tables": len(tables),
            "columns": sum(len(t["columns"]) for t in tables),
            "measures": sum(len(t["measures"]) for t in tables),
            "relationships": len(relationships),
            "pages": len(pages),
            "visuals": sum(len(p["visuals"]) for p in pages),
        },
        "warnings": warnings,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {args.out}")
    print(json.dumps(out["counts"], indent=2))
    if warnings:
        print(f"\n{len(warnings)} warning(s) — review before trusting output:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
