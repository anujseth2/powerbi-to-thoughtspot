#!/usr/bin/env python3
"""
generate_tml.py - Power BI parsed model (model.json) -> ThoughtSpot TML.

Reads the inventory produced by parse_pbip.py and emits importable ThoughtSpot
TML for the whole dependency graph:

    Table TML   (one per Power BI table: columns, types, db binding)
    Model TML   (one: joins from relationships, formulas from DAX measures, columns)
    Answer TML  (one per visual: chart type + columns)
    Liveboard   (ONE per report: each report page becomes a tab in layout.tabs[])

It also writes a `mapping.json` in the status shape migration_report.py consumes,
so the three scripts chain:

    parse_pbip.py  pbip/         --out model.json
    generate_tml.py model.json   --out out/ --connection "<conn>" --db DB --schema SCH
    migration_report.py model.json --mapping out/mapping.json --out report.md

Design (matches the skill's philosophy: keep judgment visible, never guess silently):
  * The data model (tables, columns, joins) is deterministic.
  * DAX measures are translated by the safe-subset translator below. Anything with
    CALCULATE / time-intelligence / iterators / filter-removal is NOT translated:
    the measure is flagged NEEDS REVIEW with the original DAX preserved, rather than
    emitting a confidently-wrong formula.
  * You can override any measure formula or visual chart type by passing --overrides
    overrides.json (same shape as mapping.json); supplied values win over the defaults.

TML format target is MODEL TML (Worksheets are deprecated), verified against real
cluster exports: joins live in model.model_tables[].joins[] with an inline quoted
'on', a `type`, and a `cardinality`; DAX measures become model.formulas[] entries
(id/name/expr) referenced from model.columns[] via `formula_id`; physical columns
are referenced via `column_id: Table::Column`.

Stdlib only (a minimal YAML emitter is included) so the skill stays self-contained.

Usage:
    python generate_tml.py model.json --out out/ \
        --connection "My Connection" --db ANALYTICS --schema PUBLIC \
        [--model-name "AdventureWorks Sales"] [--overrides overrides.json] \
        [--join-type LEFT_OUTER] [--model-fqn <model_guid>]
"""
import argparse
import json
import os
import re
import sys


# --------------------------------------------------------------------------- #
# Minimal YAML emitter (stdlib-only).                                         #
# Handles the constrained shapes we build: nested dicts, lists of dicts, and  #
# str/int/float/bool/None scalars. None values and empty dict/list values are #
# omitted. Block sequences put the dash at the parent key's indent, matching  #
# ThoughtSpot's exportMetadataTML output.                                     #
# --------------------------------------------------------------------------- #

# YAML 1.1 reads these (any case) as booleans/null, so a key or a string value
# equal to one of them must be quoted to stay a string. This is why the join
# key `on` is emitted as `'on'`.
_YAML_RESERVED = {"true", "false", "null", "none", "~", "yes", "no", "on", "off",
                  "y", "n"}
# Indicators that are only special at the START of a plain scalar (block context).
_LEADING_SPECIAL = set("!&*?|>@%\"'#,`[]{}")


def _looks_numeric(s):
    return bool(re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s))


def _needs_quote(s):
    """Quote only when a plain (unquoted) block scalar would be misread.

    In block context the flow indicators ([] {} , : & * etc.) are NOT special
    mid-string, so `Date::Date` and `sum_if([a] = [b], [c])` stay plain (matching
    ThoughtSpot's exportMetadataTML). We quote on: emptiness, edge whitespace,
    reserved/numeric literals, an ambiguous leading char, a `:` that ends a token
    (`: ` or trailing), or a comment-starting ` #`."""
    if s == "":
        return True
    if s != s.strip():                       # leading / trailing whitespace
        return True
    if s.lower() in _YAML_RESERVED or _looks_numeric(s):
        return True
    if s[0] in _LEADING_SPECIAL:
        return True
    if s[0] in "-:" and (len(s) == 1 or s[1] == " "):   # "- " / ": " style leads
        return True
    if ": " in s or s.endswith(":"):         # colon that closes a mapping key
        return True
    if " #" in s:                            # starts an inline comment
        return True
    return False


def _scalar(v):
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\r", " ").replace("\n", " ")
    if _needs_quote(s):
        return "'" + s.replace("'", "''") + "'"
    return s


def _key(k):
    return "'" + k + "'" if str(k).lower() in _YAML_RESERVED else str(k)


def _is_empty(v):
    return v is None or (isinstance(v, (dict, list)) and len(v) == 0)


def _dump_dict(d, ind):
    lines = []
    pad = " " * ind
    for k, v in d.items():
        if _is_empty(v):
            continue
        if isinstance(v, dict):
            lines.append(f"{pad}{_key(k)}:")
            lines.extend(_dump_dict(v, ind + 2))
        elif isinstance(v, list):
            lines.append(f"{pad}{_key(k)}:")
            lines.extend(_dump_list(v, ind))
        else:
            lines.append(f"{pad}{_key(k)}: {_scalar(v)}")
    return lines


def _dump_list(lst, ind):
    lines = []
    pad = " " * ind
    for item in lst:
        if isinstance(item, dict):
            inner = _dump_dict(item, ind + 2)
            if not inner:
                lines.append(f"{pad}- {{}}")
                continue
            # Hoist the first key onto the dash line.
            lines.append(f"{pad}- {inner[0][ind + 2:]}")
            lines.extend(inner[1:])
        elif isinstance(item, list):
            lines.append(f"{pad}-")
            lines.extend(_dump_list(item, ind + 2))
        else:
            lines.append(f"{pad}- {_scalar(item)}")
    return lines


def dump_yaml(obj):
    """Serialize a dict to a TML/YAML string."""
    return "\n".join(_dump_dict(obj, 0)) + "\n"


# --------------------------------------------------------------------------- #
# Naming + type conventions.                                                  #
# --------------------------------------------------------------------------- #

# Power BI / TMDL dataType -> ThoughtSpot TML db data_type.
_TML_TYPE = {
    "int64": "INT64", "int": "INT64", "integer": "INT64",
    "double": "DOUBLE", "decimal": "DOUBLE", "currency": "DOUBLE",
    "single": "DOUBLE", "float": "DOUBLE",
    "string": "VARCHAR", "text": "VARCHAR",
    "boolean": "BOOL", "bool": "BOOL",
    "datetime": "DATE_TIME", "date": "DATE", "time": "DATE_TIME",
}

# Power BI summarizeBy -> TML aggregation.
_AGG = {
    "sum": "SUM", "average": "AVERAGE", "avg": "AVERAGE",
    "min": "MIN", "max": "MAX", "count": "COUNT",
    "distinctcount": "COUNT_DISTINCT",
}

# Power BI auto-generated date tables (the implicit date hierarchy behind every
# date column). They are internal artifacts, not real source tables, so they and
# any relationship touching them are dropped (and recorded as Skipped).
_AUTO_TABLE = re.compile(r"^(LocalDateTable_|DateTableTemplate_)", re.I)


def _slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-").lower() or "obj"


def _dbname(name):
    """Display name -> a warehouse-safe physical name. Databricks Delta (and most
    warehouses) reject spaces and ` ,;{}()=\\t\\n` in identifiers, so collapse any
    run of non-alphanumeric/underscore characters to a single underscore."""
    return re.sub(r"[^0-9A-Za-z_]+", "_", str(name).strip()).strip("_") or "col"


def _is_key_col(name):
    n = str(name).strip().lower()
    return n.endswith("id") or n.endswith("key") or n.endswith("sk")


def _tml_type(data_type):
    return _TML_TYPE.get((data_type or "").strip().lower(), "VARCHAR")


