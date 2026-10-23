"""Transform an extracted MicroStrategy dashboard into a Power BI build spec.

Stage 2 of the migration workflow. Offline: no MicroStrategy or Power BI calls.
  1. extract_dashboard_metadata.py  -> 2_work/<ID>/1_extract/  migration_spec.json + raw/
  2. transform_to_powerbi.py        -> 2_work/<ID>/2_transform/
       powerbi_build_spec.json  semantic model (M + DAX), report pages/visuals/slicers, validation
       BUILD_PLAN.md            step-by-step runbook for Claude + Power BI MCP
  3. Claude + Power BI MCP builds the model and report from BUILD_PLAN.md and runs the validation.

Usage:
  .venv\\Scripts\\python.exe scripts\\transform_to_powerbi.py <DASHBOARD_ID>
      [--source-type snowflake|postgresql|sqlserver] [--server ..] [--database ..] [--warehouse ..]
Source settings default to PBI_SOURCE_* in .env.
"""
import argparse
import json
import math
import operator
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import sqlglot
from dotenv import load_dotenv
from sqlglot import exp

import mstr_schema
from paths import ENV_FILE, extract_dir, overrides_file, transform_dir

load_dotenv(ENV_FILE)

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# navigate: Power Query steps from the connection to one table ({schema}, {table} are M text literals)
SOURCES = {
    "snowflake": {
        "dialect": "snowflake",
        "parameters": ["SourceServer", "SourceWarehouse", "SourceDatabase"],
        "connect": ["Source = Snowflake.Databases(SourceServer, SourceWarehouse)",
                    'Database = Source{[Name = SourceDatabase, Kind = "Database"]}[Data]'],
        "navigate": ['Schema = Database{{[Name = {schema}, Kind = "Schema"]}}[Data]',
                     "Data = Schema{{[Name = {table}]}}[Data]"],
        "query_target": "Database",
    },
    "postgresql": {
        "dialect": "postgres",
        "parameters": ["SourceServer", "SourceDatabase"],
        "connect": ["Source = PostgreSQL.Database(SourceServer, SourceDatabase)"],
        "navigate": ["Data = Source{{[Schema = {schema}, Item = {table}]}}[Data]"],
        "query_target": "Source",
    },
    "sqlserver": {
        "dialect": "tsql",
        "parameters": ["SourceServer", "SourceDatabase"],
        "connect": ["Source = Sql.Database(SourceServer, SourceDatabase)"],
        "navigate": ["Data = Source{{[Schema = {schema}, Item = {table}]}}[Data]"],
        "query_target": "Source",
    },
}
PARAM_ENV = {"SourceServer": "PBI_SOURCE_SERVER", "SourceDatabase": "PBI_SOURCE_DATABASE",
             "SourceWarehouse": "PBI_SOURCE_WAREHOUSE"}

# MicroStrategy data type (lower-case) -> Power BI / TOM data type
PBI_TYPES = {
    "timestamp": "dateTime", "date": "dateTime", "time": "dateTime", "datetime": "dateTime",
    "double": "double", "real": "double", "float": "double", "numeric": "double",
    "decimal": "double", "bigdecimal": "double", "big_decimal": "double",
    "currency": "double", "money": "double",
    "integer": "int64", "int": "int64", "long": "int64", "short": "int64", "unsigned": "int64",
    "int64": "int64", "int32": "int64", "int16": "int64", "int8": "int64", "bigint": "int64",
    "biginteger": "int64", "smallint": "int64", "tinyint": "int64",
    "utf8char": "string", "char": "string", "varchar": "string", "nchar": "string",
    "nvarchar": "string", "longvarchar": "string", "nlongvarchar": "string", "string": "string",
    "text": "string", "memo": "string", "bool": "boolean", "boolean": "boolean",
    # modeling API (logical tables, attribute forms)
    "fixed_length_string": "string", "variable_length_string": "string", "time_stamp": "dateTime",
}
# Attribute forms MicroStrategy uses for maps, and the Power BI data category of the column.
GEO_FORM_CATEGORIES = {"latitude": "Latitude", "longitude": "Longitude"}
# Word in a geo attribute's name -> Power BI data category of its display column.
GEO_ROLES = {"country": "Country", "state": "StateOrProvince", "province": "StateOrProvince", "city": "City",
             "county": "County", "postal": "PostalCode", "postcode": "PostalCode", "zip": "PostalCode",
             "continent": "Continent", "address": "Address"}
DEFAULT_NAME = re.compile(r"^(chapter|page|layout)\s*\d+$", re.IGNORECASE)
M_TYPES = {"string": "type text", "int64": "Int64.Type", "double": "type number",
           "dateTime": "type datetime", "boolean": "type logical"}

VIZ_TYPE_MAP = {
    "bar_chart": "clusteredColumnChart", "horizontal_bar_chart": "clusteredBarChart",
    "line_chart": "lineChart", "time_series": "lineChart", "area_chart": "areaChart",
    "pie_chart": "pieChart", "ring_chart": "donutChart",
    "bubble_chart": "scatterChart", "scatter_chart": "scatterChart",
    "combo_chart": "lineClusteredColumnComboChart", "dual_axis_chart": "lineClusteredColumnComboChart",
    "histogram": "clusteredColumnChart", "waterfall": "waterfallChart", "funnel": "funnel",
    "gauge": "gauge", "heat_map": "treemap", "map": "map", "geospatial_service": "map",
    "kpi": "card", "key_performance_indicator": "card", "text": "textbox", "image": "image",
}
VIZ_NOTES = {
    "bar_chart": "MicroStrategy bar_chart can be vertical or horizontal and the REST definition "
                 "does not say which. Defaulted to vertical (clusteredColumnChart); use "
                 "clusteredBarChart if the original is horizontal.",
    "heat_map": "MicroStrategy heat map has no exact Power BI equivalent; treemap is the closest.",
    "histogram": "Histogram binning must be recreated with a binned column/group in Power BI.",
}
CHART_TYPES = {"clusteredColumnChart", "clusteredBarChart", "lineChart", "areaChart",
               "waterfallChart", "funnel"}
STACKED = {"clusteredColumnChart": "columnChart", "clusteredBarChart": "barChart"}
# MicroStrategy LegendPosition code -> Power BI legend position (codes seen so far)
LEGEND_POSITIONS = {"13": "BottomCenter"}

SLICER_STYLES = {"combo_box": "Dropdown", "pulldown": "Dropdown", "drop_down": "Dropdown",
                 "check_box": "List", "checkbox": "List", "radio_button": "List", "radio_buttons": "List",
                 "list": "List", "search_box": "List", "slider": "List",
                 "link_bar": "Tile", "button_bar": "Tile", "box": "Tile", "hyper_links": "Tile"}

DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y")
FORMAT_TOKEN = re.compile(r"\[([^\]]*)\]")
FORMAT_CONDITION = re.compile(r"^(>=|<=|<>|>|<|=)\s*(-?\d+(?:\.\d+)?)$")
FORMAT_TESTS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
                "=": operator.eq, "<>": operator.ne}
GENERIC_FORM_NAMES = {"id", "desc", "description", "name", "dsc", "none", "code"}
MATCH_THRESHOLD = 0.5
CANVAS_W, CANVAS_H, GAP = 1280, 720, 10


class Issues(list):
    def add(self, severity: str, where: str, message: str) -> None:
        self.append({"severity": severity, "where": where, "message": message})


# ---------------------------------------------------------------------------
# Small helpers (DAX / M quoting, names, values)
# ---------------------------------------------------------------------------

def dax_table(t: str) -> str:
    return "'" + t.replace("'", "''") + "'"


def dax_col(t: str, c: str) -> str:
    return f"{dax_table(t)}[{c.replace(']', ']]')}]"


def dax_measure(m: str) -> str:
    return f"[{m.replace(']', ']]')}]"


