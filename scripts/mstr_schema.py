"""Schema-based MicroStrategy datasets: Intelligent Cubes and reports built on the project schema.

Unlike Data Import cubes, their SQL can be multi-pass (temporary tables, then a final SELECT) and metric columns get
generated names (WJXBFS1, ...). This module makes the SQL one query and works out which column holds which metric,
from the modeling API's metric, fact and transformation definitions:
  base metric  = aggregation of a fact, optionally shifted by a transformation (LY, LM...) or at a fixed level
  SQL column   = aggregation of the fact's expression in a pass; the pass's time join tells the transformation
                 (e.g. a join on id_tm_yyyymm_ly) and its GROUP BY tells the level
  compound     = arithmetic of other metrics, e.g. {SQM (Occupied)} / {SQM}
star_layout() reads the same SQL as a star schema: the aggregated tables are facts, a join on a lookup table's key is
a relationship, a join on a transformation column (id_tm_yyyymm_ly) is a time shift, and the WHERE is the cube filter.
"""
import re

import sqlglot
from sqlglot import exp

ARITHMETIC = {"+": 1, "-": 1, "*": 2, "/": 2}  # operator -> precedence
TEMP_TABLE = re.compile(r"create\s+(?:temporary\s+|temp\s+|volatile\s+)?table\s+(\w+)\s+as\s+(.*)", re.I | re.S)
INSERT = re.compile(r"insert\s+into\s+(\w+)\s*(?:\([^)]*\))?\s*(select\b.*)", re.I | re.S)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

def split_passes(sql: str) -> tuple[dict[str, str], str]:
    """({temp table: its SELECT}, final SELECT). MicroStrategy separates the statements with blank lines."""
    passes, final = {}, None
    for stmt in (s.strip() for s in re.split(r"\n\s*\n", sql)):
        if (m := TEMP_TABLE.match(stmt)) or (m := INSERT.match(stmt)):
            passes[m.group(1)] = m.group(2).strip()
        elif re.match(r"(select|with)\b", stmt, re.I):
            final = stmt
    return passes, final or sql


def single_query(sql: str) -> str:
    """Multi-pass SQL as one statement: each temporary table becomes a WITH clause (CTE)."""
    passes, final = split_passes(sql)
    if not passes:
        return sql
    return "with " + ",\n".join(f"{name} as (\n{body}\n)" for name, body in passes.items()) + "\n" + final


def _normal(node: exp.Expression, dialect: str) -> str:
    """Comparable text of an expression: no table qualifiers, no redundant brackets, lower case."""
    node = node.copy()
    for col in node.find_all(exp.Column):
        col.set("table", None)
    for paren in list(node.find_all(exp.Paren)):
        paren.replace(paren.this)
    return node.sql(dialect=dialect).lower()


def normal_sql(text: str, dialect: str) -> str:
    return _normal(sqlglot.parse_one(text, read=dialect), dialect)


def _pass_info(select: exp.Select, dialect: str) -> dict:
    joins = set()
    for join in select.args.get("joins") or []:
        if join.args.get("on"):
            joins |= {c.name.lower() for c in join.args["on"].find_all(exp.Column)}
    group = select.args.get("group")
    return {"joinColumns": joins,
            "groupBy": {c.name.lower() for g in (group.expressions if group else []) for c in g.find_all(exp.Column)},
            "tables": {t.name.lower() for t in select.find_all(exp.Table)},
            "columns": {p.alias_or_name.lower(): _normal(p.unalias(), dialect) for p in select.expressions}}


def column_lineage(sql: str, dialect: str) -> dict[str, dict]:
    """Final SELECT alias (lower case) -> {expr, pass, joinColumns, groupBy, tables} of the pass that computes it."""
    passes, final = split_passes(sql)
    infos = {name.lower(): _pass_info(sqlglot.parse_one(body, read=dialect), dialect) for name, body in passes.items()}
    tree = sqlglot.parse_one(final, read=dialect)
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    aliases = {t.alias_or_name.lower(): t.name.lower() for t in select.find_all(exp.Table)}  # pa11 -> zzmd00
    own = _pass_info(select, dialect)
    lineage = {}
    for proj in select.expressions:
        inner = proj.unalias()
        source = aliases.get(inner.table.lower()) if isinstance(inner, exp.Column) else None
        if source in infos:
            info = infos[source]
            lineage[proj.alias_or_name.lower()] = {**info, "expr": info["columns"].get(inner.name.lower()),
                                                   "pass": source}
        else:
            lineage[proj.alias_or_name.lower()] = {**own, "expr": _normal(inner, dialect), "pass": None}
    return lineage


# ---------------------------------------------------------------------------
# Metric definitions (GET /api/model/metrics/{id}, showExpressionAs=tokens)
# ---------------------------------------------------------------------------