def _col_role(col):
    """Infer (column_type, aggregation) for a parsed Power BI column.

    summarizeBy drives it when present; otherwise numeric non-keys are SUM
    measures and everything else is an attribute (keys included)."""
    summ = (col.get("summarizeBy") or "").strip().lower()
    dt = (col.get("dataType") or "").strip().lower()
    if summ in ("none", "default", ""):
        if summ in ("none", "default"):
            return "ATTRIBUTE", None
        # unset: infer from type
        if dt in ("int64", "int", "integer", "double", "decimal", "currency",
                  "single", "float") and not _is_key_col(col.get("name", "")):
            return "MEASURE", "SUM"
        return "ATTRIBUTE", None
    if summ in _AGG:
        return "MEASURE", _AGG[summ]
    return "ATTRIBUTE", None


# --------------------------------------------------------------------------- #
# DAX -> ThoughtSpot formula translation (safe subset).                       #
# Mirrors references/dax_to_thoughtspot.md. Returns (expr, status, note):     #
#   status in {Migrated, Approximated, NEEDS REVIEW}; expr is None when the   #
#   measure needs a human (the original DAX is preserved by the caller).      #
# --------------------------------------------------------------------------- #

# Presence of any of these makes the whole measure NEEDS REVIEW: they manipulate
# filter context / iterate / do time intelligence and have no 1:1 TS formula.
_DAX_REVIEW = {
    "calculate", "calculatetable", "filter", "all", "allexcept", "allselected",
    "removefilters", "keepfilters", "earlier", "earliest", "sumx", "averagex",
    "minx", "maxx", "countx", "rankx", "addcolumns", "summarize", "summarizecolumns",
    "topn", "values", "distinct", "related", "relatedtable", "userelationship",
    "totalytd", "totalqtd", "totalmtd", "datesytd", "datesqtd", "datesmtd",
    "sameperiodlastyear", "dateadd", "datediff", "parallelperiod", "previousmonth",
    "previousyear", "previousquarter", "previousday", "nextmonth", "nextyear",
    "lastdate", "firstdate", "startofyear", "endofyear", "startofmonth",
    "endofmonth", "var", "return", "switch",
}

# DAX function -> ThoughtSpot function (1:1, deterministic).
_DAX_FUNC = {
    "sum": "sum", "average": "average", "min": "min", "max": "max",
    "count": "count", "counta": "count", "distinctcount": "unique_count",
    "abs": "abs", "round": "round", "int": "floor", "trunc": "floor",
    "ceiling": "ceil", "floor": "floor",
    "sqrt": "sqrt", "exp": "exp", "power": "pow", "mod": "mod", "sign": "sign",
    "year": "year", "month": "month", "day": "day", "hour": "hour",
    "minute": "minute", "second": "second", "quarter": "quarter_number",
    "upper": "upper", "lower": "lower", "len": "strlen", "trim": "trim",
    "isblank": "isnull",
}

_FUNC_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")  # incl. dotted names (PERCENTILE.INC)
# Table[Column] or 'Table Name'[Column] -> capture (table, column). Unquoted
# DAX table names have no spaces (only the quoted form may), so the bare branch
# is \w-only: this stops it from swallowing a preceding keyword (e.g. "then x[c]").
_COL_REF = re.compile(r"(?:'([^']+)'|([A-Za-z_]\w*))\s*\[([^\]]+)\]")
# A bare measure reference: [Measure Name] not preceded by a table token.
_MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


def _split_args(s):
    """Split a function-call argument string on top-level commas (respecting
    nested parens and brackets). Returns the list of trimmed arg strings."""
    args, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur or args:
        args.append("".join(cur).strip())
    return args


def _match_paren(s, open_idx):
    """Given index of a '(', return index of its matching ')' (or -1)."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _calc_approx(args):
    """Approximate a 2-arg CALCULATE(<agg>, <filter>) as a conditional aggregation.
    Returns a DAX-ish `sum_if(cond, expr)` string (re-processed downstream) or None
    when the pattern isn't a simple agg+filter (e.g. wraps another measure)."""
    if len(args) != 2:
        return None                       # multiple filters / context transition: defer
    inner, filt = args[0].strip(), args[1].strip()
    if inner.startswith("(") and _match_paren(inner, 0) == len(inner) - 1:
        inner = inner[1:-1].strip()       # unwrap a paren added by reference inlining
    fm = re.match(r"(?i)FILTER\s*\(", filt)         # CALCULATE(agg, FILTER(table, cond))
    if fm:
        fc = _match_paren(filt, fm.end() - 1)
        fa = _split_args(filt[fm.end():fc]) if fc > 0 else []
        if len(fa) != 2:
            return None
        cond = fa[1]
    else:
        cond = filt                                  # CALCULATE(agg, <boolean>)
    sm = re.match(r"(?i)SUM\s*\(", inner)
    if sm:
        return f"sum_if({cond}, {inner[sm.end():_match_paren(inner, sm.end() - 1)]})"
    if re.match(r"(?i)COUNTROWS\s*\(", inner) or re.match(r"(?i)COUNTA?\s*\(", inner):
        return f"sum_if({cond}, 1)"                    # COUNT/COUNTA/COUNTROWS -> count rows meeting cond
    return None      # AVERAGE/MIN/MAX/measure-ref: not a safe 1-line approximation


def _refs_to_ids(dax, names):
    """Rewrite a DAX reference to another measure/calc-column as a ThoughtSpot
    formula-ID reference: [formula_<name>].

    ThoughtSpot resolves a sibling formula by its column id (formula_<name>), NOT by
    its display name -- a bare [Display Name] in a formula expression does not resolve
    (and a leading reserved word like 'Sum' is even mis-parsed as the agg keyword).
    Using the id keeps the measure dependency graph intact instead of inlining every
    definition: DIVIDE([Seps],[Actives]) -> [formula_Seps] / [formula_Actives], and
    SUM(Employee[isNewHire]) -> sum([formula_isNewHire]). Physical column refs
    ('Table'[Col] / Table[Col]) are NOT in `names`, so they fall through to _COL_REF
    which qualifies them to [Table::Col]. Verified on-cluster 2026-06-29.

    A reference to a measure that itself fails to translate would dangle; build_model_tml
    cascades a NEEDS-REVIEW to dependents (see _cascade_flag) so the report stays honest."""
    out = dax
    for name in sorted((n for n in names if n), key=len, reverse=True):
        # optional table qualifier ('T'[name] / T[name]) or a bare [name]
        pat = re.compile(r"(?:'[^']*'|[A-Za-z_]\w*)?\s*\[" + re.escape(name) + r"\]")
        out = pat.sub("[formula_" + name + "]", out)
    return out


def _approx_calculate(s):
    """Rewrite every approximable CALCULATE in `s` to a conditional sum_if. Returns
    the rewritten string, or None if any CALCULATE is present but not approximable
    (so the caller leaves it to be flagged NEEDS REVIEW)."""
    out, guard = s, 0
    while guard < 50:
        guard += 1
        m = re.search(r"\bCALCULATE\s*\(", out, re.I)
        if not m:
            return out
        close = _match_paren(out, m.end() - 1)
        if close < 0:
            return None
        repl = _calc_approx(_split_args(out[m.end():close]))
        if repl is None:
            return None
        out = out[:m.start()] + repl + out[close + 1:]
    return None