def dax_string(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def m_text(s: str) -> str:
    s = s.replace('"', '""').replace("#(", "#(#)(")
    s = s.replace("\r\n", "\n").replace("\n", "#(lf)").replace("\t", "#(tab)")
    return f'"{s}"'


def m_field(name: str) -> str:
    return '#"' + name.replace('"', '""') + '"'


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokens(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return {t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t}


def similarity(label: str, alias: str) -> float:
    if _norm(label) == _norm(alias):
        return 1.0
    a, b = _tokens(label), _tokens(alias)
    jaccard = len(a & b) / len(a | b) if a | b else 0.0
    seq = SequenceMatcher(None, _norm(label), _norm(alias)).ratio()
    return 0.95 * max(jaccard, seq)


def _unique(name: str, taken: set[str], also: tuple[set[str], ...] = ()) -> str:
    """Return name (or name (2), ...) not present case-insensitively; record it in taken."""
    candidate, i = name, 2
    while candidate.lower() in taken or any(candidate.lower() in s for s in also):
        candidate, i = f"{name} ({i})", i + 1
    taken.add(candidate.lower())
    return candidate


def pbi_type(mstr_type: str | None, default: str, issues=None, where: str = "", what: str = "") -> str:
    known = PBI_TYPES.get((mstr_type or "").lower())
    if known is None and issues is not None:
        issues.add("warning", where, f"Unknown MicroStrategy data type '{mstr_type}' on {what}; "
                                     f"imported as {default}. Check the column type in Power BI.")
    return known or default


def geo_role(attribute_name: str) -> str | None:
    return next((role for word, role in GEO_ROLES.items() if word in _tokens(attribute_name)), None)


def parse_value(text: str, dtype: str):
    if dtype == "dateTime":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).isoformat()
            except ValueError:
                pass
    if dtype in ("int64", "double"):
        try:
            num = float(text)
            return int(num) if dtype == "int64" else num
        except ValueError:
            pass
    return text


def dax_literal(value, dtype: str) -> str:
    if dtype == "dateTime" and isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            lit = f"DATE({dt.year}, {dt.month}, {dt.day})"
            if (dt.hour, dt.minute, dt.second) != (0, 0, 0):
                lit += f" + TIME({dt.hour}, {dt.minute}, {dt.second})"
            return lit
        except ValueError:
            pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    return dax_string(str(value))


def is_row_count(metric: dict) -> bool:
    return metric["name"].lower().startswith("row count - ")


# ---------------------------------------------------------------------------
# SQL -> columns, MicroStrategy objects -> SQL columns
# ---------------------------------------------------------------------------

def parse_select_columns(sql: str, dialect: str) -> list[dict]:
    statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(statements) != 1:
        raise ValueError(f"expected 1 SQL statement, found {len(statements)}")
    tree = statements[0]
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        raise ValueError("no SELECT found")
    cols = []
    for proj in select.expressions:
        if isinstance(proj, exp.Star) or proj.alias_or_name == "*":
            raise ValueError("SELECT * cannot be mapped; list the columns explicitly")
        inner = proj.unalias()
        cols.append({"alias": proj.alias_or_name,
                     "expression": inner.sql(dialect=dialect),
                     "aggregate": type(inner).__name__.upper() if isinstance(inner, exp.AggFunc) else None})
    return cols


def mapping_targets(ds: dict, metrics: list[dict], exact_forms: dict | None) -> list[dict]:
    """MicroStrategy objects that should come from a SQL column, with (label, weight) candidates. With exact_forms
    (schema datasets) every target carries its exact SQL alias ('exact') and is never matched by name."""
    targets = []
    for a in ds["attributes"]:
        for f in a["forms"]:
            labels = [(f["name"], 1.0), (f"{a['name']} {f['name']}", 1.0)]
            if f.get("category") == "ID" or len(a["forms"]) == 1:
                labels.append((a["name"], 0.9))
            target = {"kind": "form", "attr": a, "form": f, "labels": labels}
            if exact_forms is not None:
                target["exact"] = exact_forms.get((a["id"], f["id"]))
            targets.append(target)
    for m in metrics:
        if is_row_count(m) or m.get("formula"):
            continue
        target = {"kind": "metric", "metric": m, "labels": [(m["name"], 1.0)]}
        if exact_forms is not None:
            target["exact"] = m.get("column")
        targets.append(target)
    return targets


def assign_columns(targets: list[dict], sql_cols: list[dict]) -> dict[int, dict]:
    """Exact aliases first, then greedy best-first one-to-one matching of the other targets to SQL columns."""
    assigned, used_cols = {}, set()
    by_alias = {c["alias"].lower(): ci for ci, c in enumerate(sql_cols)}
    for ti, t in enumerate(targets):
        ci = by_alias.get((t.get("exact") or "").lower())
        if ci is not None and ci not in used_cols:
            assigned[ti] = {**sql_cols[ci], "index": ci, "confidence": 1.0}
            used_cols.add(ci)
    pairs = []
    for ti, t in enumerate(targets):
        if "exact" in t:
            continue
        for ci, c in enumerate(sql_cols):
            score = max(similarity(label, c["alias"]) * w for label, w in t["labels"])
            pairs.append((score, ti, ci))
    for score, ti, ci in sorted(pairs, key=lambda p: -p[0]):
        if score < MATCH_THRESHOLD or ti in assigned or ci in used_cols:
            continue
        assigned[ti] = {**sql_cols[ci], "index": ci, "confidence": round(score, 3)}
        used_cols.add(ci)
    return assigned


def form_column_name(attr: dict, form: dict) -> str:
    if len(attr["forms"]) == 1:
        return attr["name"]
    if _norm(form["name"]) in GENERIC_FORM_NAMES:
        # The description is what users see: name it after the attribute ('Area'); other forms 'Area ID'.
        return attr["name"] if form.get("category") == "DESC" else f"{attr['name']} {form['name']}"
    if _norm(attr["name"]) not in _norm(form["name"]):
        return f"{attr['name']} {form['name']}"  # e.g. 'Year Month Short Desc'
    return form["name"]


# ---------------------------------------------------------------------------
# Semantic model
# ---------------------------------------------------------------------------

class Lookups:
    def __init__(self):
        self.ds_table = {}      # dataset id -> table name (star schema: its main fact table)
        self.home = {}          # (ds, attr id[, form id]) or (ds, metric id) -> table, when not ds_table (star schema)
        self.form_col = {}      # (ds, attr id, form id) -> column
        self.attr_display = {}  # (ds, attr id) -> column
        self.measure = {}       # (ds, metric id) -> measure
        self.derived = {}       # metric id -> {table, measure} for metrics created in the dashboard
        self.linked = {}        # attribute id -> {table, display, byForm} shared table of a linked attribute
        self.related = {}       # shared table or dimension -> tables it filters
        self.col_type = {}      # (table, column) -> pbi type
        self.report_filters = []  # cube filters of schema cubes -> report-level filters

    def table_of(self, key: tuple) -> str:
        """Table holding a dataset's attribute form, attribute or metric."""
        return self.home.get(key) or self.ds_table[key[0]]


def _format_sections(fs: str) -> list[str]:
    sections, current, quoted = [], "", False
    for ch in fs:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            sections.append(current)
            current = ""
        else:
            current += ch
    return sections + [current]


def move_scaling_commas(section: str) -> str:
    """Put scaling commas (each one divides by 1000) where Power BI's visuals look for them.

    Visuals only find them right before the decimal point, or at the very end of the format: with
    MicroStrategy's '#,##0,,"M"' they show 16424327305M. '#,##0"M",,' and '#,##0,,.0"M"' work in the visuals and
    in the engine (FORMAT)."""
    tokens = re.findall(r'"[^"]*"|\\.|.', section)
    code = [t if len(t) == 1 else "" for t in tokens]  # quoted and escaped literals don't count
    dot = code.index(".") if "." in code else len(code)
    digits = [i for i, c in enumerate(code) if c in ("0", "#")]
    last_whole = max((i for i in digits if i < dot), default=-1)
    last_any = max(digits, default=-1)
    scaling = [i for i, c in enumerate(code) if c == "," and (last_whole < i < dot or i > last_any > dot)]
    if not scaling or last_whole < 0:
        return section
    kept = [t for i, t in enumerate(tokens) if i not in scaling]
    commas = "," * len(scaling)
    if dot < len(code):
        at = kept.index(".")
        return "".join(kept[:at]) + commas + "".join(kept[at:])
    return "".join(kept) + commas


def pbi_format(fs: str, max_abs: float | None) -> tuple[str, str | None]:
    """MicroStrategy (Excel-style) number format -> Power BI format string, plus a note when simplified.

    Power BI has no conditional sections like [>=1000000]: keep the section that fits the largest value the
    dashboard showed. Colour tokens ([Red]) are dropped and [$€-407] becomes its currency symbol.
    """
    parsed = []
    for section in _format_sections(fs):
        condition = None

        def token(m: re.Match) -> str:
            nonlocal condition
            inner = m.group(1).strip()
            if (c := FORMAT_CONDITION.match(inner)):
                condition = (c.group(1), float(c.group(2)))
                return ""
            return inner[1:].split("-")[0] if inner.startswith("$") else ""

        text = move_scaling_commas(FORMAT_TOKEN.sub(token, section))  # sub() sets condition
        parsed.append((condition, text))
    if not any(c for c, _ in parsed):
        return ";".join(f for _, f in parsed[:3]), None
    value = abs(max_abs or 0)
    chosen = (next((f for c, f in parsed if c and FORMAT_TESTS[c[0]](value, c[1])), None)
              or next((f for c, f in parsed if not c), parsed[0][1]))
    reach = f" (values on the dashboard reach {value:,.0f})" if max_abs is not None else ""
    return chosen, (f"MicroStrategy format '{fs}' switches K/M by value size; a Power BI format string can't. "
                    f"Used '{chosen}'{reach}. Use a dynamic format string if values vary in scale.")


def collect_metric_formats(spec: dict) -> dict[str, tuple[str, float | None]]:
    """Metric id -> (MicroStrategy format string, largest absolute value shown on a visual)."""
    formats = {}
    for ch in spec["chapters"]:
        for pg in ch["pages"]:
            for v in pg["visualizations"]:
                for m in v["metrics"]:
                    fs = (m.get("numberFormat") or {}).get("formatString")
                    if fs and fs.lower() != "general":
                        first_fs, seen = formats.get(m["id"], (fs, None))
                        values = [abs(x) for x in (m.get("min"), m.get("max"), seen) if isinstance(x, (int, float))]
                        formats[m["id"]] = (first_fs, max(values, default=None))
    return formats


def metrics_on_visuals(spec: dict) -> set[str]:
    return {m["id"] for ch in spec["chapters"] for pg in ch["pages"]
            for v in pg["visualizations"] for m in v["metrics"]}


def build_m_expression(source: dict, sql: str, rename: dict[str, str],
                       types: list[tuple[str, str]]) -> str:
    steps = list(source["connect"])
    steps.append(f"Query = Value.NativeQuery({source['query_target']}, {m_text(sql)})")
    # Case-insensitive rename: Snowflake upper-cases and PostgreSQL lower-cases unquoted aliases.
    record = ", ".join(f"{m_field(k)} = {m_text(v)}" for k, v in rename.items())
    steps.append(f"RenameMap = [{record}]")
    steps.append("Renamed = Table.TransformColumnNames(Query, (c) => "
                 "Record.FieldOrDefault(RenameMap, Text.Lower(c), c))")
    type_list = ", ".join(f"{{{m_text(c)}, {t}}}" for c, t in types)
    steps.append(f"Typed = Table.TransformColumnTypes(Renamed, {{{type_list}}})")
    return "let\n" + ",\n".join(f"    {s}" for s in steps) + "\nin\n    Typed"


def clean_server(value: str) -> str:
    """Host name as the Power BI connectors expect it: drops a pasted https:// or /path and doubled dots."""
    host = re.sub(r"^[a-z]+://", "", value, flags=re.I).split("/")[0]
    return re.sub(r"\.{2,}", ".", host).strip(".")


DAX_AGGREGATION = {"sum": "SUM", "count": "SUM", "max": "MAX", "min": "MIN", "avg": "AVERAGE"}


def prepare_schema_dataset(ds: dict, dialect: str, issues: Issues, where: str) -> tuple[str, dict, list[dict]]:
    """Schema cube: (one-statement SQL, {(attribute id, form id): alias}, metrics to build).

    Metrics: the dataset's own plus the base metrics its compound metrics use (hidden). Base metrics get the SQL
    alias that computes them; compound metrics their formula."""
    schema, sql = ds["schema"], ds["sourceSql"]
    lineage = mstr_schema.column_lineage(sql, dialect)
    base = [{"id": mid, "aggregation": m["aggregation"], "factExpressions": m["factExpressions"],
             "factTables": set(m["factTables"]), "transformationColumns": set(m["transformationColumns"]),
             "levelColumns": set(m["levelColumns"])}
            for mid, m in schema["metrics"].items() if m["kind"] == "base"]
    columns = mstr_schema.match_metric_columns(lineage, base, dialect)
    forms = {}
    for a in ds["attributes"]:
        for f in a["forms"]:
            exprs = [e.lower() for e in schema["attributeForms"].get(a["id"], {}).get(f["id"], [])]
            forms[(a["id"], f["id"])] = next((e for e in exprs if e in lineage), None)
    own = {m["id"]: m for m in ds["metrics"]}
    metrics = []
    for mid, m in schema["metrics"].items():
        entry = {"id": mid, "name": m["name"], "dataType": own.get(mid, {}).get("dataType") or "double",
                 "hidden": mid not in own}
        if m["kind"] == "base":
            alias = columns.get(mid)
            entry.update(column=alias, aggregation=m["aggregation"], levelColumns=m["levelColumns"],
                         sqlPass=(lineage.get(alias) or {}).get("pass"))
            if not alias:
                issues.add("error", where, f"Base metric '{m['name']}' ({m['aggregation']} of "
                                           f"{m['factExpressions']}) not found in the cube SQL; write its DAX manually.")
        elif m["kind"] == "compound":
            entry["formula"] = m["formula"]
        else:
            issues.add("error", where, f"Metric '{m['name']}' uses functions the migration does not rebuild; "
                                       "write its DAX manually.")
        metrics.append(entry)
    passes = len(mstr_schema.split_passes(sql)[0])
    issues.add("info", where, f"Cube built on the project schema: {passes} SQL pass(es) combined into one query "
                              f"(WITH clauses); {len(columns)} metric column(s) mapped through the metric, fact and "
                              "transformation definitions.")
    return mstr_schema.single_query(sql), forms, metrics


# ---------------------------------------------------------------------------
# Schema cubes as a star schema: fact and dimension tables from the cube SQL's joins
# ---------------------------------------------------------------------------

DAX_AGGREGATORS = {"sum": ("SUM", "SUMX"), "avg": ("AVERAGE", "AVERAGEX"), "min": ("MIN", "MINX"),
                   "max": ("MAX", "MAXX"), "count": ("COUNT", "COUNTX"), "distinct count": ("DISTINCTCOUNT", None)}
DAX_OPERATORS = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=",
                 exp.LT: "<", exp.LTE: "<=", exp.And: "&&"}


def sql_to_dax(node: exp.Expression, column) -> str:
    """Row-level SQL expression (a fact's) as DAX; column(name) gives a column's DAX reference.
    Raises ValueError for SQL it does not translate."""
    def t(n):
        return sql_to_dax(n, column)
    if isinstance(node, exp.Column):
        return column(node.name)
    if isinstance(node, exp.Literal):
        return dax_string(node.this) if node.is_string else node.this
    if isinstance(node, exp.Paren):
        return f"({t(node.this)})"
    if isinstance(node, exp.Null):
        return "BLANK()"
    if isinstance(node, exp.Boolean):
        return "TRUE()" if node.this else "FALSE()"
    if isinstance(node, exp.Neg):
        return f"-{t(node.this)}"
    if isinstance(node, exp.Div):
        return f"DIVIDE({t(node.left)}, {t(node.right)})"  # SQL fails on division by zero, DAX DIVIDE gives blank
    if type(node) in DAX_OPERATORS:
        return f"{t(node.left)} {DAX_OPERATORS[type(node)]} {t(node.right)}"
    if isinstance(node, exp.Or):
        return f"({t(node.left)} || {t(node.right)})"
    if isinstance(node, exp.Not):
        return f"NOT ({t(node.this)})"
    if isinstance(node, exp.In) and node.expressions and not node.args.get("query"):
        return f"{t(node.this)} IN {{{', '.join(t(e) for e in node.expressions)}}}"
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        return f"ISBLANK({t(node.this)})"
    if isinstance(node, exp.Case) and not node.this and len(node.args.get("ifs") or []) == 1:
        when = node.args["ifs"][0]
        default = [t(node.args["default"])] if node.args.get("default") else []
        return f"IF({', '.join([t(when.this), t(when.args['true'])] + default)})"
    if isinstance(node, exp.Case):
        parts = [t(node.this) if node.this else "TRUE()"]
        parts += [x for i in node.args.get("ifs") or [] for x in (t(i.this), t(i.args["true"]))]
        if node.args.get("default"):
            parts.append(t(node.args["default"]))
        return f"SWITCH({', '.join(parts)})"
    if isinstance(node, exp.If):
        args = [node.this, node.args["true"]] + ([node.args["false"]] if node.args.get("false") else [])
        return f"IF({', '.join(t(a) for a in args)})"
    if isinstance(node, exp.Coalesce):
        return f"COALESCE({', '.join(t(a) for a in [node.this, *node.expressions])})"
    if isinstance(node, exp.Abs):
        return f"ABS({t(node.this)})"
    if isinstance(node, exp.Cast):
        return t(node.this)
    raise ValueError(node.sql())


def star_keys(meta: dict, attributes: dict) -> dict[str, str]:
    """Lookup table -> its key column: the ID column of the table's one key attribute (fact tables have several)."""
    keys = {}
    for table, info in meta.items():
        if len(info["keyAttributeIds"]) != 1:
            continue
        forms = (attributes.get(info["keyAttributeIds"][0]) or {}).get("forms", [])
        ids = [f["expressions"].get(table) for f in forms if f.get("category") == "ID"]
        if len(ids) == 1 and ids[0] and re.fullmatch(r"\w+", ids[0]):
            keys[table] = ids[0].lower()
    return keys