def parse_metric(model: dict) -> dict:
    """{kind: base|compound|other, ...} from a metric's modeling-API definition.
    base: aggregation, factId, levelAttributeIds, transformationIds. compound: formula tree."""
    tokens = model.get("expression", {}).get("tokens", [])
    refs = [t["target"] for t in tokens if t.get("type") == "object_reference" and t.get("target")]
    fact = next((r for r in refs if r.get("subType") == "fact"), None)
    if fact:
        agg = next((t["value"] for t in tokens if t.get("type") == "function"), "")
        units = model.get("dimty", {}).get("dimtyUnits", [])
        return {"kind": "base", "aggregation": agg.lower(), "factId": fact["objectId"],
                "levelAttributeIds": [u["target"]["objectId"] for u in units if u.get("dimtyUnitType") == "attribute"],
                "transformationIds": [u["target"]["objectId"] for u in units
                                      if u.get("dimtyUnitType") == "role"
                                      and u.get("target", {}).get("subType") == "role_transformation"]}
    tree = _infix(tokens)
    return {"kind": "compound", "formula": tree} if tree else {"kind": "other"}


def _infix(tokens: list[dict]) -> dict | None:
    """Expression tree of metric references combined with + - * / and brackets (shunting-yard); None otherwise."""
    out, ops = [], []

    def reduce() -> None:
        op = ops.pop()
        right, left = out.pop(), out.pop()
        out.append({"function": op, "args": [left, right]})

    try:
        for t in tokens:
            value, kind = (t.get("value") or "").strip(), t.get("type")
            if kind == "object_reference" and (t.get("target") or {}).get("subType") == "metric":
                out.append({"metric": t["target"]["objectId"]})
            elif value in ARITHMETIC:
                while ops and ops[-1] in ARITHMETIC and ARITHMETIC[ops[-1]] >= ARITHMETIC[value]:
                    reduce()
                ops.append(value)
            elif value == "(":
                ops.append(value)
            elif value == ")":
                while ops[-1] != "(":
                    reduce()
                ops.pop()
            elif kind == "end_of_text" or not value:
                continue
            else:
                return None  # functions, constants, levels...: not rebuilt
        while ops:
            reduce()
    except (IndexError, KeyError):
        return None
    return out[0] if len(out) == 1 and "(" not in ops else None


APPLY = re.compile(r'Apply(?:Simple|Agg|Comparison|Logic)\s*\(\s*"((?:[^"]|"")*)"\s*,(.*)\)\s*$', re.S | re.I)


def expand_apply(expr: str) -> str:
    """MicroStrategy pass-through expression as plain SQL:
    ApplySimple("CASE WHEN #0 IN (2,3) THEN 0 ELSE #1 END", a, b) -> CASE WHEN a IN (2,3) THEN 0 ELSE b END."""
    m = APPLY.match(expr.strip())
    if not m:
        return expr
    args, depth, current = [], 0, ""
    for ch in m.group(2):
        if ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
            continue
        depth += (ch == "(") - (ch == ")")
        current += ch
    args.append(current.strip())
    return re.sub(r"#(\d+)", lambda g: args[int(g.group(1))] if int(g.group(1)) < len(args) else g.group(0),
                  m.group(1).replace('""', '"'))


def _aggregated(aggregation: str, expressions: list[str], dialect: str) -> set[str]:
    out = set()
    for e in expressions:
        try:
            out.add(normal_sql(f"{aggregation}({expand_apply(e)})", dialect))
        except sqlglot.errors.ParseError:
            continue
    return out


# ---------------------------------------------------------------------------
# Star schema: tables, joins and filters of the SQL
# ---------------------------------------------------------------------------