def translate_dax(dax, home_table=None, home_cols=None, date_cols=None, measure_dax=None):
    """Translate a DAX measure/calc-column expression to a ThoughtSpot formula.

    home_table / home_cols (the physical column display names on that table) let
    bare DAX refs like [HireDate] be qualified to [Employee::HireDate] so they bind
    by column_id and survive display-name disambiguation. date_cols (qualified
    "Table::Col" of DATE columns) turns DAX date subtraction [a]-[b] into
    diff_days([a],[b]) (TS has no date '-' operator). Returns (expr, status, note);
    expr is None for NEEDS REVIEW."""
    src = (dax or "").strip()
    if not src:
        return None, "NEEDS REVIEW", "empty measure expression"

    # Rewrite references to other measures/calc-columns as TS formula-id references
    # ([formula_<name>]) instead of inlining their DAX. TS resolves siblings by id and
    # this keeps the measure dependency graph intact. (A CALCULATE that wraps a measure
    # ref still flags, because _calc_approx only approximates a direct SUM/COUNT.)
    if measure_dax:
        src = _refs_to_ids(src, set(measure_dax))

    note_bits = []
    # Approximate the common CALCULATE(<agg>, FILTER(table, cond)) pattern as a
    # conditional sum_if BEFORE the review check (otherwise all CALCULATE is flagged).
    approx = _approx_calculate(src)
    if approx is not None and approx != src:
        src = approx
        note_bits.append("CALCULATE+FILTER approximated as a conditional sum_if; verify vs Power BI")

    funcs = [m.group(1).lower() for m in _FUNC_CALL.finditer(src)]
    bad = sorted({f for f in funcs if f in _DAX_REVIEW})
    # VAR/RETURN appear as keywords, not calls.
    if re.search(r"\bvar\b", src, re.I) and re.search(r"\breturn\b", src, re.I):
        bad.append("VAR/RETURN")
    if bad:
        return (None, "NEEDS REVIEW",
                "contains " + ", ".join(sorted(set(bad))) +
                " (filter-context / time-intelligence / iterator) - rebuild by hand")

    # Qualify Table[Col] -> [Table::Col] FIRST, before expanding IF/DIVIDE/OR/AND,
    # so the keywords those expansions introduce ("then"/"else") are never mistaken
    # for a table name by the column-ref pattern.
    expr = _COL_REF.sub(
        lambda m: f"[{(m.group(1) or m.group(2)).strip()}::{m.group(3).strip()}]", src)

    # Expand argument-aware calls: OR/AND conditions before IF; DIVIDE/ROUND anywhere.
    for fname, repl in (("DIVIDE", _divide_repl), ("OR", _or_repl),
                        ("AND", _and_repl), ("IF", _if_repl), ("ROUND", _round_repl)):
        expr = _expand_calls(expr, fname, repl)
        if expr is None:
            return None, "NEEDS REVIEW", f"could not expand {fname}()"

    # String literals: DAX uses "double" quotes, ThoughtSpot uses 'single'.
    expr = re.sub(r'"([^"]*)"', lambda m: "'" + m.group(1).replace("'", "''") + "'", expr)
    # Operators: <> -> !=, && -> and, || -> or, NOT -> not.
    expr = expr.replace("<>", "!=")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"\bNOT\b", "not", expr)
    # String concat: a & b -> concat(a, b) is non-trivial; flag if a lone & remains.
    if re.search(r"(?<![&])&(?![&])", expr):
        note_bits.append("string '&' concatenation left as-is; verify (use concat())")
        status_floor = "Approximated"
    else:
        status_floor = "Migrated"

    # CONCATENATE(a, b) -> concat(a, b)
    expr = re.sub(r"\bCONCATENATE\s*\(", "concat(", expr, flags=re.I)

    # Rename remaining known functions; any unknown function -> NEEDS REVIEW.
    unknown = []

    # Logical keywords sit before a '(' but are operators, not calls -> keep a
    # space ("else (x)"). Function passthroughs are real calls -> tight ("concat(").
    _LOGICAL_KW = {"if", "then", "else", "and", "or", "not", "in"}
    # functions we synthesize (sum_if from CALCULATE, diff_days from date subtraction)
    # plus the TS targets of _DAX_FUNC -> never flag these as unmapped.
    _PASS_FUNCS = ({"concat", "safe_divide", "sum_if", "count_if", "diff_days"}
                   | set(_DAX_FUNC.values()))

    def _rename(m):
        low = m.group(1).lower()
        if low in _DAX_FUNC:
            return _DAX_FUNC[low] + "("
        if low in _LOGICAL_KW:
            return m.group(1) + " ("
        if low in _PASS_FUNCS:
            return m.group(1) + "("
        unknown.append(m.group(1))
        return m.group(1) + "("

    expr = _FUNC_CALL.sub(_rename, expr)
    if unknown:
        return (None, "NEEDS REVIEW",
                "unmapped function(s): " + ", ".join(sorted(set(unknown))))

    for sent, kw in _SENTINELS:              # restore synthesized keywords/functions
        expr = expr.replace(sent, kw)

    # Qualify bare refs to physical home-table columns: [HireDate] -> [Employee::HireDate].
    # Leaves already-qualified refs and refs to formulas/measures (not physical
    # columns) bare, so they keep resolving by display name.
    if home_table and home_cols:
        def _qual(m):
            inner = m.group(1)
            if "::" in inner or inner not in home_cols:
                return m.group(0)
            return f"[{home_table}::{inner}]"
        expr = re.sub(r"\[([^\]]+)\]", _qual, expr)

    # DAX subtracts dates to get days ([a]-[b]); ThoughtSpot has no date '-' operator,
    # so rewrite a subtraction of two DATE columns to diff_days([a], [b]).
    if date_cols:
        dpat = re.compile(r"\[([^\]]+)\]\s*-\s*\[([^\]]+)\]")
        def _diff(m):
            a, b = m.group(1), m.group(2)
            return f"diff_days([{a}], [{b}])" if a in date_cols and b in date_cols else m.group(0)
        for _ in range(6):
            new = dpat.sub(_diff, expr)
            if new == expr:
                break
            expr = new

    expr = re.sub(r"\s+", " ", expr).strip()
    status = "Approximated" if note_bits else status_floor
    return expr, status, "; ".join(note_bits)


def _expand_calls(expr, fname, repl_fn):
    """Replace every FNAME(...) call (case-insensitive) using repl_fn(args)->str.
    Works inside-out so nested calls of the same function expand correctly.
    Returns None on an unbalanced/invalid call."""
    pat = re.compile(r"\b" + fname + r"\s*\(", re.I)
    guard = 0
    while True:
        guard += 1
        if guard > 1000:
            return None
        # find the LAST occurrence so inner calls (later in string for same-name
        # nesting) resolve first when we go right-to-left.
        matches = list(pat.finditer(expr))
        if not matches:
            return expr
        m = matches[-1]
        close = _match_paren(expr, m.end() - 1)
        if close < 0:
            return None
        args = _split_args(expr[m.end():close])
        repl = repl_fn(args)
        if repl is None:
            return None
        expr = expr[:m.start()] + repl + expr[close + 1:]


# Synthesized keywords/functions are emitted as sentinels so the case-insensitive
# expanders don't re-match (and choke on) the `if`/`or`/`and`/`round` they just
# produced. translate_dax swaps the sentinels back once all expansion is done.
_IF = "\x00IF\x00"
_OR = "\x00OR\x00"
_AND = "\x00AND\x00"
_RND = "\x00ROUND\x00"
_SENTINELS = ((_IF, "if"), (_OR, "or"), (_AND, "and"), (_RND, "round"))


def _divide_repl(args):
    if len(args) == 2:
        return f"safe_divide({args[0]}, {args[1]})"
    if len(args) == 3:
        return f"({_IF} ({args[1]} = 0) then {args[2]} else ({args[0]}) / ({args[1]}))"
    return None


def _if_repl(args):
    if len(args) == 2:
        return f"({_IF} ({args[0]}) then {args[1]} else 0)"
    if len(args) == 3:
        return f"({_IF} ({args[0]}) then {args[1]} else {args[2]})"
    return None


def _or_repl(args):    # DAX OR(a, b ...) -> (a) or (b) ...
    return "(" + f" {_OR} ".join(f"({a})" for a in args) + ")" if len(args) >= 2 else None


def _and_repl(args):   # DAX AND(a, b ...) -> (a) and (b) ...
    return "(" + f" {_AND} ".join(f"({a})" for a in args) + ")" if len(args) >= 2 else None


