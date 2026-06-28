#!/usr/bin/env python3
"""
make_fixture.py - write a tiny synthetic Power BI Project (.pbip) folder used to
exercise parse_pbip.py -> generate_tml.py -> migration_report.py end to end.

It is deliberately small but covers the cases that matter:
  * a fact table + two dimensions, with two relationships (fact -> dim);
  * a numeric key column (must stay an ATTRIBUTE, not a SUM measure);
  * a clean measure (SUM), an approximable measure (DIVIDE), and a measure that
    must be flagged NEEDS REVIEW (TOTALYTD time-intelligence);
  * a report page with a column chart, a card (KPI), and a line chart.

TMDL is tab-indented (parse_pbip.py measures indentation in tabs), so this writer
emits real tab characters.

    python tests/make_fixture.py [dest_dir]   # default: tests/fixtures/AdventureWorksMini
"""
import json
import os
import sys

T = "\t"


def tmdl_table(name, lines):
    return f"table {name}\n" + "\n".join(lines) + "\n"


def col(name, data_type, summarize_by=None, source=None):
    out = [f"{T}column '{name}'",
           f"{T}{T}dataType: {data_type}"]
    if summarize_by:
        out.append(f"{T}{T}summarizeBy: {summarize_by}")
    out.append(f"{T}{T}sourceColumn: {source or name.replace(' ', '')}")
    return out


def measure(name, expr, fmt=None):
    out = [f"{T}measure '{name}' = {expr}"]
    if fmt:
        out.append(f"{T}{T}formatString: {fmt}")
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "fixtures", "AdventureWorksMini")
    sm = os.path.join(dest, "AdventureWorksMini.SemanticModel", "definition")
    rp = os.path.join(dest, "AdventureWorksMini.Report", "definition")

    # ---- Semantic model: tables ----
    sales = tmdl_table("Sales", [
        *col("Sales Amount", "double", "sum", "SalesAmount"),
        *col("Margin", "double", "sum", "Margin"),
        *col("Order Date", "dateTime", "none", "OrderDate"),
        *col("Order Date Key", "int64", "none", "OrderDateKey"),
        *col("Product Key", "int64", "none", "ProductKey"),
        *measure("Total Sales", "SUM(Sales[Sales Amount])", "\\$#,0"),
        *measure("Margin Pct", "DIVIDE(SUM(Sales[Margin]), SUM(Sales[Sales Amount]))", "0.0%"),
        *measure("Sales YTD", "TOTALYTD(SUM(Sales[Sales Amount]), 'Date'[Date])"),
    ])
    date = tmdl_table("Date", [
        *col("Date Key", "int64", "none", "DateKey"),
        *col("Date", "dateTime", "none", "Date"),
        *col("Calendar Year", "int64", "none", "CalendarYear"),
    ])
    product = tmdl_table("Product", [
        *col("Product Key", "int64", "none", "ProductKey"),
        *col("Product Category", "string", None, "ProductCategory"),
        *col("Product Name", "string", None, "ProductName"),
    ])
    write(os.path.join(sm, "tables", "Sales.tmdl"), sales)
    write(os.path.join(sm, "tables", "Date.tmdl"), date)
    write(os.path.join(sm, "tables", "Product.tmdl"), product)

    # ---- Semantic model: relationships ----
    rels = "\n".join([
        "relationship Sales_Date",
        f"{T}fromColumn: Sales.Order Date Key",
        f"{T}toColumn: Date.Date Key",
        f"{T}fromCardinality: many",
        f"{T}toCardinality: one",
        "",
        "relationship Sales_Product",
        f"{T}fromColumn: Sales.Product Key",
        f"{T}toColumn: Product.Product Key",
        f"{T}fromCardinality: many",
        f"{T}toCardinality: one",
        "",
    ])
    write(os.path.join(sm, "relationships.tmdl"), rels)
    write(os.path.join(sm, "model.tmdl"), "model Model\n\tculture: en-US\n")

    # ---- Report: one page, three visuals ----
    def visual(vid, vtype, fields):
        proj = {}
        for role, name in fields:
            proj.setdefault(role, {"projections": []})
            proj[role]["projections"].append({"queryRef": name})
        return {"visual": {"visualType": vtype, "query": {"queryState": proj}}}

    page = {"displayName": "Sales Overview"}
    visuals = {
        "v1_column": visual("v1", "columnChart",
                            [("Category", "Product Category"), ("Y", "Total Sales")]),
        "v2_card": visual("v2", "card", [("Values", "Total Sales")]),
        "v3_line": visual("v3", "lineChart",
                          [("Category", "Calendar Year"), ("Y", "Total Sales")]),
    }
    write(os.path.join(rp, "pages", "page1", "page.json"), json.dumps(page, indent=2))
    for vid, v in visuals.items():
        write(os.path.join(rp, "pages", "page1", "visuals", vid, "visual.json"),
              json.dumps(v, indent=2))

    print(f"Fixture written: {dest}")


if __name__ == "__main__":
    main()
