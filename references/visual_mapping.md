# Power BI visual → ThoughtSpot answer mapping

Each Power BI visual becomes a ThoughtSpot **Answer**. Map the visual *type* to a chart type, and the *field wells* to the answer's columns and aggregations.

## Chart type mapping

| Power BI visual | ThoughtSpot chart type | Notes |
|---|---|---|
| Clustered/stacked column | `COLUMN` / `STACKED_COLUMN` | |
| Clustered/stacked bar | `BAR` / `STACKED_BAR` | |
| Line chart | `LINE` | |
| Area / stacked area | `AREA` | |
| Line and clustered column (combo) | `COMBO` | |
| Pie / Donut | `PIE` | |
| Scatter | `SCATTER` | |
| Table | `TABLE` | |
| Matrix | `PIVOT_TABLE` | matrix rows/cols → pivot rows/cols |
| Card / Multi-row card | `KPI` | single measure → KPI |
| Gauge | `KPI` | approximate; flag |
| Map / Filled map | `GEO_BUBBLE` / `GEO_AREA` | needs a geo-recognized column; flag if none |
| Treemap | `TREEMAP` | |
| Funnel | `FUNNEL` | |
| Waterfall | `WATERFALL` | |
| KPI visual | `KPI` | |
| Slicer | (not a visual) → becomes a **filter**, see below |
| Custom / AppSource visual | — | cannot map; **NEEDS REVIEW** |

If a visual type has no sensible target, default to `TABLE` with the same fields and flag it **Approximated** so nothing is lost.

## Field-well mapping

| Power BI field well | ThoughtSpot answer role |
|---|---|
| Axis / Category | attribute column(s) on the x / row axis |
| Legend | series / color attribute |
| Values | measure column(s) with their aggregation |
| Columns (matrix) | pivot column attribute |
| Rows (matrix) | pivot row attribute |
| Tooltips | usually dropped (note it) |
| Small multiples | dropped or flagged |

Apply the aggregation the measure/field uses (e.g. a `Values` field set to "Sum of Amount" → `sum(Amount)`).

## Filters & slicers
- A **slicer** on a page → a Liveboard filter on the corresponding column.
- A **visual-level filter** → a filter on that answer.
- A **page-level filter** → a Liveboard filter.
- Filter on a measure → flag **NEEDS REVIEW** (ThoughtSpot filters on attributes/aggregations differ).

## Page → Liveboard
Each report page becomes one Liveboard. Add every answer from that page to the liveboard, preserving a sensible left-to-right, top-to-bottom order. Power BI's exact pixel layout does not transfer — match content, not coordinates, and note that visual positioning is approximate.