def _round_repl(args):
    # DAX ROUND(x, n): n = decimal places. ThoughtSpot round(x, inc): inc = rounding
    # increment. So ROUND(x, 0) -> round(x, 1); ROUND(x, 2) -> round(x, 0.01).
    if len(args) == 1:
        return f"{_RND}({args[0]})"
    if len(args) == 2 and re.fullmatch(r"-?\d+", args[1].strip()):
        n = int(args[1].strip())
        inc = 10.0 ** (-n)
        inc_s = str(int(inc)) if inc >= 1 else ("%.10f" % inc).rstrip("0")
        return f"{_RND}({args[0]}, {inc_s})"
    return None      # non-literal precision -> can't convert reliably; NEEDS REVIEW


# --------------------------------------------------------------------------- #
# Chart type mapping (mirrors references/visual_mapping.md).                  #
# --------------------------------------------------------------------------- #

_CHART_MAP = {
    # Power BI's "columnChart"/"barChart" ARE the stacked variants; the clustered
    # forms have their own visualType names. (Got this backwards originally.)
    "columnchart": "STACKED_COLUMN", "clusteredcolumnchart": "COLUMN",
    "stackedcolumnchart": "STACKED_COLUMN", "hundredpercentstackedcolumnchart": "STACKED_COLUMN",
    "barchart": "STACKED_BAR", "clusteredbarchart": "BAR",
    "stackedbarchart": "STACKED_BAR", "hundredpercentstackedbarchart": "STACKED_BAR",
    "linechart": "LINE", "areachart": "AREA", "stackedareachart": "AREA",
    "lineclusteredcolumncombochart": "LINE_COLUMN", "linestackedcolumncombochart": "LINE_STACKED_COLUMN",
    "piechart": "PIE", "donutchart": "PIE", "scatterchart": "SCATTER",
    "tableex": "GRID_TABLE", "table": "GRID_TABLE", "pivottable": "PIVOT_TABLE", "matrix": "PIVOT_TABLE",
    "card": "KPI", "multirowcard": "KPI", "cardvisual": "KPI", "kpi": "KPI", "gauge": "KPI",
    "map": "GEO_BUBBLE", "filledmapvisual": "GEO_AREA", "shapemap": "GEO_AREA",
    "treemap": "TREEMAP", "funnel": "FUNNEL", "waterfallchart": "WATERFALL",
}
_NON_VISUAL = {"slicer", "advancedslicervisual", "textbox", "actionbutton", "image", "shape"}
# Minimum measures a chart type needs to render; used to FLAG (not downgrade) a viz
# whose measures didn't all translate. Types not listed need 1 (tables need 0).
_CHART_NEEDS = {"LINE_COLUMN": 2, "LINE_STACKED_COLUMN": 2, "SCATTER": 2, "ADVANCED_BUBBLE": 2}


_MONTH_PARTS = {"month", "month name", "monthname", "month of year"}


def _date_bucket_map(model_json):
    """Map a date-table month-name column -> (search_token, bucketed_label).

    A Power BI date hierarchy puts a "Month" text column on the axis but sorts it
    by an underlying date, so it reads Jan, Feb, Mar... In ThoughtSpot a varchar
    Month sorts alphabetically (Apr, Aug, Dec...), wrecking the order. The faithful
    equivalent is the base date column bucketed monthly: the search keyword
    `[Date].MONTHLY`, which surfaces as a `Month(Date)` column. Returns
    {month_col_display_name: ("[Date].MONTHLY", "Month(Date)")}."""
    out = {}
    for t in model_json.get("tables", []):
        date_col, month_cols = None, []
        for c in t.get("columns", []):
            if c.get("calculated"):
                continue
            if (c.get("dataType") or "").lower() in ("datetime", "date") and date_col is None:
                date_col = c["name"]
            if c["name"].strip().lower() in _MONTH_PARTS:
                month_cols.append(c["name"])
        if date_col:
            for mc in month_cols:
                out[mc] = ("[%s].MONTHLY" % date_col, "Month(%s)" % date_col)
    return out


def chart_type_for(visual_type):
    """Power BI visualType -> (ts_chart, status, note). None ts_chart => skip (slicer/text)."""
    vt = (visual_type or "").strip().lower()
    if vt in _NON_VISUAL:
        return None, "Skipped", f"{visual_type} is not a chart (slicer/text/button)"
    if vt in ("", "unknown", "unparsed"):
        return "GRID_TABLE", "NEEDS REVIEW", "visual type unknown; defaulted to GRID_TABLE"
    if vt in _CHART_MAP:
        ct = _CHART_MAP[vt]
        if ct in ("GEO_BUBBLE", "GEO_AREA"):
            return ct, "Approximated", "needs a geo-recognized column; verify"
        if vt == "gauge":
            return ct, "Approximated", "gauge approximated as KPI"
        return ct, "Migrated", ""
    return "GRID_TABLE", "Approximated", f"no direct mapping for '{visual_type}'; defaulted to GRID_TABLE"


# --------------------------------------------------------------------------- #
# Builders: parsed model -> TML dicts.                                        #
# --------------------------------------------------------------------------- #

def build_table_tml(table, connection_name, connection_fqn, db, schema, warnings,
                    table_map=None, column_map=None, drop_unmapped=False, lower_db_table=False,
                    force_physical=None):
    """Build a Table TML. Returns (tml_dict, [dropped_column_display_names]).

    A name-mapping override binds the logical model to existing physical tables:
      table_map[pbi_table]            -> physical db_table
      column_map[pbi_table][pbi_col]  -> physical db_column_name
    Logical display names stay the Power BI names (the model/answers reference
    those); only the physical db_table / db_column_name are remapped. With
    drop_unmapped, a column absent from the table's column_map is dropped (it has
    no physical backing) and returned so the model can drop it too.

    force_physical maps "Table::Col" -> data_type for calculated columns that are
    used as join keys: joins are physical, so such a column must be emitted as a
    real column (and materialized in the warehouse) instead of becoming a formula."""
    table_map = table_map or {}
    force_physical = force_physical or {}
    cmap = (column_map or {}).get(table["name"], {})
    cols, dropped = [], []
    for c in table.get("columns", []):
        colid = f"{table['name']}::{c['name']}"
        is_calc = c.get("calculated")
        if is_calc and colid not in force_physical:
            continue  # calculated columns become model formulas, not physical columns
        if cmap and drop_unmapped and not is_calc and c["name"] not in cmap:
            dropped.append(c["name"])
            continue
        if is_calc:                       # materialized join-key calc column
            ctype, agg, dtype = "ATTRIBUTE", None, _tml_type(force_physical[colid])
        else:
            ctype, agg = _col_role(c)
            dtype = _tml_type(c.get("dataType"))
        props = {"column_type": ctype}
        if agg:
            props["aggregation"] = agg
        cols.append({
            "name": c["name"],
            "db_column_name": cmap.get(c["name"]) or _dbname(c.get("sourceColumn") or c["name"]),
            "properties": props,
            "db_column_properties": {"data_type": dtype},
        })
    db_table = table_map.get(table["name"])
    if not db_table:
        db_table = _dbname(table["name"])
        if lower_db_table:        # Databricks folds unquoted table names to lowercase
            db_table = db_table.lower()
    tbl = {
        "name": table["name"],
        "db": db,
        "schema": schema,
        "db_table": db_table,
        "connection": {"name": connection_name},
        "columns": cols,
    }
    if connection_fqn:
        tbl["connection"]["fqn"] = connection_fqn
    obj = {"obj_id": f"{_slug(table['name'])}-pbi", "table": tbl}
    return obj, dropped


def _cardinality(rel):
    f = (rel.get("fromCardinality") or "").lower()
    t = (rel.get("toCardinality") or "").lower()
    if f == "one" and t == "one":
        return "ONE_TO_ONE"
    if f == "one" and t == "many":
        return "ONE_TO_MANY"
    if f == "many" and t == "many":
        return "MANY_TO_MANY"
    return "MANY_TO_ONE"  # the common fact -> dimension default


