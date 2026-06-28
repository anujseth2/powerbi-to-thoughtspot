#!/usr/bin/env python3
"""
test_generate_tml.py - unit tests for the Power BI -> ThoughtSpot TML converter.

Two things matter most and are tested adversarially:
  1. The stdlib YAML emitter never produces a scalar that re-reads as a different
     value (the dangerous failure: an under-quoted formula breaking TML import).
     A PyYAML round-trip checks emit -> load == original. PyYAML is optional; the
     round-trip test is skipped if it is not installed.
  2. translate_dax translates the safe subset and flags everything else, never
     emitting a confidently-wrong formula.

Run:  python tests/test_generate_tml.py      (or: pytest tests/test_generate_tml.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import generate_tml as g  # noqa: E402

try:
    import yaml
except Exception:
    yaml = None


# Strings that have broken naive YAML emitters. Each must survive emit -> load.
ADVERSARIAL = [
    "Date::Date",                                   # mid-string :: (must stay plain & intact)
    "sum_if([a::x] = [b::y], [c::z])",              # mid brackets/commas/equals
    "[formula_X] - [formula_Y]",                    # LEADING [ -> must quote
    "[Order Date].MONTHLY",                         # leading [ + dot bucket
    "Valid values: Open, Cash, COD",               # ": " -> must quote
    "ends with colon:",                            # trailing : -> must quote
    "a comment # here",                            # " #" -> must quote
    "yes", "no", "on", "off", "true", "null", "~",  # reserved literals
    "12345", "3.14", "-7", "1e6",                   # numeric-looking
    "  leading space", "trailing space  ",          # edge whitespace
    "*anchorish", "&ampish", "?question", "!bang",  # leading indicators
    "it's a string", '5\'6" tall',                  # internal quotes
    "90+ Days Past Due Amount",                     # leading digit, '+', spaces -> stays plain
    "if ([x] = 0) then 1 else 2",                   # formula with parens/brackets
]


def test_quoter_roundtrip():
    if yaml is None:
        print("  (skip round-trip: pyyaml not installed)")
        return
    doc = {"items": [{"v": s} for s in ADVERSARIAL],
           "on": "[A::x] = [B::y]"}        # 'on' key must survive as a string key
    text = g.dump_yaml(doc)
    loaded = yaml.safe_load(text)
    for orig, got in zip(ADVERSARIAL, [i["v"] for i in loaded["items"]]):
        assert got == orig, f"round-trip changed {orig!r} -> {got!r}\n---\n{text}"
    assert "on" in loaded, "join key 'on' was lost / coerced to bool"
    assert loaded["on"] == "[A::x] = [B::y]"
    print("  quoter round-trip: OK")


def test_quoter_style():
    # plain where safe, quoted where required (matches exportMetadataTML style)
    assert g._scalar("Date::Date") == "Date::Date"
    assert g._scalar("sum([Sales::Amount])") == "sum([Sales::Amount])"
    assert g._scalar("[formula_X] - 1").startswith("'"), "leading [ must quote"
    assert g._scalar("a: b").startswith("'"), "': ' must quote"
    assert g._scalar("on") == "'on'"
    assert g._key("on") == "'on'" and g._key("name") == "name"
    print("  quoter style: OK")


def test_translate_dax_safe():
    cases = {
        "SUM(Sales[Amount])": "sum([Sales::Amount])",
        "AVERAGE(Sales[Amount])": "average([Sales::Amount])",
        "DISTINCTCOUNT(Sales[CustomerKey])": "unique_count([Sales::CustomerKey])",
        "DIVIDE(SUM(Sales[Margin]), SUM(Sales[Amount]))":
            "safe_divide(sum([Sales::Margin]), sum([Sales::Amount]))",
        "Sales[A] + Sales[B] * 2": "[Sales::A] + [Sales::B] * 2",
    }
    for dax, want in cases.items():
        expr, status, note = g.translate_dax(dax)
        assert status in ("Migrated", "Approximated"), f"{dax} -> {status} {note}"
        assert expr == want, f"{dax}\n  got:  {expr}\n  want: {want}"
    # DIVIDE with alternate -> conditional
    expr, status, _ = g.translate_dax("DIVIDE(Sales[A], Sales[B], 0)")
    assert "if ([Sales::B] = 0) then 0 else" in expr, expr
    # IF -> if/then/else
    expr, status, _ = g.translate_dax("IF(Sales[A] > 0, Sales[A], 0)")
    assert expr == "(if ([Sales::A] > 0) then [Sales::A] else 0)", expr
    # operators
    expr, _, _ = g.translate_dax("IF(Sales[A] > 0 && Sales[B] <> 1, 1, 0)")
    assert " and " in expr and "!=" in expr, expr
    print("  translate_dax safe subset: OK")


def test_translate_dax_flags():
    for dax in [
        "TOTALYTD(SUM(Sales[Amount]), 'Date'[Date])",
        "SUMX(Sales, Sales[Qty] * Sales[Price])",
        "CALCULATE(SUM(Sales[Amount]), ALL(Sales))",       # ALL remains in the cond -> flagged
        "CALCULATE([Revenue], SAMEPERIODLASTYEAR('Date'[Date]))",  # measure-ref + time-intel
        "VAR x = SUM(Sales[Amount]) RETURN x * 2",
    ]:
        expr, status, note = g.translate_dax(dax)
        assert expr is None and status == "NEEDS REVIEW", f"{dax} should be flagged, got {status}"
        assert note, f"{dax} should carry a reason"
    print("  translate_dax flags context/iterator/time-intel: OK")


def test_translate_dax_calculate_approx():
    # CALCULATE(<agg>, FILTER(t, cond)) and CALCULATE(<agg>, <cond>) -> conditional sum_if
    expr, status, note = g.translate_dax(
        "CALCULATE(COUNT(Employee[EmplID]), FILTER(Employee, NOT(ISBLANK(Employee[TermDate]))))")
    assert status == "Approximated" and expr and expr.startswith("sum_if("), (status, expr)
    assert "isnull" in expr, expr
    expr, status, _ = g.translate_dax("CALCULATE(SUM(Sales[Amount]), Sales[Region] = \"East\")")
    assert status == "Approximated" and expr.startswith("sum_if("), expr
    assert "'East'" in expr, f"double-quote string literal should become single-quoted: {expr}"
    print("  translate_dax approximates CALCULATE+FILTER: OK")


def test_build_model_structure():
    model_json = {
        "source_folder": "/x/Demo",
        "tables": [
            {"name": "Sales",
             "columns": [
                 {"name": "Amount", "dataType": "double", "summarizeBy": "sum"},
                 {"name": "OrderKey", "dataType": "int64", "summarizeBy": "none"},
             ],
             "measures": [
                 {"name": "Total Sales", "expression": "SUM(Sales[Amount])"},
                 {"name": "Bad", "expression": "CALCULATE(SUM(Sales[Amount]), ALL(Sales))"},
             ]},
            {"name": "Date",
             "columns": [{"name": "DateKey", "dataType": "int64", "summarizeBy": "none"}],
             "measures": []},
        ],
        "relationships": [
            {"name": "S_D", "fromTable": "Sales", "fromColumn": "OrderKey",
             "toTable": "Date", "toColumn": "DateKey",
             "fromCardinality": "many", "toCardinality": "one"},
        ],
        "pages": [],
    }
    model, measures, rels = g.build_model_tml(model_json, "Demo Model", "LEFT_OUTER", {}, [])
    m = model["model"]
    # join lives on the fact table with inline on + cardinality
    sales = [t for t in m["model_tables"] if t["name"] == "Sales"][0]
    assert sales["joins"][0]["with"] == "Date"
    assert sales["joins"][0]["cardinality"] == "MANY_TO_ONE"
    assert sales["joins"][0]["on"] == "[Sales::OrderKey] = [Date::DateKey]"
    # OrderKey is a numeric key -> ATTRIBUTE, not a SUM measure
    cols = {c["name"]: c for c in m["columns"]}
    assert cols["OrderKey"]["properties"]["column_type"] == "ATTRIBUTE"
    assert cols["Amount"]["properties"] == {"column_type": "MEASURE", "aggregation": "SUM"}
    # clean measure -> formula + formula_id column; flagged measure -> no formula
    fnames = {f["name"] for f in m.get("formulas", [])}
    assert "Total Sales" in fnames and "Bad" not in fnames
    assert cols["Total Sales"]["formula_id"] == "formula_Total Sales"
    assert "column_id" not in cols["Total Sales"]
    bad = [x for x in measures if x["name"] == "Bad"][0]
    assert bad["status"] == "NEEDS REVIEW" and bad["ts_formula"] == ""
    assert rels[0]["status"] == "Migrated"
    print("  build_model_tml structure: OK")


def test_overrides_win():
    model_json = {"source_folder": "/x/D", "tables": [
        {"name": "S", "columns": [], "measures": [
            {"name": "M", "expression": "CALCULATE(SUM(S[a]), ALL(S))"}]}],
        "relationships": [], "pages": []}
    ov = {"measures": [{"name": "M", "ts_formula": "sum([S::a])", "status": "Approximated",
                        "note": "hand-translated"}]}
    model, measures, _ = g.build_model_tml(model_json, "D", "LEFT_OUTER", ov, [])
    fids = {f["name"]: f["expr"] for f in model["model"].get("formulas", [])}
    assert fids.get("M") == "sum([S::a])", "override formula should be emitted"
    assert measures[0]["status"] == "Approximated"
    print("  overrides win over default translation: OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} test group(s) passed.")