class Star:
    """Fact and dimension tables of the dashboard's schema cubes: one per database table, shared by the datasets
    that read it, so a dimension filters every fact it joins."""

    def __init__(self, source: dict):
        self.source = source
        self.tables = {}      # database table (lower case) -> model table
        self.edges = set()    # (table, column, lookup table, key column), database names: the relationships
        self.form_col = {}    # (attribute id, form id) -> (model table, column)
        self.measure_of = {}  # metric id -> (model table, measure)
        self.filters = {}     # (database table, column) -> values of a cube filter

    def column(self, db: str, column: str, name: str | None = None, hidden: bool = True,
               dtype: str | None = None, mstr: dict | None = None, computed: str | None = None) -> str:
        """Model name of a database column of a star table, adding the column on first use."""
        table = self.tables[db]
        for c in table["columns"]:
            if c["dbColumn"].lower() == column.lower() and not computed:
                c["isHidden"] = c["isHidden"] and hidden
                return c["name"]
        info = table["db"]["columns"].get(column.lower(), {})
        cname = _unique(name or info.get("name") or column, {c["name"].lower() for c in table["columns"]})
        table["columns"].append({"name": cname, "sourceColumn": cname, "dbColumn": info.get("name") or column,
                                 "computed": computed, "dataType": dtype or pbi_type(info.get("dataType"), "string"),
                                 "summarizeBy": "none", "isHidden": hidden, "mstr": mstr or {}, "sqlAlias": None})
        return cname

    def ancestors(self, db: str) -> list[str]:
        """Tables a table joins to (its dimensions, theirs, ...)."""
        out, todo = [], [db]
        while todo:
            t = todo.pop()
            for t1, _, t2, _ in self.edges:
                if t1 == t and t2 not in out:
                    out.append(t2)
                    todo.append(t2)
        return out


def _star_table(star: Star, db: str, meta: dict, attributes: dict, fact: bool, tables: list[dict],
                taken_tables: set[str], ds: dict) -> dict:
    if db in star.tables:
        table = star.tables[db]
    else:
        info = meta[db]
        key_attr = (attributes.get(info["keyAttributeIds"][0]) or {}).get("name") if info["keyAttributeIds"] else None
        name = f"Fact {db}" if fact else f"Dim {key_attr or db}"
        where = f"{info['namespace']}.{info['tableName']}" if info.get("namespace") else info["tableName"]
        table = {"name": _unique(name, taken_tables), "kind": "star", "role": "fact" if fact else "dimension",
                 "mstrDatasetIds": [], "db": info, "columns": [], "measures": [], "partition": None,
                 "description": (f"Fact table {where}, aggregated by the MicroStrategy cube SQL" if fact else
                                 f"Dimension {where}: MicroStrategy lookup table of attribute {key_attr or '?'}")}
        star.tables[db] = table
        tables.append(table)
    if ds["id"] not in table["mstrDatasetIds"]:
        table["mstrDatasetIds"].append(ds["id"])
    return table


def add_star_dataset(ds: dict, star: Star, lk: Lookups, tables: list[dict], taken_tables: set[str],
                     taken_measures: set[str], formats: dict, issues: Issues) -> bool:
    """Schema cube -> fact and dimension tables (shared through star), relationships from the SQL joins, and measures
    over the fact rows. False when the SQL has no fact to build on (the caller imports the cube SQL as one table)."""
    where = f"dataset '{ds['name']}'"
    schema, sql, dialect = ds["schema"], ds["sourceSql"], star.source["dialect"]
    meta, attributes = schema["tables"], schema["attributes"]
    keys = star_keys(meta, attributes)
    base = [{"id": mid, "aggregation": m["aggregation"], "factExpressions": m["factExpressions"],
             "factTables": set(m["factTables"]), "transformationColumns": set(m["transformationColumns"]),
             "levelColumns": set(m["levelColumns"])}
            for mid, m in schema["metrics"].items() if m["kind"] == "base"]
    shifting = set().union(*(m["transformationColumns"] for m in base)) if base else set()
    aliases = mstr_schema.match_metric_columns(mstr_schema.column_lineage(sql, dialect), base, dialect)
    layout = mstr_schema.star_layout(sql, dialect, keys, shifting)
    sources = {mid: layout["measures"][a] for mid, a in aliases.items()
               if a in layout["measures"] and layout["measures"][a]["table"] in meta}
    facts = list(dict.fromkeys(s["table"] for s in sources.values()))
    if not facts:
        issues.add("warning", where, "No fact table found in the cube SQL; imported as one table instead of a star "
                                     "schema.")
        return False

    graph = {}
    for e in layout["edges"]:
        graph.setdefault(e["from"][0], []).append((*e["from"], *e["to"]))
    needed, used = set(facts), set()

    def reach(goal: str) -> bool:
        """Add the shortest chain of joins from a fact to goal."""
        best = None
        for start in facts:
            seen, todo = {start: []}, [start]
            while todo and goal not in seen:
                t = todo.pop(0)
                for edge in graph.get(t, []):
                    if edge[2] not in seen:
                        seen[edge[2]] = seen[t] + [edge]
                        todo.append(edge[2])
            if goal in seen and (best is None or len(seen[goal]) < len(best)):
                best = seen[goal]
        if best is None:
            return False
        used.update(best)
        needed.update(e[2] for e in best)
        return True

    # Dimensions: each attribute's lookup table, with the tables joining it to a fact (snowflake)
    for a in ds["attributes"]:
        lookup = (attributes.get(a["id"]) or {}).get("lookupTable")
        if lookup in needed or (lookup in meta and reach(lookup)):
            continue
        if lookup in meta:
            needed.add(lookup)
            issues.add("warning", where, f"Attribute '{a['name']}': the cube SQL does not join its table {lookup} to "
                                         "a fact table, so it filters no measure. Relate it manually.")
        else:
            issues.add("error", where, f"Attribute '{a['name']}': lookup table {lookup} is not in the cube SQL; add "
                                       "its column manually.")
    for s in sources.values():
        if s["shift"] and not (s["shift"]["table"] in needed or reach(s["shift"]["table"])):
            issues.add("error", where, f"Transformation table {s['shift']['table']} is not joined to a fact table.")

    # Cube filter (the WHERE of every pass) -> report-level filter on a dimension column
    for f in {json.dumps(f, sort_keys=True): f for s in sources.values() for f in s["filters"]}.values():
        if "sql" in f:
            issues.add("warning", where, f"Cube filter `{f['sql']}` not converted; add it as a report filter.")
            continue
        holder = next((t for t in sorted(needed, key=lambda t: (t in facts, t))
                       if f["column"] in meta.get(t, {}).get("columns", {})), None)
        if holder is None and f["table"] in meta and reach(f["table"]):
            holder = f["table"]
        if holder is None:
            issues.add("warning", where, f"Cube filter {f['column']} in {f['values']} not converted: no table of the "
                                         "model has that column. Add it as a report filter.")
            continue
        star.filters[(holder, f["column"])] = f["values"]

    # Tables and columns
    for db in sorted(needed, key=lambda t: (t not in facts, t)):
        table = _star_table(star, db, meta, attributes, db in facts, tables, taken_tables, ds)
        for aid, attr in attributes.items():
            if attr.get("lookupTable") != db:
                continue
            ids = []
            for f in attr["forms"]:
                col = f["expressions"].get(db) or ""
                if not re.fullmatch(r"\w+", col) or (table["db"]["columns"] and col.lower() not in table["db"]["columns"]):
                    continue
                category = GEO_FORM_CATEGORIES.get((f.get("category") or "").lower())
                cname = star.column(db, col, form_column_name(attr, f), hidden=False,
                                    mstr={"attribute": attr["name"], "attributeId": aid, "form": f["name"],
                                          "formCategory": f.get("category"), "dataType": f.get("dataType")})
                if category:
                    next(c for c in table["columns"] if c["name"] == cname)["dataCategory"] = category
                star.form_col[(aid, f["id"])] = (table["name"], cname)
                ids += [cname] if f.get("category") == "ID" else []
            # MicroStrategy lists elements in ID order: the other forms sort by the ID
            for c in table["columns"]:
                if ids and c["mstr"].get("attributeId") == aid and c["name"] != ids[0]:
                    c["sortByColumn"] = ids[0]
        if db in keys:
            star.column(db, keys[db])
    for t1, c1, t2, c2 in used:
        star.column(t1, c1)
        star.column(t2, c2)
        star.edges.add((t1, c1, t2, c2))
    for (db, col) in [k for k in star.filters if k[0] in needed]:
        attr = next((a for a in attributes.values() for f in a["forms"]
                     if f.get("category") == "ID" and (f["expressions"].get(db) or "").lower() == col), None)
        star.column(db, col, attr["name"] if attr else None, hidden=False)

    # Attribute forms of the dataset -> their dimension columns
    for a in ds["attributes"]:
        for f in a["forms"]:
            hit = star.form_col.get((a["id"], f["id"]))
            if hit:
                lk.home[(ds["id"], a["id"], f["id"])] = hit[0]
                lk.form_col[(ds["id"], a["id"], f["id"])] = hit[1]
            elif not GEO_FORM_CATEGORIES.get((f.get("category") or "").lower()):
                issues.add("error", where, f"Attribute form '{a['name']}' / '{f['name']}' has no column in the star "
                                           "schema; add it manually.")
        mapped = [(f, lk.form_col.get((ds["id"], a["id"], f["id"]))) for f in a["forms"]]
        mapped = [(f, c) for f, c in mapped if c]
        if mapped:
            desc = [(f, c) for f, c in mapped if f.get("category") == "DESC"] or mapped
            lk.attr_display[(ds["id"], a["id"])] = desc[0][1]
            lk.home[(ds["id"], a["id"])] = lk.home[(ds["id"], a["id"], desc[0][0]["id"])]
            if any((g.get("category") or "").lower() in GEO_FORM_CATEGORIES for g in a["forms"]) \
                    and (role := geo_role(a["name"])):
                table = next(t for t in star.tables.values() if t["name"] == lk.home[(ds["id"], a["id"])])
                next(c for c in table["columns"] if c["name"] == desc[0][1])["dataCategory"] = role

    # Measures: base metrics over the fact rows, then compound metrics from their formulas
    lk.ds_table[ds["id"]] = star.tables[facts[0]]["name"]
    taken_cols = {c["name"].lower() for t in star.tables.values() for c in t["columns"]}
    own = {m["id"] for m in ds["metrics"]}
    compound = []

    def add_measure(mid: str, m: dict, table: dict, expression: str, note: str) -> None:
        mname = _unique(f"{m['name']} (Metric)" if m["name"].lower() in taken_cols else m["name"], taken_measures)
        fmt, simplified = pbi_format(*formats[mid]) if mid in formats else (None, None)
        if simplified:
            issues.add("info", where, f"Measure '{mname}': {simplified}")
        table["measures"].append({"name": mname, "expression": expression, "formatString": fmt, "note": note,
                                  "isHidden": mid not in own,
                                  "mstr": {"metric": m["name"], "metricId": mid,
                                           "formatString": formats.get(mid, (None,))[0]}})
        star.measure_of[mid] = (table["name"], mname)

    for mid, m in schema["metrics"].items():
        if mid in star.measure_of:
            continue  # built for another dataset reading the same tables
        if m["kind"] == "compound":
            compound.append((mid, m))
        elif m["kind"] == "base" and mid in sources:
            add_measure(mid, m, star.tables[sources[mid]["table"]], *star_measure(sources[mid], m, star, issues, where))
        else:
            add_measure(mid, m, star.tables[facts[0]], "BLANK()", "TODO: not rebuilt from the cube SQL.")
            issues.add("error", where, f"Metric '{m['name']}' ({m['kind']}) was not found in the cube SQL or uses "
                                       "functions the migration does not rebuild; write its DAX manually.")
    while compound:
        ready = [(mid, m) for mid, m in compound if all(i in star.measure_of for i in _formula_metrics(m["formula"]))]
        for mid, m in ready or compound[:1]:
            compound.remove((mid, m))
            if ready:
                home = star.measure_of[_formula_metrics(m["formula"])[0]][0]
                table = next(t for t in star.tables.values() if t["name"] == home)
                add_measure(mid, m, table, dax_formula(m["formula"], star.measure_of),
                            "MicroStrategy compound metric, rebuilt from its formula.")
            else:
                add_measure(mid, m, star.tables[facts[0]], "BLANK()", "TODO: formula uses metrics not rebuilt.")
                issues.add("error", where, f"Compound metric '{m['name']}' uses metrics that were not rebuilt; "
                                           "write its DAX manually.")
    for mid in schema["metrics"]:
        lk.home[(ds["id"], mid)], lk.measure[(ds["id"], mid)] = star.measure_of[mid]

    unused = sorted({a["name"] for a in attributes.values() if a.get("lookupTable") in meta
                     and a.get("lookupTable") not in needed})
    issues.add("info", where, f"Star schema from the cube SQL ({len(mstr_schema.split_passes(sql)[0])} pass(es)): "
                              f"fact table(s) {', '.join(facts)}; {len(needed) - len(facts)} dimension table(s) joined "
                              f"as in the SQL; {len(used)} relationship(s). Fact rows are imported, not the cube's "
                              f"aggregates: the measures aggregate them as MicroStrategy's metrics do."
                              + (f" Cube attributes not used by this dataset, left out: {', '.join(unused)}."
                                 if unused else ""))
    skipped = [o for o in layout["otherJoins"] if not {o["left"][0], o["right"][0]} - needed - set(facts)]
    for o in skipped:
        issues.add("info", where, f"SQL join {'.'.join(o['left'])} = {'.'.join(o['right'])} is not on a lookup "
                                  "table's key; no relationship (MicroStrategy uses it for the cube filter or "
                                  "to combine passes).")
    return True