def build_model_tml(model_json, model_name, join_type, overrides, warnings,
                    dropped_ids=None, force_physical=None):
    """Return (model_tml_dict, measure_status_rows, rel_status_rows)."""
    dropped_ids = dropped_ids or set()   # "Table::Col" dropped at the physical layer
    force_physical = force_physical or {}  # "Table::Col" calc cols emitted as physical (join keys)
    tables = model_json.get("tables", [])
    rels = model_json.get("relationships", [])
    table_names = {t["name"] for t in tables}

    # Joins keyed by their source (the "from"/many side) table.
    joins_by_src = {}
    rel_rows = []
    for rel in rels:
        ft, fc = rel.get("fromTable"), rel.get("fromColumn")
        tt, tc = rel.get("toTable"), rel.get("toColumn")
        nm = rel.get("name") or f"{ft}->{tt}"
        if not (ft and tt and fc and tc):
            rel_rows.append({"name": nm, "status": "NEEDS REVIEW",
                             "note": "relationship missing an endpoint column"})
            continue
        if ft not in table_names or tt not in table_names:
            rel_rows.append({"name": nm, "status": "NEEDS REVIEW",
                             "note": "relationship references an unknown table"})
            continue
        if f"{ft}::{fc}" in dropped_ids or f"{tt}::{tc}" in dropped_ids:
            rel_rows.append({"name": nm, "status": "NEEDS REVIEW",
                             "note": "join key column has no physical match (dropped); join skipped"})
            continue
        card = _cardinality(rel)
        joins_by_src.setdefault(ft, []).append({
            "with": tt,
            "on": f"[{ft}::{fc}] = [{tt}::{tc}]",
            "type": join_type,
            "cardinality": card,
        })
        note = f"{join_type}, {card}"
        if (rel.get("crossFilter") or "").lower() in ("both", "bothdirections"):
            note += "; PBI bidirectional cross-filter not modelled (verify)"
        rel_rows.append({"name": nm, "status": "Migrated", "note": note})

    model_tables = []
    for t in tables:
        entry = {"name": t["name"]}
        if t["name"] in joins_by_src:
            entry["joins"] = joins_by_src[t["name"]]
        model_tables.append(entry)

    # Columns: every physical column. ThoughtSpot model column DISPLAY names are
    # case-insensitive and must be unique, but column_id is table-qualified and may
    # repeat a leaf name (Employee::date vs Date::Date). A join references column_id,
    # so we must KEEP every column (dropping one breaks a join) and instead
    # disambiguate the colliding *display name*. column_id is left unchanged.
    seen = set()
    columns = []
    renamed = []
    for t in tables:
        for c in t.get("columns", []):
            colid = f"{t['name']}::{c['name']}"
            if c.get("calculated") and colid not in force_physical:
                continue
            if colid in dropped_ids:
                continue
            disp = c["name"]
            if disp.lower() in seen:
                disp = f"{c['name']} ({t['name']})"
                i = 2
                while disp.lower() in seen:
                    disp = f"{c['name']} ({t['name']} {i})"
                    i += 1
                renamed.append(f"{colid} -> '{disp}'")
            seen.add(disp.lower())
            if c.get("calculated"):       # materialized join-key calc column -> attribute
                ctype, agg = "ATTRIBUTE", None
            else:
                ctype, agg = _col_role(c)
            props = {"column_type": ctype}
            if agg:
                props["aggregation"] = agg
            columns.append({
                "name": disp,
                "column_id": colid,
                "properties": props,
            })
    if renamed:
        warnings.append("Duplicate display names disambiguated (all columns kept, "
                        "column_id unchanged): " + ", ".join(renamed))

    # Formulas: DAX measures + DAX calculated columns.
    ov_measures = {m["name"]: m for m in (overrides.get("measures") or [])}
    formulas = []
    measure_rows = []

    date_cols = {f"{t['name']}::{c['name']}" for t in tables for c in t.get("columns", [])
                 if (c.get("dataType") or "").lower() in ("datetime", "date") and not c.get("calculated")}
    # DAX of every measure + calc column (except materialized join-key calc cols, which
    # are physical now) -> used to inline cross-references so each formula stands alone.
    measure_dax = {me["name"]: me.get("expression", "") for t in tables for me in t.get("measures", [])}
    measure_dax.update({c["name"]: c.get("expression", "") for t in tables for c in t.get("columns", [])
                        if c.get("calculated") and f"{t['name']}::{c['name']}" not in force_physical})

    def add_formula(name, dax, kind, home_table=None, home_cols=None):
        ov = ov_measures.get(name)
        if ov and ov.get("ts_formula"):
            expr, status = ov["ts_formula"], ov.get("status", "Migrated")
            note = ov.get("note", "from overrides")
        else:
            expr, status, note = translate_dax(dax, home_table, home_cols, date_cols, measure_dax)
        if expr and any(f"[{d}]" in expr for d in dropped_ids):
            expr, status = None, "NEEDS REVIEW"
            note = (note + "; " if note else "") + "references a column with no physical match (dropped)"
        row = {"name": name, "original_dax": dax,
               "ts_formula": expr or "", "status": status, "note": note}
        measure_rows.append(row)
        if not expr:
            return  # NEEDS REVIEW: do not emit an invalid formula
        fid = f"formula_{name}"
        formulas.append({"id": fid, "name": name, "expr": expr})
        if name.lower() in seen:        # case-insensitive: ThoughtSpot names must be unique
            return
        seen.add(name.lower())
        # an override may force the column role (e.g. a month-of-year formula used as an
        # axis must be an ATTRIBUTE, not a summable MEASURE)
        ctype = (ov.get("column_type") if ov else None) or ("MEASURE" if kind == "measure" else "ATTRIBUTE")
        col = {"name": name, "formula_id": fid, "properties": {"column_type": ctype}}
        columns.append(col)

    for t in tables:
        # physical columns on this table (non-calc + materialized join-key calc cols);
        # bare DAX refs to these get qualified to [table::col] so renames don't break them.
        hcols = {c["name"] for c in t.get("columns", []) if not c.get("calculated")}
        hcols |= {c["name"] for c in t.get("columns", [])
                  if c.get("calculated") and f"{t['name']}::{c['name']}" in force_physical}
        for m in t.get("measures", []):
            add_formula(m["name"], m.get("expression", ""), "measure", t["name"], hcols)
        for c in t.get("columns", []):
            if c.get("calculated") and f"{t['name']}::{c['name']}" not in force_physical:
                add_formula(c["name"], c.get("expression", ""), "column", t["name"], hcols)

    # Override-only measures: hand-authored formulas (overrides.measures) with no Power BI
    # counterpart -- e.g. parameter-driven SPLY/current-year measures. ts_formula is already
    # fully qualified, so no home table/cols. Added after PBI measures so they can reference
    # them by [formula_<name>].
    existing = {f["name"] for f in formulas}
    for name, ov in ov_measures.items():
        if name not in existing and ov.get("ts_formula"):
            add_formula(name, "", "measure")

    # Cascade NEEDS-REVIEW: a formula that references [formula_X] where X did not
    # translate (flagged, never emitted) would dangle at import. Drop it (and whatever
    # then dangles) so the model imports clean AND the report stays honest -- without
    # this the measure would read "Migrated" but get silently pruned on import.
    row_by_name = {r["name"]: r for r in measure_rows}
    _idref = re.compile(r"\[formula_([^\]]+)\]")
    changed = True
    while changed:
        changed = False
        emitted = {f["name"] for f in formulas}
        for f in list(formulas):
            dangling = sorted({x for x in _idref.findall(f["expr"])
                               if x != f["name"] and x not in emitted})
            if dangling:
                formulas.remove(f)
                columns[:] = [c for c in columns if c.get("formula_id") != f["id"]]
                r = row_by_name.get(f["name"])
                if r:
                    r["status"], r["ts_formula"] = "NEEDS REVIEW", ""
                    r["note"] = ((r["note"] + "; ") if r.get("note") else "") + \
                        "depends on un-migrated measure(s): " + ", ".join(dangling)
                changed = True

    # Order formulas so a formula appears AFTER the formulas it references (by id):
    # ThoughtSpot adds them sequentially, so a forward [formula_X] reference fails.
    # Stable topological sort, cycle-safe.
    fnames = {f["name"] for f in formulas}
    by_name = {f["name"]: f for f in formulas}
    ordered, state = [], {}      # state: 1=visiting, 2=done

    def _visit(f):
        st = state.get(f["name"])
        if st:
            return                                   # done or in a cycle -> skip
        state[f["name"]] = 1
        for g in fnames:
            if g != f["name"] and f"[formula_{g}]" in f["expr"]:
                _visit(by_name[g])
        state[f["name"]] = 2
        ordered.append(f)

    for f in formulas:
        _visit(f)
    formulas = ordered

    model = {
        "obj_id": f"{_slug(model_name)}-pbi",
        "model": {
            "name": model_name,
            "model_tables": model_tables,
            "columns": columns,
        },
    }
    if formulas:
        model["model"]["formulas"] = formulas
    # Parameters (overrides.parameters): typed model-level values a formula can read by
    # name, e.g. a Reference Date that drives parameter-based SPLY/YoY. DATE default_value
    # must be MM/DD/YYYY. They enable time-comparison that has no DAX-to-formula path.
    params = overrides.get("parameters")
    if params:
        model["model"]["parameters"] = params
    return model, measure_rows, rel_rows


