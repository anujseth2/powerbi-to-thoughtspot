# ThoughtSpot TML shapes (what generate_tml.py emits)

TML is YAML. `scripts/generate_tml.py` produces these shapes; this file documents
them so you can read a generated `.tml`, hand-tweak it, or build an `overrides.json`.

Target is **MODEL TML**, not the deprecated Worksheet form. These shapes are taken
from real `exportMetadataTML` output from a ThoughtSpot cluster, so prefer them over
older examples. Objects in one import batch resolve to each other **by name**; FQNs
(GUIDs) are only needed when binding to objects that already exist on the cluster.

## Table
One per Power BI table. Binds columns to a physical table on a Connection.
```yaml
obj_id: sales-pbi
table:
  name: Sales
  db: <DATABASE>
  schema: <SCHEMA>
  db_table: Sales            # guessed from the table name; verify against the warehouse
  connection:
    name: <CONNECTION_NAME>  # add `fqn: <guid>` if you have it
  columns:
    - name: Sales Amount
      db_column_name: SalesAmount        # from the TMDL sourceColumn when present
      properties:
        column_type: MEASURE             # MEASURE for numeric facts, ATTRIBUTE otherwise
        aggregation: SUM
      db_column_properties:
        data_type: DOUBLE                # INT64 / DOUBLE / BOOL / VARCHAR / DATE / DATE_TIME
    - name: Order Date Key
      db_column_name: OrderDateKey
      properties:
        column_type: ATTRIBUTE           # numeric *keys* stay attributes, not SUM measures
      db_column_properties:
        data_type: INT64
```

## Model (joins + formulas live here)
One per project. Joins are defined inline on the **fact** (many) side's `model_tables`
entry. The join key is `'on'` — it **must be quoted** because bare `on` is a YAML boolean.
```yaml
obj_id: adventureworks-sales-pbi
model:
  name: AdventureWorks Sales
  model_tables:
    - name: Date
    - name: Product
    - name: Sales
      joins:
        - with: Date
          'on': '[Sales::Order Date Key] = [Date::Date Key]'
          type: LEFT_OUTER                # from --join-type; PBI default keeps fact rows
          cardinality: MANY_TO_ONE        # from the PBI relationship cardinality
  formulas:                               # one per migrated DAX measure / calc column
    - id: formula_Total Sales
      name: Total Sales
      expr: sum([Sales::Sales Amount])
    - id: formula_Margin Pct
      name: Margin Pct
      expr: safe_divide(sum([Sales::Margin]), sum([Sales::Sales Amount]))
  columns:
    - name: Order Date                    # a physical column: referenced by column_id
      column_id: Sales::Order Date
      properties:
        column_type: ATTRIBUTE
    - name: Total Sales                   # a formula column: referenced by formula_id
      formula_id: formula_Total Sales     # (no column_id, and no extra aggregation:
      properties:                         #  the formula's sum() already aggregates)
        column_type: MEASURE
```
A measure that needs human review (CALCULATE / time-intelligence / iterators) gets
**no** `formulas` entry and **no** column; it is recorded in `mapping.json` as
`NEEDS REVIEW` with the original DAX, so the import stays valid.

## Answer (one per Power BI visual)
```yaml
obj_id: sales-overview-columnchart-1-pbi
answer:
  name: Sales Overview - columnChart 1
  display_mode: CHART_MODE
  tables:
    - name: AdventureWorks Sales          # the Model, by name (add id/fqn if known)
  search_query: '[Product Category] [Total Sales]'
  answer_columns:
    - name: Product Category
    - name: Total Sales
  table:
    table_columns:
      - column_id: Product Category
      - column_id: Total Sales
    ordered_column_ids:
      - Product Category
      - Total Sales
  chart:
    type: COLUMN                          # COLUMN / BAR / LINE / PIE / KPI / TABLE / ...
    chart_columns:
      - column_id: Product Category
      - column_id: Total Sales
    axis_configs:
      - x: [Product Category]
        y: [Total Sales]
```

## Liveboard (one per Power BI page)
```yaml
obj_id: sales-overview-pbi
liveboard:
  name: Sales Overview
  visualizations:
    - id: Viz_1
      answer:
        name: Sales Overview - columnChart 1   # the Answer, by name
    - id: Viz_2
      answer:
        name: Sales Overview - card 2
```

## Import
```
POST /api/rest/2.0/metadata/tml/import
```
Send tables + model + answers + liveboards in **one** request so the by-name references
resolve. Requires `DATAMANAGEMENT` or `ADMINISTRATION`. Use `policy: VALIDATE_ONLY` first
to check the TML without creating objects.