def star_measure(src: dict, metric: dict, star: Star, issues: Issues, where: str) -> tuple[str, str]:
    """DAX of a base metric over its fact table's rows: the aggregation of the fact's SQL expression, shifted
    in time for a transformation (LY, LM...), computed per level for a level metric."""
    fact = star.tables[src["table"]]
    simple, iterator = DAX_AGGREGATORS[src["aggregation"]]

    def col(name: str) -> str:
        return dax_col(fact["name"], star.column(src["table"], name))

    node = sqlglot.parse_one(src["expression"], read=star.source["dialect"])
    while isinstance(node, exp.Paren):
        node = node.this
    fact_name = metric.get("factName") or metric["name"]
    note = (f"MicroStrategy base metric: {src['aggregation']} of fact '{fact_name}' = {src['expression']} "
            f"on {src['table']}.")
    if isinstance(node, exp.Column):
        body = f"{simple}({col(node.name)})"
    else:
        try:
            if not iterator:
                raise ValueError(src["aggregation"])
            body = f"{iterator}({dax_table(fact['name'])}, {sql_to_dax(node, col)})"
        except ValueError:
            # Not translatable to DAX: computed in the fact table's SQL query instead
            cname = star.column(src["table"], f"{fact_name} (fact)", f"{fact_name} (fact)", dtype="double",
                                computed=src["expression"])
            body = f"{simple}({dax_col(fact['name'], cname)})"
            note += f" Computed in the SQL of the fact table as column '{cname}'."
    if src["shift"]:
        s = src["shift"]
        dim = star.tables.get(s["table"])
        if dim:
            shifted = dax_col(dim["name"], star.column(s["table"], s["column"]))
            removes = [dim["name"]] + [star.tables[t]["name"] for t in star.ancestors(s["table"]) if t in star.tables]
            body = ("CALCULATE(\n    " + body + ",\n    "
                    + ",\n    ".join(f"REMOVEFILTERS({dax_table(n)})" for n in removes)
                    + f",\n    TREATAS(VALUES({shifted}), {col(s['factColumn'])})\n)")
            note += (f" MicroStrategy transformation: joins {src['table']}.{s['factColumn']} to {s['table']}."
                     f"{s['column']}, rebuilt as the periods in {shifted} of the periods in context.")
    if metric.get("levelColumns"):
        keys = [c for c in metric["levelColumns"] if c in fact["db"]["columns"]]
        if len(keys) == len(metric["levelColumns"]):
            over = ", ".join(col(k) for k in keys)
            body = (f"SUMX(\n    SUMMARIZE({dax_table(fact['name'])}, {over}),\n    "
                    f"CALCULATE({body}, ALLEXCEPT({dax_table(fact['name'])}, {over}))\n)")
        issues.add("warning", where, f"'{metric['name']}' is a level metric ({', '.join(metric['levelColumns'])}); "
                                     "computed per level value and summed. Check totals against MicroStrategy.")
    return body, note


def finish_star(star: Star, lk: Lookups, issues: Issues) -> list[dict]:
    """Power Query partitions, column types, filter relations and report filters of the star tables; returns the
    relationships."""
    for db, table in star.tables.items():
        table["partition"] = {"name": table["name"], "type": "M"}
        table["partition"]["expression"], table["nativeQuery"] = star_partition(star.source, table)
        for c in table["columns"]:
            lk.col_type[(table["name"], c["name"])] = c["dataType"]
    names = {db: t["name"] for db, t in star.tables.items()}
    for db in star.tables:
        downstream = {t1 for t1 in star.tables if db in star.ancestors(t1)}
        lk.related.setdefault(names[db], set()).update(names[t] for t in downstream)
    for (db, col), values in star.filters.items():
        table = star.tables[db]
        cname = next(c["name"] for c in table["columns"] if c["dbColumn"].lower() == col)
        dtype = lk.col_type[(table["name"], cname)]
        lk.report_filters.append({"id": f"cube|{db}|{col}", "title": cname,
                                  "field": {"table": table["name"], "column": cname, "mstrId": None},
                                  "dataType": dtype, "singleSelect": False,
                                  "defaultSelection": {"mode": "include",
                                                       "values": [parse_value(str(v), dtype) for v in values]}})
        issues.add("info", "cube filter", f"MicroStrategy cube filter {col} in {values} -> report-level filter on "
                                          f"{table['name']}[{cname}]. The fact tables load every period, so the "
                                          "LY/LM measures can reach the periods before the filter.")
    def col_name(db: str, column: str) -> str:
        return next(c["name"] for c in star.tables[db]["columns"] if c["dbColumn"].lower() == column)

    return [{"fromTable": names[t1], "fromColumn": col_name(t1, c1), "toTable": names[t2],
             "toColumn": col_name(t2, c2), "sqlJoin": f"{t1}.{c1} = {t2}.{c2}"} for t1, c1, t2, c2 in sorted(star.edges)]


def star_partition(source: dict, table: dict) -> tuple[str, bool]:
    """Power Query of a star table and whether it is native SQL. Navigation to the database table folds to SQL and
    needs no native-query approval; native SQL is used when the schema is unknown, the MicroStrategy table is a
    logical view, or a fact expression could not be written in DAX."""
    db, cols = table["db"], table["columns"]
    native = any(c["computed"] for c in cols) or not db.get("namespace") or db.get("type") not in (None, "normal")
    steps = list(source["connect"])
    if not native:
        steps += [s.format(schema=m_text(db["namespace"]), table=m_text(db["tableName"])) for s in source["navigate"]]
        steps.append("Columns = Table.SelectColumns(Data, {" + ", ".join(m_text(c["dbColumn"]) for c in cols) + "})")
        renames = [(c["dbColumn"], c["name"]) for c in cols if c["dbColumn"] != c["name"]]
        last = "Columns"
        if renames:
            steps.append(f"Renamed = Table.RenameColumns({last}, {{"
                         + ", ".join(f"{{{m_text(a)}, {m_text(b)}}}" for a, b in renames) + "})")
            last = "Renamed"
    else:
        def q(n: str) -> str:
            return exp.to_identifier(n, quoted=True).sql(dialect=source["dialect"])
        if db.get("sqlExpression"):
            src = f"({db['sqlExpression']}) t"
        else:
            src = (q(db["namespace"]) + "." if db.get("namespace") else "") + q(db["tableName"])
        select = [f"{c['computed'] or q(c['dbColumn'])} AS {q(c['name'])}" for c in cols]
        sql = "select " + ",\n       ".join(select) + f"\nfrom {src}"
        steps.append(f"Data = Value.NativeQuery({source['query_target']}, {m_text(sql)})")
        last = "Data"
    steps.append(f"Typed = Table.TransformColumnTypes({last}, {{"
                 + ", ".join(f"{{{m_text(c['name'])}, {M_TYPES[c['dataType']]}}}" for c in cols) + "})")
    return "let\n" + ",\n".join(f"    {s}" for s in steps) + "\nin\n    Typed", native