def _leaf(field_name):
    """A parsed PBIR field ref ('Sum of Sales', 'Sales.Amount', 'Amount') ->
    a best-effort leaf name for matching against model column display names."""
    if not field_name:
        return ""
    s = str(field_name).strip()
    s = re.sub(r"^(sum|average|avg|min|max|count|count of|sum of|average of)\s+",
               "", s, flags=re.I)
    if "." in s:
        s = s.split(".")[-1]
    return s.strip().strip("[]")


def build_answers_and_liveboards(model_json, model_name, model_fqn, column_names,
                                 measure_names, overrides, warnings, agg_measures=None):
    """Return (answers, liveboards, visual_rows, page_rows).

    agg_measures maps (agg_word, column_name_lower) -> a model measure name, so a
    Power BI inline aggregation (e.g. Sum(BadHires)) binds to the equivalent measure
    ("Sum of BadHires") instead of the bare calc column (which can't be summed)."""
    ov_visuals = {(v.get("page"), v.get("visual")): v
                  for v in (overrides.get("visuals") or [])}
    agg_measures = agg_measures or {}
    answers, liveboards, visual_rows, page_rows, tabs = [], [], [], [], []
    norm = {n.lower(): n for n in column_names}
    bucket_map = _date_bucket_map(model_json)   # month-name col -> monthly date bucket
    # the report (all its pages) becomes one liveboard; name it after the report
    report_name = (model_json.get("project_name")
                   or re.sub(r"\s*\(PBI\)\s*$|\s+Model\s*$", "", model_name).strip()
                   or model_name)

    for page in model_json.get("pages", []):
        page_name = page.get("name") or page.get("id")
        # A Power BI "Tooltip" page is a hover overlay bound to visuals on other pages,
        # not a navigable page -- it has no ThoughtSpot equivalent and must NOT become a
        # tab. Flag it (and its visuals) rather than presenting a hidden popup as a tab.
        if page.get("tooltip"):
            page_rows.append({"name": page_name, "liveboard": "(none)",
                              "status": "NEEDS REVIEW",
                              "note": "PBI custom tooltip page (hover overlay on other pages' "
                                      "visuals); no ThoughtSpot equivalent; not migrated as a tab"})
            for vi, vis in enumerate(page.get("visuals", [])):
                visual_rows.append({"page": page_name,
                                    "visual": f"{page_name} - {vis.get('type', 'visual')} {vi + 1}",
                                    "ts_chart": "(tooltip)", "status": "NEEDS REVIEW",
                                    "note": "on a PBI tooltip page; renders on hover, not as a tab"})
            continue
        page_answers = []
        for vi, vis in enumerate(page.get("visuals", [])):
            title = f"{page_name} - {vis.get('type', 'visual')} {vi + 1}"
            ov = ov_visuals.get((page_name, title)) or ov_visuals.get((page_name, vis.get("id")))
            # Explicit answer override: the auto-builder can't express some visuals (e.g. a
            # parameter-driven SPLY combo with a date-bucket column). When the override gives
            # `search` + `columns`, emit that answer verbatim instead of matching parsed fields.
            if ov and ov.get("search") and ov.get("columns"):
                name = ov.get("name") or vis.get("title") or title
                a_obj = _answer_tml_explicit(name, title, model_name, model_fqn, ov)
                answers.append(a_obj)
                page_answers.append(a_obj["answer"])
                visual_rows.append({"page": page_name, "visual": title,
                                    "ts_chart": ov.get("ts_chart", "?"),
                                    "status": ov.get("status", "Migrated"),
                                    "note": ov.get("note", "explicit answer override")})
                continue
            ct, status, note = chart_type_for(vis.get("type"))
            if ov and ov.get("ts_chart"):
                ct, status, note = ov["ts_chart"], ov.get("status", "Migrated"), ov.get("note", "from overrides")
            if ct is None:
                visual_rows.append({"page": page_name, "visual": title,
                                    "ts_chart": "(filter)", "status": status, "note": note})
                continue

            # Resolve fields to model columns by leaf-name match, keeping each field's
            # PBIR role (Category/Series/Rows/Columns/Values) so the axis layout is faithful.
            cols, roles, missing = [], [], []
            bucket_tokens = {}

            def _place(col, role):
                if col not in cols:
                    cols.append(col)
                    roles.append(role)

            for f in vis.get("fields", []):
                role = f.get("role") or ""
                if f.get("kind") == "aggregation":   # inline Sum(col) -> the equivalent measure
                    mname = agg_measures.get(((f.get("agg") or "sum"), (f.get("field") or "").lower()))
                    if mname and mname.lower() in norm:
                        _place(norm[mname.lower()], role)
                    else:
                        missing.append(f"{f.get('agg') or 'agg'}({f.get('field')})")
                    continue
                leaf = _leaf(f.get("field"))
                if not leaf:
                    continue
                match = norm.get(leaf.lower())
                if match and match in bucket_map:     # date-month part -> monthly date bucket
                    tok, label = bucket_map[match]
                    _place(label, role)
                    bucket_tokens[label] = tok
                elif match:
                    _place(match, role)
                else:
                    missing.append(leaf)
            if missing:
                note = (note + "; " if note else "") + "unmatched fields: " + ", ".join(sorted(set(missing)))
                if status == "Migrated":
                    status = "Approximated"
            if not cols:
                visual_rows.append({"page": page_name, "visual": title, "ts_chart": ct,
                                    "status": "NEEDS REVIEW",
                                    "note": (note + "; " if note else "") + "no fields matched the model"})
                continue

            # Flag (do NOT downgrade) when the chart type's measure requirement isn't
            # met by the measures that survived translation. The chart keeps its source
            # type so the gap is visible for manual fix, not silently swapped.
            n_meas = sum(1 for c in cols if c in measure_names)
            need = _CHART_NEEDS.get(ct, 0 if ct in ("GRID_TABLE", "PIVOT_TABLE") else 1)
            if n_meas < need:
                status = "NEEDS REVIEW"
                note = ((note + "; ") if note else "") + (
                    f"{ct} needs {need} measure(s) but {n_meas} survived translation "
                    "(the rest are time-intelligence / not migrated); flagged, not downgraded")

            # Meaningful name: explicit PBI title, else auto-generate '<measures> by
            # <attrs>' the way Power BI does, else fall back to the type+index key.
            name = vis.get("title") or _auto_name(cols, measure_names) or title
            a_obj = _answer_tml(name, title, model_name, model_fqn, cols, ct, measure_names,
                                roles, bucket_tokens)
            answers.append(a_obj)
            page_answers.append(a_obj["answer"])   # full answer payload, embedded in the liveboard viz
            visual_rows.append({"page": page_name, "visual": title, "ts_chart": ct,
                                "status": status, "note": note})

        tabs.append((page_name, page_answers))
        page_rows.append({"name": page_name, "liveboard": report_name,
                          "status": "Migrated" if page_answers else "NEEDS REVIEW"})
    # One liveboard, one tab per report page (PBI pageOrder preserved by the parser).
    liveboards = [_liveboard_tml(report_name, tabs)] if any(p for _, p in tabs) else []
    return answers, liveboards, visual_rows, page_rows


