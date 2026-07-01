# DAX → ThoughtSpot formula translation

ThoughtSpot formulas are closer to SQL/spreadsheet expressions than to DAX. They have no implicit filter context and no `CALCULATE`. Translate the deterministic cases below; for everything else, produce a best-effort formula and mark it **NEEDS REVIEW**, keeping the original DAX in the migration report.

## Golden rule
DAX measures carry *filter context* from the visual they sit in. ThoughtSpot formulas are aggregations evaluated within the query's grouping. Simple aggregates map cleanly. Anything that manipulates context (`CALCULATE`, `FILTER`, `ALL`, time intelligence) does not have a 1:1 equivalent and almost always needs review.

## Deterministic mappings (safe to auto-translate)

| DAX | ThoughtSpot formula | Notes |
|---|---|---|
| `SUM(T[c])` | `sum(c)` | |
| `AVERAGE(T[c])` | `average(c)` | |
| `MIN/MAX(T[c])` | `min(c)` / `max(c)` | |
| `COUNT(T[c])` | `count(c)` | counts non-null |
| `COUNTROWS(T)` | `count(<any key col>)` | |
| `DISTINCTCOUNT(T[c])` | `unique_count(c)` | |
| `DIVIDE(a, b)` | `safe_divide(a, b)` | both handle divide-by-zero |
| `DIVIDE(a, b, alt)` | `if (b = 0) then alt else a / b` | |
| `a + b`, `a - b`, `a * b`, `a / b` | same operators | |
| `IF(cond, t, f)` | `if (cond) then t else f` | |
| `SWITCH(e, v1,r1, v2,r2, d)` | nested `if ... else if ... else d` | |
| ` && ` / ` || ` / `NOT` | `and` / `or` / `not` | |
| `CONCATENATE(a,b)` / `a & b` | `concat(a, b)` | |
| `UPPER/LOWER/LEN/TRIM` | `upper` / `lower` / `strlen` / `trim` | |
| `YEAR/MONTH/DAY(d)` | `year(d)` / `month(d)` / `day(d)` | |
| `ROUND(x, n)` | `round(x, n)` | |
| `ABS(x)` | `abs(x)` | |
| `ISBLANK(x)` | `is_null(x)` | |

## Needs-review triggers (do NOT silently translate)
Flag the measure `NEEDS REVIEW` and preserve the original DAX whenever it contains:

- `CALCULATE` / `CALCULATETABLE` — context override. A simple `CALCULATE(SUM(...), Region="East")` can be approximated with a conditional sum, but note it.
- Any **time intelligence**: `TOTALYTD`, `SAMEPERIODLASTYEAR`, `DATEADD`, `DATESYTD`, `PREVIOUSMONTH`, etc. ThoughtSpot handles period-over-period through its date keywords / `growth of` in search, not formulas — these usually become a ThoughtSpot date bucketing + comparison, not a formula. Flag for manual rebuild.
- Iterators: `SUMX`, `AVERAGEX`, `RANKX`, `FILTER`, `ADDCOLUMNS` — row-by-row evaluation.
- `ALL`, `ALLEXCEPT`, `REMOVEFILTERS`, `KEEPFILTERS` — filter removal.
- `RELATED` / `RELATEDTABLE` — relies on the model graph; usually fine once joins exist, but verify the join was created.
- `EARLIER`, variables (`VAR`/`RETURN`) with multiple steps, calculation groups.

## Approximation guidance
When you DO approximate a flagged measure, mark it **Approximated** (not Migrated), give the closest formula, and add a one-line caveat. Example:

**Example — simple CALCULATE:**
Input (DAX): `East Sales := CALCULATE(SUM(Sales[Amount]), Region[Name] = "East")`
Output (TS): `sum (if (Name = 'East') then Amount else 0)`  → status **Approximated**, caveat: "filter context approximated as conditional sum; verify against Power BI value."

**Example — time intelligence (do not force a formula):**
Input (DAX): `Sales YTD := TOTALYTD(SUM(Sales[Amount]), 'Date'[Date])`
Output: leave as **NEEDS REVIEW**, note: "Rebuild in ThoughtSpot using a date column with cumulative/'YTD' in search rather than a stored formula."