def build_semantic_model(spec: dict, source_type: str, conn_values: dict, issues: Issues):
    source = SOURCES[source_type]
    parameters, missing = [], []
    for p in source["parameters"]:
        value = (conn_values.get(p) or "").strip()
        if p == "SourceServer" and value and (cleaned := clean_server(value)) != value:
            issues.add("warning", "connection", f"Source server '{value}' is not a valid host name; used "
                                                f"'{cleaned}'. Correct it in the migration inventory.")
            value = cleaned
        if not value:
            missing.append(p)
        parameters.append({"name": p, "type": "Text", "value": value or f"<set {PARAM_ENV[p]}>"})
    if missing:
        issues.add("warning", "connection", f"No value for {', '.join(missing)}: fill the Source columns in the "
                                            "migration inventory (row or Settings), or edit the parameters in "
                                            "Power BI (Transform data > Edit parameters).")

    lk, tables = Lookups(), []
    taken_tables, taken_measures = set(), set()
    formats = collect_metric_formats(spec)
    used_metrics = metrics_on_visuals(spec)
    star = Star(source)

    for ds in spec["datasets"]:
        # Schema cubes: fact and dimension tables (star schema) rather than the cube's multi-pass SQL as one table
        if (ds.get("schema") or {}).get("tables") and ds.get("sourceSql") and \
                add_star_dataset(ds, star, lk, tables, taken_tables, taken_measures, formats, issues):
            continue
        tname = _unique(ds["name"], taken_tables)
        lk.ds_table[ds["id"]] = tname
        where = f"dataset '{ds['name']}'"
        sql = ds.get("sourceSql")
        sql_cols, ds_metrics, exact_forms = [], ds["metrics"], None
        if not sql:
            issues.add("error", where, "No source SQL extracted; the table query must be written manually.")
        else:
            try:
                if ds.get("schema"):
                    sql, exact_forms, ds_metrics = prepare_schema_dataset(ds, source["dialect"], issues, where)
                sql_cols = parse_select_columns(sql, source["dialect"])
            except Exception as e:  # sqlglot errors vary by dialect
                issues.add("error", where, f"Could not parse source SQL ({e}); map columns manually.")
        if ds.get("analyticalEngineSteps"):
            issues.add("warning", where, "Cube SQL has analytical-engine steps that run after the query; "
                                         "replicate them in Power Query or DAX.")

        targets = mapping_targets(ds, ds_metrics, exact_forms)
        assigned = assign_columns(targets, sql_cols)
        taken_cols, columns, rename, types, mapping = set(), [], {}, [], []

        # Attribute forms -> visible columns
        for ti, t in enumerate(targets):
            if t["kind"] != "form":
                continue
            a, f, hit = t["attr"], t["form"], assigned.get(ti)
            geo_category = GEO_FORM_CATEGORIES.get((f.get("category") or "").lower())
            if not hit:
                if geo_category:
                    continue  # added by MicroStrategy geo mapping, not by the SQL; handled with the attribute below
                issues.add("error", where, f"Attribute form '{a['name']}' / '{f['name']}' has no matching "
                                           "SQL column; add it manually.")
                continue
            cname = _unique(form_column_name(a, f), taken_cols)
            dtype = pbi_type(f.get("dataType"), "string", issues, where, f"'{a['name']}' / '{f['name']}'")
            columns.append({"name": cname, "sourceColumn": cname, "dataType": dtype,
                            "summarizeBy": "none", "isHidden": False, "dataCategory": geo_category,
                            "mstr": {"attribute": a["name"], "attributeId": a["id"], "form": f["name"],
                                     "formCategory": f.get("category"), "dataType": f.get("dataType")},
                            "sqlAlias": hit["alias"], "mappingConfidence": hit["confidence"]})
            rename[hit["alias"].lower()] = cname
            types.append((cname, M_TYPES[dtype]))
            lk.form_col[(ds["id"], a["id"], f["id"])] = cname
            lk.col_type[(tname, cname)] = dtype
            mapping.append({"sqlAlias": hit["alias"], "sqlExpression": hit["expression"],
                            "mappedTo": f"attribute {a['name']} / {f['name']}",
                            "pbiColumn": cname, "confidence": hit["confidence"]})
        for a in ds["attributes"]:
            mapped = [(f, lk.form_col.get((ds["id"], a["id"], f["id"]))) for f in a["forms"]]
            mapped = [(f, c) for f, c in mapped if c]
            if mapped:
                desc = [c for f, c in mapped if f.get("category") == "DESC"]
                lk.attr_display[(ds["id"], a["id"])] = desc[0] if desc else mapped[0][1]
            if len(mapped) > 1:
                ids = [c for f, c in mapped if f.get("category") == "ID"]
                others = [c for f, c in mapped if f.get("category") != "ID"]
                if ids and others:
                    # MicroStrategy lists elements in ID order (regions by code, months in calendar order), not by
                    # name: every other form of the attribute sorts by its ID.
                    for col in others:
                        next(c for c in columns if c["name"] == col)["sortByColumn"] = ids[0]
                    issues.add("info", where, f"Attribute '{a['name']}': {', '.join(others)} sorted by '{ids[0]}' to "
                               "keep MicroStrategy's ID order. If refresh fails with 'more than one value', names "
                               "are not 1:1 with IDs: remove Sort by column.")

        # Geo attributes: MicroStrategy places them on maps with latitude/longitude forms,
        # Power BI with the data category of the column that shows the attribute.
        for a in ds["attributes"]:
            display = lk.attr_display.get((ds["id"], a["id"]))
            if not display or not any((f.get("category") or "").lower() in GEO_FORM_CATEGORIES for f in a["forms"]):
                continue
            column = next(c for c in columns if c["name"] == display)
            role = geo_role(a["name"])
            if role:
                column["dataCategory"] = role
                issues.add("info", where, f"'{a['name']}' is a MicroStrategy geo attribute (latitude/longitude "
                                          f"forms); column '{display}' gets Power BI data category {role} for maps.")
            else:
                issues.add("warning", where, f"'{a['name']}' is a MicroStrategy geo attribute; set the Data category "
                                             f"of column '{display}' in Power BI (Column tools) for maps.")

        # Metrics -> measures (+ hidden base columns)
        measures, compound, level_measures = [], [], []
        metric_target = {t["metric"]["id"]: ti for ti, t in enumerate(targets) if t["kind"] == "metric"}
        for m in ds_metrics:
            # Data Import adds a 'Row Count - <table>' metric to every dataset: only migrate it if a visual uses it.
            if is_row_count(m) and m["id"] not in used_metrics:
                issues.add("info", where, f"Skipped '{m['name']}': automatic Data Import row count, "
                                          "not used on any visual.")
                continue
            if m.get("formula"):
                compound.append(m)  # built below, once the measures it uses exist
                continue
            mname = m["name"]
            if mname.lower() in taken_measures:
                mname = f"{mname} ({tname})"
            elif mname.lower() in taken_cols:
                mname = f"{mname} (Metric)"
            mname = _unique(mname, taken_measures)
            fmt = None
            if m["id"] in formats:
                fmt, simplified = pbi_format(*formats[m["id"]])
                if simplified:
                    issues.add("info", where, f"Measure '{mname}': {simplified}")
            measure = {"name": mname, "formatString": fmt,
                       "mstr": {"metric": m["name"], "metricId": m["id"], "dataType": m.get("dataType"),
                                "formatString": formats.get(m["id"], (None,))[0]}}
            if is_row_count(m):
                measure.update(expression=f"COUNTROWS({dax_table(tname)})", formatString="#,0",
                               note="Automatic Data Import row count, kept because a visual uses it.")
            elif (hit := assigned.get(metric_target[m["id"]])):
                # Schema cubes: the column holds a base metric, name it after the metric (aliases are WJXBFS1...)
                base = f"{m['name']} (base)" if "aggregation" in m else hit["alias"]
                if base.lower() in taken_cols or base.lower() in taken_measures:
                    base = f"{base} (base)"
                base = _unique(base, taken_cols, (taken_measures,))
                dtype = pbi_type(m.get("dataType"), "double", issues, where, f"metric '{m['name']}'")
                columns.append({"name": base, "sourceColumn": base, "dataType": dtype, "summarizeBy": "sum",
                                "isHidden": True, "mstr": {"metricSource": m["name"]},
                                "sqlAlias": hit["alias"], "mappingConfidence": hit["confidence"]})
                rename[hit["alias"].lower()] = base
                types.append((base, M_TYPES[dtype]))
                lk.col_type[(tname, base)] = dtype
                if "aggregation" in m:
                    agg = DAX_AGGREGATION.get(m["aggregation"], "SUM")
                    measure.update(expression=f"{agg}({dax_col(tname, base)})",
                                   note=f"MicroStrategy base metric: {m['aggregation']} of {hit['expression']} "
                                        f"(SQL pass {m.get('sqlPass') or 'final'}).")
                    if m["aggregation"] == "avg":
                        issues.add("warning", where, f"'{m['name']}' averages pre-aggregated rows; check totals.")
                    if m.get("levelColumns"):
                        level_measures.append((measure, base, m))
                else:
                    measure.update(expression=f"SUM({dax_col(tname, base)})",
                                   note=f"Assumes the Data Import default aggregation (Sum). Source SQL "
                                        f"expression: {hit['expression']}.")
                mapping.append({"sqlAlias": hit["alias"], "sqlExpression": hit["expression"],
                                "mappedTo": f"metric {m['name']}", "pbiColumn": base,
                                "confidence": hit["confidence"]})
            else:
                measure.update(expression="BLANK()", note="TODO: no SQL column matched this metric.")
                issues.add("error", where, f"Metric '{m['name']}' has no matching SQL column; "
                                           "write its DAX manually.")
            if m.get("hidden"):
                measure["isHidden"] = True  # only used by other measures, as in MicroStrategy
            lk.measure[(ds["id"], m["id"])] = mname
            measures.append(measure)

        # SQL columns no MicroStrategy object uses -> keep, hidden
        used, unused = {h["index"] for h in assigned.values()}, []
        for ci, c in enumerate(sql_cols):
            if ci in used:
                continue
            cname = _unique(c["alias"], taken_cols, (taken_measures,))
            columns.append({"name": cname, "sourceColumn": cname, "dataType": None, "summarizeBy": "none",
                            "isHidden": True, "sqlAlias": c["alias"], "mappingConfidence": None})
            rename[c["alias"].lower()] = cname
            unused.append(c["alias"])
        if unused:
            issues.add("info", where, f"{len(unused)} SQL column(s) not used by any MicroStrategy object, imported "
                                      f"as hidden columns: {', '.join(unused[:12])}{' ...' if len(unused) > 12 else ''}")

        # Level metrics (e.g. SQM per Location) repeat on every row of their level: count each level once.
        for measure, base, m in level_measures:
            keys = [rename[c] for c in m["levelColumns"] if c in rename]
            if len(keys) == len(m["levelColumns"]):
                over = ", ".join(dax_col(tname, k) for k in keys)
                measure["expression"] = (f"SUMX(SUMMARIZE({dax_table(tname)}, {over}), "
                                         f"CALCULATE(MAX({dax_col(tname, base)})))")
            else:
                issues.add("warning", where, f"'{m['name']}' is at level {m['levelColumns']}, not all in the table; "
                                             "summed per row, so totals may count it several times.")

        # Compound metrics: arithmetic of the measures above (and of each other)
        measure_of = {mid: (tname, name) for (dsid, mid), name in lk.measure.items() if dsid == ds["id"]}
        while compound:
            ready = [m for m in compound if all(i in measure_of for i in _formula_metrics(m["formula"]))]
            for m in ready or compound:
                compound.remove(m)
                mname = _unique(f"{m['name']} (Metric)" if m["name"].lower() in taken_cols else m["name"],
                                taken_measures)
                fmt, simplified = pbi_format(*formats[m["id"]]) if m["id"] in formats else (None, None)
                if simplified:
                    issues.add("info", where, f"Measure '{mname}': {simplified}")
                measure = {"name": mname, "formatString": fmt,
                           "mstr": {"metric": m["name"], "metricId": m["id"], "dataType": m.get("dataType"),
                                    "formatString": formats.get(m["id"], (None,))[0]}}
                if ready:
                    measure.update(expression=dax_formula(m["formula"], measure_of),
                                   note="MicroStrategy compound metric, rebuilt from its formula.")
                else:
                    measure.update(expression="BLANK()", note="TODO: formula uses metrics that were not rebuilt.")
                    issues.add("error", where, f"Compound metric '{m['name']}' uses metrics that were not rebuilt; "
                                               "write its DAX manually.")
                if m.get("hidden"):
                    measure["isHidden"] = True
                lk.measure[(ds["id"], m["id"])] = mname
                measure_of[m["id"]] = (tname, mname)
                measures.append(measure)
                if not ready:
                    break
        for item in mapping:
            if item["confidence"] < 1.0:
                issues.add("warning", where, f"Fuzzy match: SQL column '{item['sqlAlias']}' -> "
                                             f"{item['mappedTo']} (confidence {item['confidence']}). Verify.")

        tables.append({
            "name": tname,
            "mstrDatasetId": ds["id"],
            "mstrDatasetType": f"{ds.get('cubeType')} / {ds.get('sourceType')}",
            "mode": "import",
            "partition": {"name": tname, "type": "M",
                          "expression": build_m_expression(source, sql, rename, types) if sql else None},
            "sourceSql": sql,
            "sqlColumnMapping": mapping,
            "columns": columns,
            "measures": measures,
        })

    relationships = finish_star(star, lk, issues)
    add_derived_measures(spec, tables, formats, taken_measures, lk, issues)
    relationships += add_linked_dimensions(spec, tables, taken_tables, lk, issues)
    model = {"parameters": parameters, "tables": tables, "relationships": relationships}
    return model, lk


def add_linked_dimensions(spec: dict, tables: list[dict], taken_tables: set[str], lk: Lookups,
                          issues: Issues) -> list[dict]:
    """Attributes with the same name in several datasets are linked in a MicroStrategy dashboard: a selector or
    filter on one filters every dataset. Power BI: a shared dimension (calculated table) related to each table.
    Returns the relationships."""
    groups = {}
    for ds in spec["datasets"]:
        for a in ds["attributes"]:
            if (ds["id"], a["id"]) in lk.attr_display:
                groups.setdefault(_norm(a["name"]), []).append((ds["id"], a))
    relationships = []
    for members in groups.values():
        if len({ds_id for ds_id, _ in members}) < 2:
            continue
        attr = members[0][1]
        where = f"attribute '{attr['name']}'"
        # Per dataset: (table, ID-form column or None, displayed column)
        parts = []
        for ds_id, a in members:
            ids = [lk.form_col[(ds_id, a["id"], f["id"])] for f in a["forms"]
                   if f.get("category") == "ID" and (ds_id, a["id"], f["id"]) in lk.form_col
                   and lk.table_of((ds_id, a["id"], f["id"])) == lk.table_of((ds_id, a["id"]))]
            parts.append((ds_id, a, lk.table_of((ds_id, a["id"])), ids[0] if ids else None,
                          lk.attr_display[(ds_id, a["id"])]))
        if len({(p[2], p[4]) for p in parts}) == 1:
            continue  # star schema: the datasets already share the attribute's dimension table
        use_id = all(p[3] for p in parts)  # relate on the ID form when every dataset has it
        sources = [[p[3], p[4]] if use_id and p[3] != p[4] else [p[3] if use_id else p[4]] for p in parts]
        dim_cols = sources[0]
        types = [[lk.col_type[(p[2], c)] for c in s] for p, s in zip(parts, sources)]
        if any(t != types[0] for t in types) or any(len(s) != len(dim_cols) for s in sources):
            issues.add("warning", where, f"In datasets {', '.join(p[2] for p in parts)} with different columns or "
                                         "data types; not linked. MicroStrategy links them: relate them manually.")
            continue
        name = _unique(attr["name"], taken_tables)
        selects = [f"SELECTCOLUMNS({dax_table(p[2])}, "
                   + ", ".join(f"{dax_string(n)}, {dax_col(p[2], c)}" for n, c in zip(dim_cols, s)) + ")"
                   for p, s in zip(parts, sources)]
        columns = [{"name": n, "sourceColumn": f"[{n}]", "dataType": t, "summarizeBy": "none", "isHidden": False,
                    "mstr": {"attribute": attr["name"], "form": "linked across datasets"}, "sqlAlias": None}
                   for n, t in zip(dim_cols, types[0])]
        if len(dim_cols) > 1:
            columns[1]["sortByColumn"] = dim_cols[0]  # names in ID order, as MicroStrategy lists them
        tables.append({"name": name, "kind": "calculated", "mstrDatasetId": None,
                       "mstrDatasetType": "shared dimension", "linkedAttribute": attr["name"],
                       "expression": "DISTINCT(\n    UNION(\n        " + ",\n        ".join(selects) + "\n    )\n)",
                       "partition": None, "columns": columns, "measures": []})
        for col, t in zip(dim_cols, types[0]):
            lk.col_type[(name, col)] = t
        for (ds_id, a, table, id_col, display), src in zip(parts, sources):
            relationships.append({"fromTable": table, "fromColumn": src[0], "toTable": name, "toColumn": dim_cols[0]})
            for c in next(t for t in tables if t["name"] == table)["columns"]:
                if c["name"] in src:
                    c["isHidden"] = True  # use the shared table instead
            by_form = {f["id"]: dim_cols[src.index(lk.form_col[(ds_id, a["id"], f["id"])])] for f in a["forms"]
                       if lk.form_col.get((ds_id, a["id"], f["id"])) in src}
            lk.linked[a["id"]] = {"table": name, "display": dim_cols[-1], "byForm": by_form}
        lk.related[name] = {p[2] for p in parts}
        issues.add("info", where, f"In datasets {', '.join(p[2] for p in parts)}. MicroStrategy dashboards link "
                                  f"attributes with the same name, so its selectors and filters filter every dataset. "
                                  f"Shared table '{name}' ({', '.join(dim_cols)}) is related to each; slicers, page "
                                  "filters and visuals use it.")
    return relationships


def _formula_metrics(node: dict) -> list[str]:
    return [node["metric"]] if "metric" in node else [m for a in node["args"] for m in _formula_metrics(a)]