def _auto_name(cols, measure_names):
    """Derive a meaningful answer name from the columns the way Power BI auto-titles a
    visual: '<measures> by <attributes>' (e.g. 'New Hires by Month and FPDesc'). Used
    when the PBI visual carries no explicit title. Returns None if it can't form one."""
    measures = [c for c in cols if c in measure_names]
    attrs = [c for c in cols if c not in measure_names]
    if measures and attrs:
        return f"{', '.join(measures)} by {' and '.join(attrs)}"
    return None


def _answer_tml(name, obj_key, model_name, model_fqn, cols, chart_type, measure_names,
                roles=None, bucket_tokens=None):
    roles = roles or [""] * len(cols)
    bucket_tokens = bucket_tokens or {}
    role_of = {c: r for c, r in zip(cols, roles)}
    ys = [c for c in cols if c in measure_names]
    xs = [c for c in cols if c not in measure_names]
    # A monthly-bucketed date column carries its own search token ([Date].MONTHLY);
    # everything else is just [Name].
    search = " ".join(bucket_tokens.get(c, f"[{c}]") for c in cols)

    def by_role(*names):
        return [c for c in xs if role_of.get(c) in names]

    rows_x = by_role("Rows")
    cols_c = by_role("Columns")
    cat_x = by_role("Category", "Axis", "X")
    ser_c = by_role("Series", "Legend", "Group")

    chart = {"type": chart_type,
             "chart_columns": [{"column_id": c} for c in cols]}
    ax = {}
    if chart_type == "KPI":
        ax = {"y": ys or cols}
    elif chart_type in ("PIE",) and len(cols) >= 2:
        xax = (cat_x or rows_x or xs)[:1] or [cols[0]]
        ax = {"x": xax, "y": ys[:1] or [cols[-1]]}
    elif chart_type == "PIVOT_TABLE" and ys:
        # Faithful pivot layout from PBIR roles: rows on the left, columns across the
        # top, measures in the cells. Without axis_configs a pivot renders blank.
        xax = rows_x or cat_x or xs[:1] or [cols[0]]
        cax = cols_c or ser_c or [c for c in xs if c not in xax]
        ax = {"x": xax, "y": ys}
        if cax:
            ax["color"] = cax
    elif chart_type in ("COLUMN", "BAR", "LINE", "AREA", "STACKED_COLUMN",
                        "STACKED_BAR", "LINE_COLUMN", "LINE_STACKED_COLUMN") and len(cols) >= 2:
        xax = (cat_x or rows_x or xs)[:1] or [cols[0]]
        cax = (ser_c or cols_c or [c for c in xs if c not in xax])[:1]
        ax = {"x": xax, "y": ys or [cols[-1]]}
        if cax:
            ax["color"] = cax
    if ax:
        chart["axis_configs"] = [ax]
    tables_ref = {"name": model_name}
    if model_fqn:
        tables_ref = {"id": model_name, "name": model_name, "fqn": model_fqn}
    return {
        "obj_id": f"{_slug(obj_key)}-pbi",
        "answer": {
            "name": name,
            "display_mode": "CHART_MODE",
            "tables": [tables_ref],
            "search_query": search,
            "answer_columns": [{"name": c} for c in cols],
            "table": {"table_columns": [{"column_id": c} for c in cols],
                      "ordered_column_ids": list(cols)},
            "chart": chart,
        },
    }


def _answer_tml_explicit(name, obj_key, model_name, model_fqn, ov):
    """Build an answer verbatim from an override spec: ov['search'] (search_query),
    ov['columns'] (ordered column_ids, may include date-bucket columns like
    MONTH_OF_YEAR(Date)), ov['ts_chart'], and optional ov['axis'] (one axis_config).
    For visuals the field-matching auto-builder can't express.

    Capture-and-replay of manual UI polish: ov['formats'] maps a column name to a TML
    `format` block, and ov['client_state_v2'] is the chart's serialized chart-specific
    config (line/column split, KPI display, etc). Both round-trip through TML import
    (cluster-verified), so a user tunes the viz once and the converter reproduces it."""
    cols = list(ov["columns"])
    fmts = ov.get("formats") or {}
    chart = {"type": ov.get("ts_chart", "GRID_TABLE"),
             "chart_columns": [{"column_id": c} for c in cols]}
    if ov.get("axis"):
        chart["axis_configs"] = [ov["axis"]]
    if ov.get("client_state_v2"):
        chart["client_state_v2"] = ov["client_state_v2"]
    tables_ref = {"name": model_name}
    if model_fqn:
        tables_ref = {"id": model_name, "name": model_name, "fqn": model_fqn}
    return {
        "obj_id": f"{_slug(obj_key)}-pbi",
        "answer": {
            "name": name,
            "display_mode": "CHART_MODE",
            "tables": [tables_ref],
            "search_query": ov["search"],
            "answer_columns": [dict({"name": c}, **({"format": fmts[c]} if c in fmts else {}))
                               for c in cols],
            "table": {"table_columns": [{"column_id": c} for c in cols],
                      "ordered_column_ids": cols},
            "chart": chart,
        },
    }