AGGREGATIONS = {exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max", exp.Count: "count"}


def _query(sql: str, dialect: str) -> dict:
    tree = sqlglot.parse_one(sql, read=dialect)
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    aliases = {t.alias_or_name.lower(): t.name.lower() for t in select.find_all(exp.Table)}
    return {"select": select, "aliases": aliases,
            "projections": {p.alias_or_name.lower(): p.unalias() for p in select.expressions}}


def _sources(node: exp.Expression, query: dict, passes: dict) -> set[tuple[str, str]]:
    """(table, column) pairs an expression reads; a pass (temporary table) column is followed to its table."""
    out = set()
    only = next(iter(query["aliases"].values())) if len(query["aliases"]) == 1 else None
    for col in node.find_all(exp.Column):
        source = query["aliases"].get(col.table.lower()) if col.table else only
        if source in passes:
            inner = passes[source]["projections"].get(col.name.lower())
            if isinstance(inner, exp.Column):
                out |= _sources(inner, passes[source], {})
        elif source:
            out.add((source, col.name.lower()))
    return out


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _literal(node: exp.Expression):
    if not isinstance(node, exp.Literal):
        raise ValueError(node.sql())
    if node.is_string:
        return node.this
    return float(node.this) if re.search(r"[.eE]", node.this) else int(node.this)


def _filter(cond: exp.Expression, query: dict, dialect: str) -> dict:
    """WHERE condition -> {table, column, values} for 'col IN (...)' and 'col = value', else {sql}."""
    try:
        if isinstance(cond, exp.In) and isinstance(cond.this, exp.Column) and cond.expressions:
            column, values = cond.this, [_literal(e) for e in cond.expressions]
        elif isinstance(cond, exp.EQ) and isinstance(cond.left, exp.Column):
            column, values = cond.left, [_literal(cond.right)]
        else:
            raise ValueError(cond.sql())
        (table, name), = _sources(column, query, {})
        return {"table": table, "column": name, "values": values}
    except ValueError:
        return {"sql": cond.sql(dialect=dialect)}


def star_layout(sql: str, dialect: str, keys: dict[str, str], shifting: set[str]) -> dict:
    """The SQL as a star schema.
    keys: lookup table -> its key column (from the table's key attribute); shifting: transformation columns.
    Returns edges [{from: [table, column], to: [lookup table, key]}] (many to one), the joins that are not on a key,
    and per final SELECT alias computed by an aggregate: {table (the fact), aggregation, expression (SQL without
    table qualifiers), pass, shift {table, column, factTable, factColumn} or None, groupBy, filters}."""
    passes_sql, final_sql = split_passes(sql)
    passes = {name.lower(): _query(body, dialect) for name, body in passes_sql.items()}
    final = _query(final_sql, dialect)
    edges, other, shifts = set(), set(), {}
    for name, q in [*passes.items(), (None, final)]:
        conditions = [c for j in q["select"].args.get("joins") or [] for c in _conjuncts(j.args.get("on"))]
        conditions += _conjuncts((q["select"].args.get("where") or exp.Where()).this)  # implicit joins
        for cond in conditions:
            if not isinstance(cond, exp.EQ) or not cond.left.find(exp.Column) or not cond.right.find(exp.Column):
                continue
            for t1, c1 in _sources(cond.left, q, passes):
                for t2, c2 in _sources(cond.right, q, passes):
                    if t1 == t2:
                        continue
                    if c2 in shifting or c1 in shifting:
                        (dim, shifted), (fact, column) = ((t2, c2), (t1, c1)) if c2 in shifting else ((t1, c1), (t2, c2))
                        shifts.setdefault(name, []).append({"table": dim, "column": shifted, "factTable": fact,
                                                            "factColumn": column})
                    elif keys.get(t2) == c2:
                        edges.add((t1, c1, t2, c2))
                    elif keys.get(t1) == c1:
                        edges.add((t2, c2, t1, c1))
                    else:
                        other.add((t1, c1, t2, c2))

    measures = {}
    for alias, node in final["projections"].items():
        q, name = final, None
        if isinstance(node, exp.Column) and final["aliases"].get(node.table.lower()) in passes:
            name = final["aliases"][node.table.lower()]
            q, node = passes[name], passes[name]["projections"].get(node.name.lower())
        agg = next((a for kind, a in AGGREGATIONS.items() if isinstance(node, kind)), None)
        if not agg:
            continue
        inner = node.this
        if isinstance(inner, exp.Distinct):
            agg, inner = "distinct count", inner.expressions[0]
        tables = {t for t, _ in _sources(inner, q, {})}
        if len(tables) != 1:
            continue
        fact = tables.pop()
        plain = inner.copy()
        for col in plain.find_all(exp.Column):
            col.set("table", None)
        group = q["select"].args.get("group")
        measures[alias] = {
            "table": fact, "aggregation": agg, "expression": plain.sql(dialect=dialect), "pass": name,
            "shift": next((s for s in shifts.get(name, []) if s["factTable"] == fact), None),
            "groupBy": sorted({p for g in (group.expressions if group else []) for p in _sources(g, q, {})}),
            "filters": [_filter(c, q, dialect) for c in _conjuncts((q["select"].args.get("where") or exp.Where()).this)
                        if not (isinstance(c, exp.EQ) and c.left.find(exp.Column) and c.right.find(exp.Column))]}
    return {"edges": [{"from": [t1, c1], "to": [t2, c2]} for t1, c1, t2, c2 in sorted(edges)],
            "otherJoins": [{"left": [t1, c1], "right": [t2, c2]} for t1, c1, t2, c2 in sorted(other)],
            "measures": measures}


def match_metric_columns(lineage: dict[str, dict], base: list[dict], dialect: str) -> dict[str, str]:
    """Base metric id -> SQL alias. base items: {id, aggregation, factExpressions, factTables, transformationColumns,
    levelColumns} (column and table names lower case)."""
    shifting = set().union(*(m["transformationColumns"] for m in base)) if base else set()
    scored = []
    for m in base:
        wanted = _aggregated(m["aggregation"], m["factExpressions"], dialect)
        for alias, info in lineage.items():
            if info["expr"] not in wanted:
                continue
            shift = info["joinColumns"] & shifting
            if (m["transformationColumns"] and not info["joinColumns"] & m["transformationColumns"]) or \
                    (not m["transformationColumns"] and shift):
                continue
            if m["factTables"] and info["tables"] and not info["tables"] & m["factTables"]:
                continue
            if m["levelColumns"]:
                score = 10 if info["groupBy"] == m["levelColumns"] else 0
            else:
                score = len(info["groupBy"])  # report level: the most detailed pass
            scored.append((score, m["id"], alias))
    mapping, used = {}, set()
    for _, mid, alias in sorted(scored, key=lambda s: -s[0]):
        if mid not in mapping and alias not in used:
            mapping[mid] = alias
            used.add(alias)
    return mapping