def dax_formula(node: dict, measure_of: dict[str, tuple[str, str]], top: bool = True) -> str:
    if "metric" in node:
        return dax_measure(measure_of[node["metric"]][1])
    left, right = (dax_formula(a, measure_of, False) for a in node["args"])
    if node["function"] == "/":
        return f"DIVIDE({left}, {right})"  # MicroStrategy returns empty, not an error, on division by zero
    text = f"{left} {node['function']} {right}"
    return text if top else f"({text})"


def add_derived_measures(spec: dict, tables: list[dict], formats: dict, taken_measures: set[str],
                         lk: Lookups, issues: Issues) -> None:
    """Metrics created in the dashboard -> measures, from the formula read in the .mstr export."""
    measure_of = {mid: (lk.table_of((ds, mid)), name) for (ds, mid), name in lk.measure.items()}
    for dm in spec.get("derivedMetrics", []):
        where = f"derived metric '{dm['name']}'"
        leaves = _formula_metrics(dm["formula"]) if dm["formula"] else []
        if leaves and all(m in measure_of for m in leaves):
            table, expression = measure_of[leaves[0]][0], dax_formula(dm["formula"], measure_of)
            note = f"Created in the MicroStrategy dashboard as {dm['formulaText']} (read from the .mstr export)."
            issues.add("info", where, f"Created in the dashboard; rebuilt as {expression}. "
                                      "The validation checks compare it with MicroStrategy's values.")
        elif tables:
            table, expression = tables[0]["name"], "BLANK()"
            note = "TODO: created in the MicroStrategy dashboard; formula not found."
            issues.add("error", where, "Created in the dashboard and its formula could not be read from the "
                                       ".mstr export; write its DAX manually.")
        else:
            continue
        home = next(t for t in tables if t["name"] == table)
        name = _unique(dm["name"], taken_measures, ({c["name"].lower() for c in home["columns"]},))
        fmt, simplified = pbi_format(*formats[dm["id"]]) if dm["id"] in formats else (None, None)
        if simplified:
            issues.add("info", where, f"Measure '{name}': {simplified}")
        home["measures"].append({"name": name, "expression": expression, "formatString": fmt, "note": note,
                                 "mstr": {"metric": dm["name"], "metricId": dm["id"], "derived": True,
                                          "formatString": formats.get(dm["id"], (None,))[0]}})
        lk.derived[dm["id"]] = {"table": table, "measure": name}
        measure_of[dm["id"]] = (table, name)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _datasets_in_order(spec: dict, preferred: list[str]) -> list[str]:
    return preferred + [d["id"] for d in spec["datasets"] if d["id"] not in preferred]


def resolve_attribute(attr: dict, ds_ids: list[str], lk: Lookups, all_forms: bool) -> list[dict]:
    """Return field refs for an attribute placed on a visual (one per form shown)."""
    shown = attr.get("formsShown") or []
    if not all_forms and len(shown) > 1:
        desc = [f for f in shown if f.get("category") == "DESC"]
        shown = desc[:1] or shown[:1]
    if attr["id"] in lk.linked:
        dim = lk.linked[attr["id"]]
        fields = [{"table": dim["table"], "column": dim["byForm"][f.get("id")], "mstrId": attr["id"],
                   "mstrFormId": f.get("id")} for f in shown if f.get("id") in dim["byForm"]]
        return fields or [{"table": dim["table"], "column": dim["display"], "mstrId": attr["id"], "mstrFormId": None}]
    for ds_id in ds_ids:
        fields = []
        for f in shown:
            col = lk.form_col.get((ds_id, attr["id"], f.get("id")))
            if col:
                fields.append({"table": lk.table_of((ds_id, attr["id"], f.get("id"))), "column": col,
                               "mstrId": attr["id"], "mstrFormId": f.get("id")})
        if not fields and (ds_id, attr["id"]) in lk.attr_display:
            fields.append({"table": lk.table_of((ds_id, attr["id"])), "column": lk.attr_display[(ds_id, attr["id"])],
                           "mstrId": attr["id"], "mstrFormId": None})
        if fields:
            return fields
    return []


def resolve_metric(metric: dict, ds_ids: list[str], lk: Lookups) -> dict | None:
    ref = next(({"table": lk.table_of((d, metric["id"])), "measure": lk.measure[(d, metric["id"])]}
                for d in ds_ids if (d, metric["id"]) in lk.measure), lk.derived.get(metric["id"]))
    if not ref:
        return None
    ref = {**ref, "mstrId": metric["id"]}
    if metric["name"] != ref["measure"]:
        ref["displayName"] = metric["name"]  # keep the label the dashboard shows, e.g. 'Gross Sales 2'
    return ref


def assign_roles(pbi: str, rows: list, cols: list, measures: list, issues: Issues, where: str) -> dict:
    attrs = cols + rows
    if pbi in CHART_TYPES:
        category, series = (cols, rows) if cols else (rows, [])
        roles = {"Category": category, "Y": measures}
        if series:
            roles["Breakdown" if pbi == "waterfallChart" else "Series"] = series[:1]
            if len(series) > 1:
                issues.add("warning", where, "Several break-by attributes; Power BI allows one legend field.")
        return roles
    if pbi in ("pieChart", "donutChart"):
        return {"Category": attrs[:1], "Y": measures[:1]}
    if pbi == "scatterChart":
        return {"Details": attrs, "X": measures[0:1], "Y": measures[1:2], "Size": measures[2:3]}
    if pbi == "lineClusteredColumnComboChart":
        return {"Category": attrs, "Y": measures[:1], "Y2": measures[1:]}
    if pbi == "pivotTable":
        return {"Rows": rows, "Columns": cols, "Values": measures}
    if pbi == "tableEx":
        return {"Values": rows + cols + measures}
    if pbi in ("card", "multiRowCard"):
        return {"Values": measures}
    if pbi == "gauge":
        return {"Y": measures[:1], "TargetValue": measures[1:2]}
    if pbi == "treemap":
        return {"Group": attrs, "Values": measures[:1]}
    if pbi == "map":
        return {"Category": attrs[:1], "Size": measures[:1]}
    return {"Fields": attrs + measures}


def build_visual(v: dict, spec: dict, lk: Lookups, issues: Issues, override: dict) -> dict:
    where = f"visual '{v['name']}' ({v['key']})"
    mstr_type = v["type"]
    if override.get("visualType"):  # e.g. a document grid that MicroStrategy displays as a graph
        pbi = override["visualType"]
    elif mstr_type in ("grid", "compound_grid"):
        pbi = "pivotTable" if (v["columns"] or mstr_type == "compound_grid") else "tableEx"
    else:
        pbi = VIZ_TYPE_MAP.get(mstr_type)
    notes = [VIZ_NOTES[mstr_type]] if mstr_type in VIZ_NOTES and not override.get("visualType") else []
    notes += [f"Override: {override['note']}"] if override.get("note") else []
    if not v.get("definitionRetrieved"):
        issues.add("error", where, "Visualization definition was not retrieved; rebuild manually.")
    if not pbi:
        issues.add("error", where, f"No Power BI mapping for MicroStrategy type '{mstr_type}'.")

    ds_ids = _datasets_in_order(spec, v.get("datasets") or [])
    is_grid = pbi in ("pivotTable", "tableEx")

    def attrs_of(units):
        fields = []
        for a in units:
            refs = resolve_attribute(a, ds_ids, lk, all_forms=is_grid)
            if not refs:
                issues.add("error", where, f"Attribute '{a['name']}' could not be resolved to a column.")
            elif len(a.get("formsShown") or []) > 1 and not is_grid:
                notes.append(f"'{a['name']}' shows forms "
                             f"{', '.join(f['name'] for f in a['formsShown'])}; Power BI uses '{refs[0]['column']}'.")
            fields += refs
        return fields

    rows, cols = attrs_of(v["rows"]), attrs_of(v["columns"])
    measures = []
    for m in v["metrics"]:
        ref = resolve_metric(m, ds_ids, lk)
        if ref:
            measures.append(ref)
        else:
            issues.add("error", where, f"Metric '{m['name']}' could not be resolved to a measure.")
    if pbi == "card" and len(measures) > 1:
        pbi = "multiRowCard"
    if (v.get("metricsPosition") or {}).get("axis") == "rows" and pbi == "pivotTable":
        notes.append("Metrics are on rows in MicroStrategy: enable 'Switch values to rows' on the matrix.")
    sorting = v.get("sorting") or {}
    if any(sorting.get(k) for k in ("rows", "columns", "pageBy")):
        notes.append(f"MicroStrategy sorting to replicate: {json.dumps(sorting)}")
    if v.get("thresholds"):
        notes.append(f"Thresholds (recreate as conditional formatting): {json.dumps(v['thresholds'])}")

    fields = {role: refs for role, refs in assign_roles(pbi, rows, cols, measures, issues, where).items() if refs}
    chart = {}
    if pbi in CHART_TYPES:
        chart = chart_settings(v, pbi, fields, lk, issues, where)
        pbi = chart.pop("visualType", None) or pbi
        if len(fields.get("Category", [])) > 1:
            notes.append(f"Axis expanded to all {len(fields['Category'])} levels "
                         f"({', '.join(f['column'] for f in fields['Category'])}) with nested labels, "
                         "as MicroStrategy shows them.")
    elif pbi in ("card", "multiRowCard"):
        # A MicroStrategy KPI shows its title and value only, not the metric name under the value.
        chart.update(displayUnitsNone=has_number_format(v), categoryLabel=False)
    return {"id": v["key"], "title": v["name"], "mstrType": mstr_type, "visualType": pbi,
            "fields": fields, **chart, "notes": notes}


def chart_settings(v: dict, pbi: str, fields: dict, lk: Lookups, issues: Issues, where: str) -> dict:
    """Colour-by, legend, data labels, sort and display units of a chart; may change fields['Series']."""
    out, display = {}, v.get("display") or {}
    category = fields.get("Category", [])
    color_by = v.get("colorBy")
    ref = color_by and next((r for r in category + fields.get("Series", [])
                             if r["mstrId"] == color_by["attributeId"]), None)
    measure_colors = {c["metricId"]: c["color"] for c in v.get("metricColors") or []}
    if ref and len(fields.get("Y", [])) > 1:
        if not measure_colors:
            issues.add("warning", where, f"Colour by '{color_by['attribute']}' with {len(fields['Y'])} metrics: a "
                                         "Power BI chart can't have a legend and several values, so bars use the "
                                         "default colour per metric. Compare with MicroStrategy.")
        ref = None  # several metrics: coloured by metric (the element colours are dashboard-wide)
    if not ref and measure_colors:
        out["measureColors"] = [{**m, "color": measure_colors[m["mstrId"]]} for m in fields.get("Y", [])
                                if m["mstrId"] in measure_colors]
    elif ref:
        fields["Series"] = [ref]
        out["seriesColors"] = color_by["colors"]
        out["seriesDataType"] = lk.col_type.get((ref["table"], ref["column"]), "string")
        on_axis = any(r["mstrId"] == color_by["attributeId"] for r in category)
        # Clustered charts leave a slot for every legend value in each category; when the colour attribute is
        # also on the axis each category has one value, so stacked gives the full-width bars MicroStrategy draws.
        if on_axis and pbi in STACKED:
            out["visualType"] = STACKED[pbi]
        issues.add("info", where, f"Colour by '{color_by['attribute']}' ({color_by['source']}): legend + "
                                  f"{len(color_by['colors'])} element colour(s). Check against MicroStrategy.")
    elif color_by and not any(r["mstrId"] == color_by["attributeId"] for r in category + fields.get("Series", [])):
        issues.add("warning", where, f"Colour by '{color_by['attribute']}' found but the attribute is not on the "
                                     "visual; colours not applied.")
    if display.get("subtype") == "Stacked" and pbi in STACKED:
        out["visualType"] = STACKED[pbi]
    if display:
        out["legend"] = display.get("legend")
        out["legendPosition"] = LEGEND_POSITIONS.get(display.get("legendPosition"))
        out["dataLabels"] = display.get("dataLabels")
    out["displayUnitsNone"] = has_number_format(v)
    # Titles as MicroStrategy writes them: attribute and metric names, metrics joined with ' | '.
    units = v["columns"] or v["rows"]  # the category axis, as in assign_roles
    out["axisTitles"] = {"category": units[-1]["name"] if units else None,
                         "value": " | ".join(m["name"] for m in v["metrics"]) or None}
    if fields.get("Series"):
        series_id = fields["Series"][0]["mstrId"]
        out["legendTitle"] = next((a["name"] for a in v["rows"] + v["columns"] if a["id"] == series_id), None)
    elif len(fields.get("Y", [])) > 1:
        out["legendTitle"] = "Metrics"
    # MicroStrategy turns data labels vertical when bars stand side by side (one per metric).
    out["labelsVertical"] = bool(out.get("dataLabels")) and len(fields.get("Y", [])) > 1
    if category and not any((v.get("sorting") or {}).values()):
        out["sort"] = category  # MicroStrategy default: elements in attribute order, not by value
    return out