def _liveboard_tml(name, tabs):
    """Build ONE liveboard whose tabs are the Power BI report's pages.

    `tabs` = [(tab_name, [answer_payload, ...]), ...]. ThoughtSpot models a multi-page
    report as a single liveboard with layout.tabs[]: every viz lives in a flat
    visualizations[] (each embedding its FULL answer def), and each tab's tiles[]
    reference vizzes by id. Tiles are laid out two-per-row on the 12-col grid.
    Tabs with no migrated vizzes are dropped (an empty tab won't render)."""
    viz, tab_layout, n = [], [], 0
    for tab_name, payloads in tabs:
        if not payloads:
            continue
        tiles = []
        for j, a in enumerate(payloads):
            n += 1
            vid = f"Viz_{n}"
            viz.append({"id": vid, "answer": a})
            tiles.append({"visualization_id": vid,
                          "x": (j % 2) * 6, "y": (j // 2) * 8, "width": 6, "height": 8})
        tab_layout.append({"name": tab_name, "tiles": tiles})
    return {"obj_id": f"{_slug(name)}-pbi",
            "liveboard": {"name": name, "visualizations": viz,
                          "layout": {"tabs": tab_layout}}}


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _write(out_dir, fname, obj):
    path = os.path.join(out_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump_yaml(obj))
    return path


def main():
    ap = argparse.ArgumentParser(description="Power BI model.json -> ThoughtSpot TML")
    ap.add_argument("model", help="model.json from parse_pbip.py")
    ap.add_argument("--out", default="tml_out", help="output folder for .tml files")
    ap.add_argument("--connection", default="<CONNECTION_NAME>",
                    help="ThoughtSpot Connection name the tables bind to")
    ap.add_argument("--connection-fqn", default="", help="Connection GUID (optional)")
    ap.add_argument("--db", default="<DATABASE>")
    ap.add_argument("--schema", default="<SCHEMA>")
    ap.add_argument("--model-name", default=None, help="name for the generated Model")
    ap.add_argument("--model-fqn", default="", help="Model GUID to bind answers to (optional)")
    ap.add_argument("--join-type", default="LEFT_OUTER",
                    choices=["LEFT_OUTER", "RIGHT_OUTER", "INNER", "FULL_OUTER"],
                    help="join type for relationships (default LEFT_OUTER keeps fact rows)")
    ap.add_argument("--overrides", default=None,
                    help="overrides.json (Claude's ts_formula / ts_chart decisions)")
    ap.add_argument("--lower-db-table", action="store_true",
                    help="lowercase db_table (Databricks folds unquoted table names to lowercase)")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as f:
        model_json = json.load(f)
    overrides = {}
    if args.overrides:
        with open(args.overrides, encoding="utf-8") as f:
            overrides = json.load(f)

    warnings = list(model_json.get("warnings", []))
    src = os.path.basename(model_json.get("source_folder", "")) or "Power BI project"
    model_name = args.model_name or overrides.get("project_name") or f"{src} Model"

    # Drop Power BI auto date tables (and any relationship touching them) before
    # building anything; record them as Skipped so the report shows what was left out.
    auto_names = {t["name"] for t in model_json.get("tables", []) if _AUTO_TABLE.match(t["name"])}
    table_rows = [{"name": n, "status": "Skipped",
                   "note": "Power BI auto date table (internal); not migrated"}
                  for n in sorted(auto_names)]
    if auto_names:
        model_json["tables"] = [t for t in model_json["tables"] if t["name"] not in auto_names]
        model_json["relationships"] = [
            r for r in model_json.get("relationships", [])
            if r.get("fromTable") not in auto_names and r.get("toTable") not in auto_names]

    # A name-mapping override (overrides.connection / table_map / column_map) binds
    # the logical model to existing physical tables on a real connection.
    conn = overrides.get("connection") or {}
    conn_name = conn.get("name") or args.connection
    conn_fqn = conn.get("fqn") or args.connection_fqn
    db = conn.get("db") or args.db
    schema = conn.get("schema") or args.schema
    table_map = overrides.get("table_map") or {}
    column_map = overrides.get("column_map") or {}
    drop_unmapped = bool(overrides.get("drop_unmapped_columns"))

    # A calculated column used as a JOIN KEY must be physical (joins are physical),
    # so emit it as a real column (type inferred from the column it joins to) and
    # flag that the warehouse must materialize it.
    phys_type, calc_set = {}, set()
    for t in model_json.get("tables", []):
        for c in t.get("columns", []):
            if c.get("calculated"):
                calc_set.add((t["name"], c["name"]))
            else:
                phys_type[(t["name"], c["name"])] = c.get("dataType")
    force_physical = {}
    for rel in model_json.get("relationships", []):
        ends = [(rel.get("fromTable"), rel.get("fromColumn")),
                (rel.get("toTable"), rel.get("toColumn"))]
        for (tbl, col), (otbl, ocol) in (ends, ends[::-1]):
            if (tbl, col) in calc_set:
                dt = phys_type.get((otbl, ocol)) or ("int64" if str(col).lower().endswith("id") else "string")
                force_physical[f"{tbl}::{col}"] = dt
    if force_physical:
        warnings.append("Calculated columns materialized as physical join keys (the warehouse "
                        "must contain them): " + ", ".join(sorted(force_physical)))

    os.makedirs(args.out, exist_ok=True)
    written = []

    # Tables.
    dropped_ids = set()
    for t in model_json.get("tables", []):
        tml, dropped = build_table_tml(t, conn_name, conn_fqn, db, schema, warnings,
                                       table_map, column_map, drop_unmapped, args.lower_db_table,
                                       force_physical)
        for d in dropped:
            dropped_ids.add(f"{t['name']}::{d}")
        written.append(_write(args.out, f"{_slug(t['name'])}.table.tml", tml))
        mapped = t["name"] in table_map
        table_rows.append({"name": t["name"], "status": "Migrated",
                           "note": (f"bound to db_table '{table_map[t['name']]}'" if mapped
                                    else f"db_table guessed as '{_dbname(t['name'])}'; verify")})
    if dropped_ids:
        warnings.append(f"{len(dropped_ids)} column(s) dropped (no physical match in the "
                        "connection): " + ", ".join(sorted(dropped_ids)))

    # Model.
    model_tml, measure_rows, rel_rows = build_model_tml(
        model_json, model_name, args.join_type, overrides, warnings, dropped_ids, force_physical)
    written.append(_write(args.out, f"{_slug(model_name)}.model.tml", model_tml))

    # Answers + Liveboards.
    col_names = [c["name"] for c in model_tml["model"]["columns"]]
    measure_names = {c["name"] for c in model_tml["model"]["columns"]
                     if c.get("properties", {}).get("column_type") == "MEASURE"}
    # Map simple aggregation measures (SUM([col])/AVERAGE([col])/...) so a Power BI
    # inline aggregation Sum(col) binds to the equivalent named measure.
    _simple_agg = re.compile(r"(?i)^\s*(SUM|AVERAGE|AVG|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\s*\(\s*(.+?)\s*\)\s*$")
    agg_measures = {}
    for t in model_json.get("tables", []):
        for me in t.get("measures", []):
            mm = _simple_agg.match(me.get("expression", ""))
            if mm:
                aggw = mm.group(1).lower().replace("avg", "average")
                col = re.sub(r".*[\[.]", "", mm.group(2)).strip(" []")
                if col:
                    agg_measures[(aggw, col.lower())] = me["name"]
    answers, liveboards, visual_rows, page_rows = build_answers_and_liveboards(
        model_json, model_name, args.model_fqn, col_names, measure_names,
        overrides, warnings, agg_measures)
    for a in answers:
        written.append(_write(args.out, f"{_slug(a['answer']['name'])}.answer.tml", a))
    for lb in liveboards:
        written.append(_write(args.out, f"{_slug(lb['liveboard']['name'])}.liveboard.tml", lb))

    # mapping.json for migration_report.py.
    mapping = {
        "project_name": model_name,
        "tables": table_rows,
        "relationships": rel_rows,
        "measures": measure_rows,
        "visuals": visual_rows,
        "pages": page_rows,
    }
    map_path = os.path.join(args.out, "mapping.json")
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)

    # Summary.
    def _count(rows, status):
        return sum(1 for r in rows if r.get("status") == status)
    print(f"Wrote {len(written)} TML file(s) + mapping.json to {args.out}/")
    skipped_tables = _count(table_rows, "Skipped")
    print(f"  tables:        {_count(table_rows, 'Migrated')}"
          f"{f' ({skipped_tables} auto date table(s) skipped)' if skipped_tables else ''}")
    print(f"  model:         1  ({len(model_tml['model'].get('formulas', []))} formula(s))")
    print(f"  answers:       {len(answers)}")
    print(f"  liveboards:    {len(liveboards)}")
    review = _count(measure_rows, "NEEDS REVIEW") + _count(visual_rows, "NEEDS REVIEW")
    approx = _count(measure_rows, "Approximated") + _count(visual_rows, "Approximated")
    print(f"  measures:      {len(measure_rows)} "
          f"(Migrated {_count(measure_rows, 'Migrated')}, "
          f"Approximated {_count(measure_rows, 'Approximated')}, "
          f"NEEDS REVIEW {_count(measure_rows, 'NEEDS REVIEW')})")
    if review or approx:
        print(f"\n  {review} item(s) NEED REVIEW, {approx} approximated - see mapping.json / migration report.")
    print(f"\nNext: python migration_report.py {args.model} --mapping {map_path} --out migration_report.md")


if __name__ == "__main__":
    main()