def has_number_format(v: dict) -> bool:
    """Model format strings carry MicroStrategy's scaling (K/M); Power BI's automatic display units would
    scale again, so visuals with such metrics get display units None."""
    return any((m.get("numberFormat") or {}).get("formatString", "General").lower() != "general"
               for m in v["metrics"])


def default_selection(sel: dict, dtype: str) -> dict | None:
    """Selected elements of a selector or chapter filter: {mode: include|exclude, values}, None = all."""
    cur = sel.get("currentSelection") or {}
    if not cur.get("elements") or cur.get("allSelected"):
        return None
    return {"mode": "exclude" if cur.get("selectionStatus") == "excluded" else "include",
            "values": [parse_value(e["name"], dtype) for e in cur["elements"]]}


def attribute_columns(attr_id: str | None, ds_ids: list[str], lk: Lookups) -> list[tuple[str, str]]:
    """(table, column) that shows an attribute: its shared table when linked, else each dataset that has it."""
    if attr_id in lk.linked:
        return [(lk.linked[attr_id]["table"], lk.linked[attr_id]["display"])]
    hits = [(lk.table_of((d, attr_id)), lk.attr_display[(d, attr_id)]) for d in ds_ids if (d, attr_id) in lk.attr_display]
    return list(dict.fromkeys(hits))  # datasets of a star schema share dimension tables


def build_page_filters(ch: dict, spec: dict, lk: Lookups, issues: Issues) -> list[dict]:
    """Chapter filters apply to every page of the chapter: Power BI page-level filters on each page."""
    filters = []
    for f in ch.get("chapterFilters", []):
        where = f"chapter '{ch['name']}' filter '{f.get('name')}'"
        src = f.get("source") or {}
        hits = attribute_columns(src.get("id"), [d["id"] for d in spec["datasets"]], lk)
        if src.get("type") != 12 or not hits:
            issues.add("warning", where, f"Not converted (only attribute filters become page filters); add it "
                                         f"manually: {json.dumps(f)}")
            continue
        if f.get("targets"):
            issues.add("warning", where, f"Targets only {[t.get('key') for t in f['targets']]} in MicroStrategy; "
                                         "a Power BI page filter applies to every visual on the page.")
        for table, column in hits:
            dtype = lk.col_type.get((table, column), "string")
            filters.append({"id": f["key"] if len(hits) == 1 else f"{f['key']}|{table}", "title": f["name"],
                            "field": {"table": table, "column": column, "mstrId": src["id"]}, "dataType": dtype,
                            "defaultSelection": default_selection(f, dtype),
                            "singleSelect": not f.get("multiSelectionAllowed", True),
                            "mstrDisplayStyle": f.get("displayStyle")})
    return filters


def build_slicer(sel: dict, page: dict, spec: dict, lk: Lookups, issues: Issues) -> dict | None:
    where = f"selector '{sel['name']}' ({sel['key']})"
    src = sel.get("source") or {}
    if src.get("type") != 12:  # 12 = attribute
        issues.add("warning", where, f"Selector type '{sel.get('selectorType')}' on a non-attribute source is "
                   "not converted (metric selectors -> visual-level filters; object selectors -> field parameters).")
        return None
    page_viz = {v["key"]: v for v in page["visualizations"]}
    targets = [t for t in sel.get("targets", []) if t in page_viz]
    ds_pref = []
    for t in targets:
        ds_pref += [d for d in page_viz[t].get("datasets", []) if d not in ds_pref]
    hits = attribute_columns(src["id"], _datasets_in_order(spec, ds_pref), lk)
    if not hits:
        issues.add("error", where, f"Attribute '{src.get('name')}' could not be resolved to a column.")
        return None
    table, column = hits[0]
    target_tables = {lk.ds_table[d] for d in ds_pref}
    if target_tables and table not in target_tables and not target_tables <= lk.related.get(table, set()):
        issues.add("warning", where, f"Slicer column {table}[{column}] is not related to the tables of the visuals "
                                     f"it targets ({', '.join(sorted(target_tables))}); relate them in Power BI.")
    dtype = lk.col_type.get((table, column), "string")
    default = default_selection(sel, dtype)
    other_targets = [t for t in sel.get("targets", []) if t not in page_viz]
    notes = [f"Also targets other selectors (cascading): {other_targets}"] if other_targets else []
    return {"id": sel["key"], "title": sel["name"], "visualType": "slicer",
            "field": {"table": table, "column": column, "mstrId": src["id"]},
            "dataType": dtype,
            "style": SLICER_STYLES.get(sel.get("displayStyle"), "List"),
            "mstrDisplayStyle": sel.get("displayStyle"),
            "singleSelect": not sel.get("multiSelect", True),
            "showSelectAll": bool(sel.get("hasAllOption")),
            "defaultSelection": default,
            "appliesTo": targets,
            "doesNotApplyTo": [k for k in page_viz if k not in sel.get("targets", [])],
            "notes": notes}


def apply_layout(slicers: list[dict], visuals: list[dict], layout: dict | None) -> tuple[float, float, bool]:
    """Set each item's position; return (page width, page height, MicroStrategy layout used). Uses the
    free-form layout from the .mstr export when it places every item, else a simple grid."""
    items = (layout or {}).get("items", {})
    if items and all(x["id"] in items for x in slicers + visuals):
        for x in slicers + visuals:
            x["position"] = {k: round(items[x["id"]][k], 1) for k in ("x", "y", "width", "height")}
        bottom = max(x["position"]["y"] + x["position"]["height"] for x in slicers + visuals)
        return layout["width"], max(layout["height"], bottom), True
    top = GAP
    if slicers:
        w = min(240, (CANVAS_W - GAP * (len(slicers) + 1)) // len(slicers))
        for i, s in enumerate(slicers):
            s["position"] = {"x": GAP + i * (w + GAP), "y": GAP, "width": w, "height": 70}
        top = GAP + 70 + GAP
    if visuals:
        ncols = math.ceil(math.sqrt(len(visuals)))
        nrows = math.ceil(len(visuals) / ncols)
        w = (CANVAS_W - GAP * (ncols + 1)) // ncols
        h = (CANVAS_H - top - GAP * nrows) // nrows
        for i, v in enumerate(visuals):
            r, c = divmod(i, ncols)
            v["position"] = {"x": GAP + c * (w + GAP), "y": top + r * (h + GAP), "width": w, "height": h}
    return CANVAS_W, CANVAS_H, False


def page_name(ch: dict, pg: dict, multi_chapter: bool) -> str:
    """Most meaningful name: a chapter's own name beats a default 'Page 1' when the chapter has one page."""
    if len(ch["pages"]) == 1 and DEFAULT_NAME.match(pg["name"] or "") and not DEFAULT_NAME.match(ch["name"] or ""):
        return ch["name"]
    return f"{ch['name']} - {pg['name']}" if multi_chapter else pg["name"]


def apply_overrides(slicers: list[dict], visuals: list[dict], page_key: str, overrides: dict,
                    size: tuple[float, float]) -> tuple[float, float, int]:
    """Positions, titles (and page size) from 1_input/overrides/<ID>.json; returns (width, height, items changed)."""
    items, changed = overrides.get("items", {}), 0
    for x in slicers + visuals:
        o = items.get(x["id"], {})
        if "position" in o:
            x["position"] = {k: o["position"][k] for k in ("x", "y", "width", "height")}
        if "title" in o:
            x["title"] = o["title"]
        changed += bool(o)
    page = overrides.get("pages", {}).get(page_key, {})
    return page.get("width", size[0]), page.get("height", size[1]), changed


def build_report(spec: dict, lk: Lookups, issues: Issues, overrides: dict) -> dict:
    pages, multi_chapter = [], len(spec["chapters"]) > 1
    items = overrides.get("items", {})
    for ch in spec["chapters"]:
        page_filters = build_page_filters(ch, spec, lk, issues)
        for pg in ch["pages"]:
            visuals = [build_visual(v, spec, lk, issues, items.get(v["key"], {})) for v in pg["visualizations"]]
            slicers = [s for s in (build_slicer(sel, pg, spec, lk, issues) for sel in pg["selectors"]) if s]
            width, height, from_mstr = apply_layout(slicers, visuals, pg.get("layout"))
            width, height, changed = apply_overrides(slicers, visuals, pg["key"], overrides, (width, height))
            name = page_name(ch, pg, multi_chapter)
            if changed:
                issues.add("info", f"page '{name}'", f"{changed} item(s) set from the overrides file "
                                                     "(chart type, title or position).")
            elif not from_mstr:
                issues.add("info", f"page '{name}'", "No free-form MicroStrategy layout for every visual (auto layout "
                                                     "or .mstr not read); used a simple grid. Arrange the visuals "
                                                     "manually.")
            used = {r["table"] for v in visuals for refs in v["fields"].values() for r in refs}
            for f in page_filters:
                field = f["field"]
                if not used & ({field["table"]} | lk.related.get(field["table"], set())):
                    issues.add("info", f"page '{name}'", f"Page filter '{f['title']}' "
                               f"({field['table']}[{field['column']}]) has no effect here: the page's "
                               "visuals use other datasets, which MicroStrategy does not link to it either.")
                for s in slicers:
                    a, b = f["defaultSelection"], s["defaultSelection"]
                    if (s["field"]["column"] == field["column"] and s["field"]["table"] == field["table"]
                            and a and b and a["mode"] == b["mode"] == "include" and not set(a["values"]) & set(b["values"])):
                        issues.add("warning", f"page '{name}'",
                                   f"Page filter '{f['title']}' = {a['values']} and slicer '{s['title']}' = {b['values']} "
                                   "never match, so the visuals it filters are empty. MicroStrategy applies both "
                                   "the chapter filter and the selector too; check the dashboard's defaults.")
            pages.append({"name": name,
                          "mstrChapterKey": ch["key"], "mstrPageKey": pg["key"],
                          "referencePdfPage": pg.get("referencePdfPage"),
                          "width": width, "height": height,
                          "filters": page_filters, "slicers": slicers, "visuals": visuals})
    return {"name": spec["source"]["dashboardName"], "filters": lk.report_filters, "pages": pages}


# ---------------------------------------------------------------------------
# Validation: DAX queries vs the values MicroStrategy returned
# ---------------------------------------------------------------------------

def _viz_records(raw: dict) -> tuple[list[dict], bool]:
    """Flatten MicroStrategy v2 visualization data into {('attr', id): [formValues], ('metric', id): value}."""
    grid, data = raw["definition"]["grid"], raw["data"]
    hdr_rows, hdr_cols = data["headers"].get("rows", []), data["headers"].get("columns", [])
    values = data["metricValues"]["raw"]
    records = {}
    for r, row in enumerate(values):
        for c, val in enumerate(row):
            key, metric, subtotal = [], None, False
            for units, pick in ((grid.get("rows", []), lambda i: hdr_rows[r][i]),
                                (grid.get("columns", []), lambda i: hdr_cols[i][c])):
                for i, unit in enumerate(units):
                    el = unit["elements"][pick(i)]
                    subtotal |= bool(el.get("subtotal"))
                    if unit.get("type") == "templateMetrics":
                        metric = el["id"]
                    else:
                        key.append((unit["id"], tuple(el.get("formValues", [])),
                                    tuple(f.get("id") for f in unit.get("forms", []))))
            if subtotal:
                continue  # 'Total' rows are not elements; the Power BI visual computes its own total
            rec = records.setdefault(tuple(key), {})
            if metric:
                rec[metric] = val
    paging = data.get("paging", {})
    complete = paging.get("total", 0) <= paging.get("offset", 0) + paging.get("current", 0)
    return [{"key": k, "metrics": m} for k, m in records.items()], complete


def build_validation(out_dir: Path, report: dict, lk: Lookups, issues: Issues) -> list[dict]:
    checks = []
    for page in report["pages"]:
        for v in page["visuals"]:
            raw_file = out_dir / "raw" / f"viz_{page['mstrChapterKey']}_{v['id']}.json"
            if not raw_file.exists() or not v["visualType"]:
                continue
            attr_fields = [f for refs in v["fields"].values() for f in refs if "column" in f]
            measure_fields = [f for refs in v["fields"].values() for f in refs if "measure" in f]
            if not measure_fields:
                continue
            try:
                records, complete = _viz_records(json.loads(raw_file.read_text(encoding="utf-8")))
            except (KeyError, IndexError, TypeError) as e:
                issues.add("info", f"visual {v['id']}", f"Could not parse MicroStrategy data for validation ({e}).")
                continue

            expected = []
            for rec in records:
                row = {}
                for attr_id, form_values, form_ids in rec["key"]:
                    for f in (f for f in attr_fields if f["mstrId"] == attr_id):
                        idx = form_ids.index(f["mstrFormId"]) if f["mstrFormId"] in form_ids else 0
                        dtype = lk.col_type.get((f["table"], f["column"]), "string")
                        row[f"{f['table']}[{f['column']}]"] = parse_value(form_values[idx], dtype)
                for m in measure_fields:
                    row[f"[{m['measure']}]"] = rec["metrics"].get(m["mstrId"])
                expected.append(row)
            sort_keys = list(dict.fromkeys(f"{f['table']}[{f['column']}]" for f in attr_fields))
            expected.sort(key=lambda r: tuple(str(r.get(k)) for k in sort_keys))

            group_cols = list(dict.fromkeys(dax_col(f["table"], f["column"]) for f in attr_fields))
            filters, filter_desc = [], []
            v_tables = {r["table"] for refs in v["fields"].values() for r in refs}
            active = ([s for s in page["slicers"] if v["id"] in s["appliesTo"]]
                      + [f for f in page.get("filters", []) + report.get("filters", [])
                         if v_tables & ({f["field"]["table"]} | lk.related.get(f["field"]["table"], set()))])
            for s in active:
                if s["defaultSelection"]:
                    col = dax_col(s["field"]["table"], s["field"]["column"])
                    vals = ", ".join(dax_literal(x, s["dataType"]) for x in s["defaultSelection"]["values"])
                    if s["defaultSelection"]["mode"] == "include":
                        filters.append(f"TREATAS({{{vals}}}, {col})")
                    else:
                        filters.append(f"FILTER(ALL({col}), NOT {col} IN {{{vals}}})")
                    filter_desc.append(f"{s['title']} = {s['defaultSelection']['values']}")
            args = group_cols + filters + [f"{dax_string(m['measure'])}, {dax_measure(m['measure'])}"
                                           for m in measure_fields]
            dax = "EVALUATE\nSUMMARIZECOLUMNS(\n    " + ",\n    ".join(args) + "\n)"
            if group_cols:
                dax += "\nORDER BY " + ", ".join(group_cols)
            checks.append({"page": page["name"], "visual": v["id"], "title": v["title"],
                           "filtersApplied": filter_desc, "dax": dax, "expected": expected,
                           "expectedIsComplete": complete,
                           "tolerance": {"absolute": 0.01, "relative": 1e-6}})
    return checks


# ---------------------------------------------------------------------------
# Runbook
# ---------------------------------------------------------------------------

def write_build_plan(bs: dict, path: Path) -> None:
    src, model, report = bs["source"], bs["semanticModel"], bs["report"]
    o = [f"# Power BI build plan - {src['dashboardName']}", "",
         f"Migrated from MicroStrategy dashboard `{src['dashboardId']}` (project `{src['projectId']}`). "
         f"Generated by transform_to_powerbi.py on {bs['generatedAt']}. Source database type: "
         f"**{bs['target']['sourceType']}**.", "",
         "## Instructions for Claude (Power BI MCP)", "",
         "Build the Power BI semantic model and report described in `powerbi_build_spec.json` (same folder), "
         "following the steps below in order.", "",
         "1. Use the object names exactly as written. Do not add fields, measures or visuals that are not in the spec.",
         "2. If an MCP tool cannot perform a step (for example report visuals), say which step and continue.",
         "3. After refreshing, run every validation query. Compare the results with the expected values "
         "(tolerance 0.01) and report each difference.",
         "4. Finish with a summary: steps done, steps skipped, validation pass/fail, and the open issues below.", ""]

    o += ["## Step 1 - Power Query parameters", "", "| Name | Type | Value |", "|---|---|---|"]
    o += [f"| {p['name']} | {p['type']} | `{p['value']}` |" for p in model["parameters"]]
    o.append("")

    for t in model["tables"]:
        if t.get("kind") == "calculated":
            o += [f"## Step 2 - Table `{t['name']}` (calculated, shared dimension)", "",
                  f"MicroStrategy links attribute '{t['linkedAttribute']}' across datasets.", "",
                  "**DAX**", "", "```dax", t["expression"], "```", ""]
        elif t.get("kind") == "star":
            o += [f"## Step 2 - Table `{t['name']}` ({t['role']}, import)", "",
                  f"{t['description']}. MicroStrategy dataset(s) {', '.join(f'`{d}`' for d in t['mstrDatasetIds'])}.",
                  "", "**Partition (Power Query M)**", "", "```powerquery", t["partition"]["expression"], "```", ""]
        else:
            o += [f"## Step 2 - Table `{t['name']}` (import)", "",
                  f"MicroStrategy dataset `{t['mstrDatasetId']}` ({t['mstrDatasetType']}).", "",
                  "**Partition (Power Query M)**", "", "```powerquery", t["partition"]["expression"] or "// TODO",
                  "```", ""]
        o += ["**Columns**", "", "| Column | Data type | Hidden | Summarize by | From SQL | MicroStrategy |",
              "|---|---|---|---|---|---|"]
        for c in t["columns"]:
            ms = c.get("mstr") or {}
            origin = (f"{ms['attribute']} / {ms['form']}" if "attribute" in ms
                      else f"source of metric {ms['metricSource']}" if "metricSource" in ms else "-")
            dtype = (c["dataType"] or "(source type)") + (f" ({c['dataCategory']})" if c.get("dataCategory") else "")
            sql = c.get("sqlAlias") or c.get("computed") or c.get("dbColumn")
            o.append(f"| {c['name']} | {dtype} | {c['isHidden']} | {c['summarizeBy']} "
                     f"| {'`' + sql + '`' if sql else '-'} | {origin} |")
        o += ["", "**Measures**", "", "| Measure | DAX | Format | Note |", "|---|---|---|---|"]
        o += [f"| {m['name']} | `{m['expression']}` | {m['formatString'] or '-'} | {m.get('note', '')} |"
              for m in t["measures"]]
        o.append("")

    o += ["## Step 3 - Relationships", ""]
    o += [f"- `{r['fromTable']}[{r['fromColumn']}]` (many) -> `{r['toTable']}[{r['toColumn']}]` (one), single direction"
          + (f" (SQL join `{r['sqlJoin']}`)" if r.get("sqlJoin") else "")
          for r in model["relationships"]] or ["None needed (no attribute shared by several datasets)."]
    native = [t["name"] for t in model["tables"] if t.get("kind") != "calculated"
              and (t.get("kind") != "star" or t.get("nativeQuery"))]
    o += ["", "## Step 4 - Refresh", "", "Refresh the model and confirm every table loads without errors."
          + (f" {', '.join(native)} use native SQL (Value.NativeQuery): if Power BI Desktop blocks it, approve the "
             "query or untick Options > Security > 'Require user approval for new native database queries'."
             if native else " Every table reads its database table directly (no native SQL to approve)."), ""]

    o += ["## Step 5 - Validate against MicroStrategy", ""]
    for chk in bs["validation"]:
        o += [f"### {chk['title']} (`{chk['visual']}`) on page '{chk['page']}'", "",
              f"Filters: {', '.join(chk['filtersApplied']) or 'none'}. "
              f"Expected set complete: {chk['expectedIsComplete']}.", "",
              "```dax", chk["dax"], "```", ""]
        if chk["expected"]:
            cols = list(chk["expected"][0].keys())
            o += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            o += ["| " + " | ".join(str(r.get(c)) for c in cols) + " |" for r in chk["expected"]]
            o.append("")

    o += ["## Step 6 - Report", ""]
    for f in report.get("filters", []):
        o.append(f"- **Report filter** (MicroStrategy cube filter) on `{f['field']['table']}[{f['field']['column']}]`: "
                 f"{f['defaultSelection']['mode']} {f['defaultSelection']['values']}")
    if report.get("filters"):
        o.append("")
    for pg in report["pages"]:
        o += [f"### Page '{pg['name']}' ({pg['width']:g}x{pg['height']:g})", ""]
        if pg.get("referencePdfPage"):
            o += [f"MicroStrategy rendering to match: `1_extract/raw/dashboard.pdf`, page {pg['referencePdfPage']}.", ""]
        for f in pg.get("filters", []):
            ds = f["defaultSelection"]
            o.append(f"- **Page filter '{f['title']}'** (MicroStrategy chapter filter) on "
                     f"`{f['field']['table']}[{f['field']['column']}]`: "
                     f"{ds['mode'] + ' ' + str(ds['values']) if ds else 'nothing selected'}, "
                     f"require single selection = {f['singleSelect']}")
        for s in pg["slicers"]:
            ds = s["defaultSelection"]
            o += [f"- **Slicer '{s['title']}'** on `{s['field']['table']}[{s['field']['column']}]`: style "
                  f"{s['style']}, single select = {s['singleSelect']}, Select all = {s['showSelectAll']}, "
                  f"default = {ds['mode'] + ' ' + str(ds['values']) if ds else 'none'}, position {s['position']}",
                  f"  - Filters visuals {s['appliesTo']}"
                  + (f"; turn OFF interaction with {s['doesNotApplyTo']}" if s["doesNotApplyTo"] else "")]
            o += [f"  - Note: {n}" for n in s["notes"]]
        for v in pg["visuals"]:
            o.append(f"- **{v['visualType']} '{v['title']}'** (MicroStrategy `{v['mstrType']}`), position {v['position']}")
            for role, refs in v["fields"].items():
                names = ", ".join(f"{r['table']}[{r.get('column') or r.get('measure')}]"
                                  + (" (measure)" if "measure" in r else "") for r in refs)
                o.append(f"  - {role}: {names}")
            if v.get("seriesColors"):
                o.append("  - Legend colours: " + ", ".join(f"{c['value']} {c['color']}" for c in v["seriesColors"]))
            if v.get("measureColors"):
                o.append("  - Measure colours: " + ", ".join(f"{c['measure']} {c['color']}" for c in v["measureColors"]))
            shown = [k for k in ("legend", "dataLabels") if v.get(k)]
            if shown:
                o.append(f"  - Shown: {', '.join(shown)}" + (f", legend at {v['legendPosition']}"
                                                             if v.get("legendPosition") else ""))
            if v.get("sort"):
                o.append("  - Sorted by: " + ", ".join(r["column"] for r in v["sort"]) + " (ascending)")
            o += [f"  - Note: {n}" for n in v["notes"]]
        o.append("")

    o += ["## Open issues and assumptions", "", "| Severity | Where | Message |", "|---|---|---|"]
    order = {"error": 0, "warning": 1, "info": 2}
    o += [f"| {i['severity']} | {i['where']} | {i['message']} |"
          for i in sorted(bs["issues"], key=lambda i: order.get(i["severity"], 3))]
    path.write_text("\n".join(o) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard_id")
    ap.add_argument("--source-type", choices=sorted(SOURCES), default=os.getenv("PBI_SOURCE_TYPE") or None)
    ap.add_argument("--server", default=os.getenv("PBI_SOURCE_SERVER"))
    ap.add_argument("--database", default=os.getenv("PBI_SOURCE_DATABASE"))
    ap.add_argument("--warehouse", default=os.getenv("PBI_SOURCE_WAREHOUSE"))
    args = ap.parse_args()
    if not args.source_type:
        ap.error("source type unknown: pass --source-type or set PBI_SOURCE_TYPE in .env")

    out_dir = extract_dir(args.dashboard_id)
    spec_file = out_dir / "migration_spec.json"
    if not spec_file.exists():
        ap.error(f"{spec_file} not found - run extract_dashboard_metadata.py first")
    spec = json.loads(spec_file.read_text(encoding="utf-8"))

    issues = Issues()
    if spec.get("prompts"):
        issues.add("warning", "dashboard", f"Dashboard has {len(spec['prompts'])} prompt(s); recreate them as "
                                           "slicers or Power Query parameters.")
    conn_values = {"SourceServer": args.server, "SourceDatabase": args.database,
                   "SourceWarehouse": args.warehouse}
    model, lk = build_semantic_model(spec, args.source_type, conn_values, issues)
    ov_file = overrides_file(args.dashboard_id)
    overrides = json.loads(ov_file.read_text(encoding="utf-8")) if ov_file.exists() else {}
    report = build_report(spec, lk, issues, overrides)
    validation = build_validation(out_dir, report, lk, issues)
    for gap in spec.get("knownGaps", []):
        issues.add("info", "extraction", gap)

    build_spec = {
        "specVersion": "1.0",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "source": spec["source"],
        "target": {"sourceType": args.source_type, "reportName": report["name"]},
        "semanticModel": model,
        "report": report,
        "validation": validation,
        "issues": issues,
    }
    pbi_dir = transform_dir(args.dashboard_id)
    pbi_dir.mkdir(parents=True, exist_ok=True)
    (pbi_dir / "powerbi_build_spec.json").write_text(
        json.dumps(build_spec, indent=2, ensure_ascii=False), encoding="utf-8")
    write_build_plan(build_spec, pbi_dir / "BUILD_PLAN.md")

    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in ("error", "warning", "info")}
    print(f"Tables: {len(model['tables'])} | Pages: {len(report['pages'])} | "
          f"Visuals: {sum(len(p['visuals']) for p in report['pages'])} | "
          f"Slicers: {sum(len(p['slicers']) for p in report['pages'])} | Validation checks: {len(validation)}")
    print(f"Issues: {counts['error']} error, {counts['warning']} warning, {counts['info']} info")
    print(f"Written to {pbi_dir}")


if __name__ == "__main__":
    main()
