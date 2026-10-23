"""Dump the full metadata of a MicroStrategy dashboard (dossier) and its datasets.

Read-only: the only write is a temporary dashboard instance, deleted at the end.
Outputs under 2_work/<ID>/1_extract/:
  raw/*.json            every REST response, untouched
  raw/dashboard.mstr    the dashboard file export, read by mstr_file.py for colours, legend and data labels
  raw/dashboard.pdf     every page as MicroStrategy draws it (one PDF page per dashboard page): the reference
                        for comparing the Power BI report
  _calls.json           endpoint log with HTTP status codes
  migration_spec.json   condensed definition (layout, visuals, selectors, datasets, SQL)
  migration_spec.md     same content, human/LLM readable

Usage:
  .venv\\Scripts\\python.exe scripts\\extract_dashboard_metadata.py <DASHBOARD_ID> [--project-id <ID>]
"""
import argparse
import base64
import json
import re
import shutil
import time
from pathlib import Path

import mstr_file
import mstr_schema
from fsutil import mirror, staging_dir
from mstr_connection import get_connection
from paths import extract_dir

VIZ_SAMPLE_ROWS = 20  # rows of data to keep per visualization (definition is the goal)
# Graph-matrix visualizations: the ones with a chart property set in the .mstr export
GRAPH_TYPES = {"bar_chart", "horizontal_bar_chart", "line_chart", "area_chart", "pie_chart", "ring_chart",
               "bubble_chart", "scatter_chart", "combo_chart", "dual_axis_chart", "histogram"}
ARITHMETIC = {"+", "-", "*", "/"}  # MicroStrategy function names that derived-metric formulas are rebuilt from
# Reference rendering: each dashboard page on one landscape PDF page, with the filter bar MicroStrategy shows.
PDF_OPTIONS = {"pageOption": "ALL", "includeOverview": True, "includeDetailedPages": False, "includeToc": False,
               "includeHeader": True, "includeFooter": False, "filterSummary": "BAR", "gridPagingMode": "none",
               "pageWidth": 16, "pageHeight": 10, "marginTop": 0.2, "marginBottom": 0.2,
               "marginLeft": 0.2, "marginRight": 0.2}
PDF_POLLS = 30  # x 2 s when the export runs asynchronously

CUBE_SUBTYPES = {776: "Intelligent Cube (OLAP)", 779: "Super Cube"}
CUBE_EXT_TYPES = {256: "Data Import"}  # anything else: built on the project schema
DATA_IMPORT = 256
SQL_ENGINE_MARKER = "[Analytical engine calculation steps:"


class Dumper:
    def __init__(self, conn, out_dir: Path):
        self.conn = conn
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.calls = []
        self.results = {}  # name -> parsed body for successful calls
        self.errors = {}   # name -> MicroStrategy error message for failed calls

    def call(self, name: str, method: str, endpoint: str, **kwargs):
        """Call an endpoint, save the body to raw/<name>.json, return parsed JSON or None."""
        resp = getattr(self.conn, method.lower())(endpoint=endpoint, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {"_non_json_body": resp.text}
        safe_name = re.sub(r"[^\w.-]+", "_", name)
        (self.raw_dir / f"{safe_name}.json").write_text(
            json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.calls.append({"name": safe_name, "method": method, "endpoint": endpoint,
                           "params": kwargs.get("params"), "status": resp.status_code})
        print(f"  [{resp.status_code}] {method} {endpoint}")
        if resp.ok:
            self.results[safe_name] = body
            return body
        message = body.get("message") if isinstance(body, dict) else None
        self.errors[safe_name] = f"HTTP {resp.status_code}: {message or resp.text[:200]}"
        return None

    def export_pdf(self, did: str, mid: str) -> Path | None:
        """Render every dashboard page to raw/dashboard.pdf (one PDF page per dashboard page)."""
        endpoint = f"/api/documents/{did}/instances/{mid}/pdf"
        resp = self.conn.post(endpoint=endpoint, json=PDF_OPTIONS)
        if resp.status_code == 202:  # large dashboards render asynchronously
            result = f"{endpoint}/results/{resp.json().get('id')}"
            for _ in range(PDF_POLLS):
                time.sleep(2)
                resp = self.conn.get(endpoint=result)
                if resp.status_code != 202:
                    break
        self.calls.append({"name": "dashboard_pdf", "method": "POST", "endpoint": endpoint,
                           "params": None, "status": resp.status_code})
        print(f"  [{resp.status_code}] POST {endpoint}")
        try:
            data = resp.json().get("data") if resp.ok else None
        except ValueError:
            data = None
        if not data:
            self.errors["dashboard_pdf"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return None
        path = self.raw_dir / "dashboard.pdf"
        path.write_bytes(base64.b64decode(data if isinstance(data, str) else "".join(data)))
        return path

    def download(self, name: str, filename: str, method: str, endpoint: str, **kwargs) -> Path | None:
        """Call an endpoint that returns a file, save it to raw/<filename>, return its path or None."""
        resp = getattr(self.conn, method.lower())(endpoint=endpoint, **kwargs)
        self.calls.append({"name": name, "method": method, "endpoint": endpoint,
                           "params": kwargs.get("params"), "status": resp.status_code})
        print(f"  [{resp.status_code}] {method} {endpoint}")
        if not resp.ok:
            self.errors[name] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return None
        path = self.raw_dir / filename
        path.write_bytes(resp.content)
        return path


def find_ids(obj, predicate) -> set[str]:
    """Recursively collect 'id'/'objectId' values from dicts matching predicate."""
    found = set()
    if isinstance(obj, dict):
        if predicate(obj):
            oid = obj.get("objectId") or obj.get("id")
            if isinstance(oid, str):
                found.add(oid)
        for v in obj.values():
            found |= find_ids(v, predicate)
    elif isinstance(obj, list):
        for v in obj:
            found |= find_ids(v, predicate)
    return found


def document_as_dossier(defn: dict) -> dict:
    """A Report Services document's layouts in the dossier shape: one chapter and page per layout, holding every
    grid/graph and attribute selector of the layout (panel stacks flattened)."""
    chapters = []
    for lay in defn.get("layouts", []):
        visuals, selectors, notes = [], [], []

        def walk(node: dict, path: str) -> None:
            visuals.extend({"key": v["key"], "name": v.get("name"), "visualizationType": v.get("visualizationType")}
                           for v in node.get("visualizations") or [])
            for s in node.get("selectors") or []:
                if s.get("selectorType") == "panel_selector":
                    if s.get("availablePanels"):
                        notes.append(f"Panel selector '{s.get('name')}' switches panels {s['availablePanels']}: use "
                                     "Power BI bookmarks or pages.")
                else:
                    selectors.append(s)
            for stack in node.get("panelStacks") or []:
                panels = stack.get("panels") or []
                if len(panels) > 1:
                    notes.append(f"Panel stack '{stack.get('name')}' in {path} has {len(panels)} panels; their visuals "
                                 "are all placed on the page. Split them into bookmarks or pages.")
                for panel in panels:
                    walk(panel, f"{path}/{stack.get('name')}")

        walk(lay, lay.get("name") or lay["key"])
        if visuals or selectors:
            # Layouts keep default names ('Layout 1'): the document's name is the better page name.
            name = lay.get("name") or defn.get("name")
            chapter = defn.get("name") if re.match(r"^layout\s*\d*$", name or "", re.I) else name
            chapters.append({"key": lay["key"], "name": chapter, "filters": [], "notes": notes,
                             "pages": [{"key": lay["key"], "name": name, "visualizations": visuals,
                                        "selectors": selectors}]})
    return {"id": defn.get("id"), "name": defn.get("name"), "datasets": defn.get("datasets", []),
            "chapters": chapters}


def _model(d: "Dumper", kind: str, oid: str, **params) -> dict:
    """Modeling-API definition (kind: metric, fact, transformation, attribute), fetched once."""
    name = f"model_{kind}_{oid}"
    return d.results.get(name) or d.call(name, "GET", f"/api/model/{kind}s/{oid}", params=params or None) or {}


def schema_model(d: "Dumper", attrs: list[dict], metrics: list[dict], sql: str | None,
                 cube_attrs: list[dict]) -> dict:
    """What the transform needs to map a schema cube's SQL columns: per metric its kind (base: aggregation, fact
    expressions and tables, transformation join columns, level columns; compound: formula), and per attribute form
    its column expressions. For the star schema: the tables the SQL reads (schema, columns, key attribute) and every
    attribute of the cube or keying one of those tables (lookup table, column per table)."""
    found, queue, table_ids = {}, [m["id"] for m in metrics], {}
    while queue:
        mid = queue.pop(0)
        if mid in found:
            continue
        model = _model(d, "metric", mid, showExpressionAs="tokens")
        if not model:
            continue
        info = {"name": model.get("information", {}).get("name"), **mstr_schema.parse_metric(model)}
        if info["kind"] == "compound":
            queue += _formula_ids(info["formula"])
        elif info["kind"] == "base":
            fact = _model(d, "fact", info["factId"])
            exprs = fact.get("expressions", [])
            info["factName"] = fact.get("information", {}).get("name")
            table_ids.update({t["name"].lower(): t["objectId"] for e in exprs for t in e.get("tables", [])})
            info["factExpressions"] = [e["expression"]["text"] for e in exprs]
            info["factTables"] = sorted({t["name"].lower() for e in exprs for t in e.get("tables", [])})
            info["transformationColumns"] = sorted(
                {f["expression"]["text"].lower() for tid in info["transformationIds"]
                 for a in _model(d, "transformation", tid).get("attributes", []) for f in a.get("forms", [])})
            info["levelColumns"] = sorted(
                {e["expression"]["text"].lower() for aid in info["levelAttributeIds"]
                 for f in _model(d, "attribute", aid).get("forms", []) if f.get("category") == "ID"
                 for e in f.get("expressions", [])})
        found[mid] = info
    forms = {a["id"]: {f["id"]: [e["expression"]["text"] for e in f.get("expressions", [])]
                       for f in _model(d, "attribute", a["id"]).get("forms", [])} for a in attrs}

    attributes = {}
    for aid in dict.fromkeys(a["id"] for a in attrs + cube_attrs):
        attributes[aid] = _attribute_info(_model(d, "attribute", aid))
        table_ids.update(attributes[aid].pop("tableIds"))
    in_sql = {w.lower() for w in re.findall(r"\w+", sql or "")}
    tables = {}
    for name, tid in sorted(table_ids.items()):
        if name not in in_sql:
            continue  # e.g. other fact tables an attribute is also in
        tables[name] = _table_info(_model(d, "table", tid))
        for aid in tables[name]["keyAttributeIds"]:
            if aid not in attributes:
                attributes[aid] = _attribute_info(_model(d, "attribute", aid))
                attributes[aid].pop("tableIds")
    return {"metrics": found, "attributeForms": forms, "attributes": attributes, "tables": tables}


def _attribute_info(model: dict) -> dict:
    """Attribute definition for the star schema: its lookup table and, per form, the column in each table."""
    lower = lambda ref: (ref or {}).get("name", "").lower() or None
    forms, table_ids = [], {}
    for f in model.get("forms", []):
        exprs = {}
        for e in f.get("expressions", []):
            for t in e.get("tables", []):
                exprs[t["name"].lower()] = e["expression"]["text"]
                table_ids[t["name"].lower()] = t["objectId"]
        forms.append({"id": f.get("id"), "name": f.get("name"), "category": f.get("category"),
                      "dataType": (f.get("dataType") or {}).get("type"),
                      "lookupTable": lower(f.get("lookupTable")), "expressions": exprs})
    return {"name": model.get("information", {}).get("name"), "lookupTable": lower(model.get("attributeLookupTable")),
            "forms": forms, "tableIds": table_ids}


def _table_info(model: dict) -> dict:
    """Logical table: physical schema and name (or SQL of a logical view), column types, key attribute(s)."""
    pt = model.get("physicalTable") or {}
    return {"id": model.get("information", {}).get("objectId"), "namespace": pt.get("namespace") or None,
            "tableName": pt.get("tableName") or model.get("information", {}).get("name"),
            "tablePrefix": pt.get("tablePrefix") or "", "type": pt.get("type"),
            "sqlExpression": (pt.get("sqlExpression") or {}).get("text") if isinstance(pt.get("sqlExpression"), dict)
            else pt.get("sqlExpression"),
            "columns": {c["columnName"].lower(): {"name": c["columnName"], "dataType": (c.get("dataType") or {}).get("type")}
                        for c in pt.get("columns", []) if c.get("columnName")},
            "keyAttributeIds": [k["objectId"] for k in model.get("tableKey") or []]}


def _formula_ids(node: dict) -> list[str]:
    return [node["metric"]] if "metric" in node else [m for a in node["args"] for m in _formula_ids(a)]


def displayed_selection(d: "Dumper", selection: dict | None, attr_id: str | None, cube_id: str | None) -> dict | None:
    """Selection with element names as users see them. Document selectors name elements by their ID ('201806'
    for ' 2018 JUNE'): those are looked up in the cube's element list."""
    elements = (selection or {}).get("elements") or []
    keys = [(e.get("id") or "").split(";")[0][1:] for e in elements]
    if not (cube_id and attr_id) or not any(e.get("name") == k for e, k in zip(elements, keys)):
        return selection
    name = f"elements_{cube_id}_{attr_id}"
    listed = d.results.get(name) or d.call(name, "GET", f"/api/cubes/{cube_id}/attributes/{attr_id}/elements",
                                           params={"limit": 5000}) or []
    display = {el["id"].split(":", 1)[-1]: (el.get("formValues") or [None])[0] for el in listed}
    return {**selection, "elements": [{**e, "name": display.get(k) or e.get("name")} for e, k in zip(elements, keys)]}


def _objects(available: list[dict]) -> dict:
    """Flat availableObjects list (document dataset) -> {attributes, metrics}."""
    return {"attributes": [o for o in available if o.get("type") == "attribute"],
            "metrics": [o for o in available if o.get("type") == "metric"]}


def is_table(d: dict) -> bool:
    t = str(d.get("type", "")).lower()
    st = str(d.get("subType", "")).lower()
    return t == "table" or "logical_table" in st or st == "table"


# ---------------------------------------------------------------------------
# Condensed migration spec
# ---------------------------------------------------------------------------

def _forms(obj: dict) -> list[dict]:
    return [{"id": f.get("id"), "name": f.get("name"), "category": f.get("baseFormCategory"),
             "dataType": f.get("dataType")} for f in obj.get("forms", [])]


def _template_units(units: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split grid axis units into (attributes, metrics), dropping element data."""
    attrs, metrics = [], []
    for u in units:
        if u.get("type") == "templateMetrics":
            for m in u.get("elements", []):
                metrics.append({"id": m.get("id"), "name": m.get("name"),
                                "dataType": m.get("dataType"),
                                "derived": bool(m.get("derived")),
                                "numberFormat": m.get("numberFormatting"),
                                "min": m.get("min"), "max": m.get("max")})
        else:
            attrs.append({"id": u.get("id"), "name": u.get("name"), "type": u.get("type"),
                          "formsShown": _forms(u)})
    return attrs, metrics


def _split_sql(sql_view: dict | None) -> tuple[str | None, str | None]:
    text = (sql_view or {}).get("sqlStatement")
    if not text:
        return None, None
    sql, _, engine = text.partition(SQL_ENGINE_MARKER)
    engine = engine.strip().rstrip("]").strip()
    return sql.strip(), (engine or None)


def _element_values(units: list[dict]) -> dict[tuple[str, str], str]:
    """(attribute id, element key) -> displayed value, from REST element ids like 'h142;<attribute id>'."""
    values = {}
    for u in units:
        if u.get("type") != "attribute":
            continue
        for e in u.get("elements", []):
            key, _, attr_id = (e.get("id") or "").partition(";")
            if key.startswith("h") and e.get("formValues"):
                values[(attr_id or u["id"], key[1:])] = e["formValues"][0]
    return values


def _mstr_file_details(mstr_path: Path) -> tuple[bytes, list[dict], list[dict], dict]:
    """(definition bytes, chart property sets, custom colours, page layouts) from the .mstr export, or empty."""
    if not mstr_path.exists():
        return b"", [], [], {}
    try:
        data = mstr_file.read_definition(mstr_path)
        return (data, mstr_file.chart_properties(data), mstr_file.element_colors(data),
                mstr_file.page_layouts(data))
    except Exception as e:  # undocumented format: never fail the extract because of it
        print(f"  could not read {mstr_path.name}: {e}")
        return b"", [], [], {}


def _formula(tokens: list[str], metric_ids: set[str], function_name) -> dict | None:
    """Expression tree from post-order tokens; None unless every function is + - * / with two operands."""
    stack = []
    for t in tokens:
        if t in metric_ids:
            stack.append({"metric": t})
            continue
        fn = function_name(t)
        if fn not in ARITHMETIC or len(stack) < 2:
            return None
        right, left = stack.pop(), stack.pop()
        stack.append({"function": fn, "args": [left, right]})
    return stack[0] if len(stack) == 1 else None


def _formula_text(node: dict, names: dict[str, str]) -> str:
    if "metric" in node:
        return f"[{names.get(node['metric'], node['metric'])}]"
    left, right = (_formula_text(a, names) for a in node["args"])
    return f"({left} {node['function']} {right})"


def derived_metrics(d: Dumper, data: bytes, visual_metrics: list[dict], dataset_metrics: dict[str, str]) -> list[dict]:
    """Metrics created in the dashboard: formula from the .mstr export, function names checked on the server."""
    derived = {m["id"]: m for m in visual_metrics if m.get("derived")}
    names = {**dataset_metrics, **{i: m["name"] for i, m in derived.items()}}

    def function_name(oid: str) -> str | None:
        obj = d.results.get(f"function_{oid}") or d.call(f"function_{oid}", "GET", f"/api/objects/{oid}",
                                                         params={"type": 11})
        return (obj or {}).get("name")

    out = []
    for mid, m in derived.items():
        tokens = mstr_file.derived_metric_tokens(data, mid, m["name"]) if data else []
        tree = _formula(tokens, set(names), function_name) if tokens else None
        out.append({"id": mid, "name": m["name"], "dataType": m.get("dataType"), "formula": tree,
                    "formulaText": _formula_text(tree, names) if tree else None, "tokens": tokens,
                    "source": ".mstr export" if tree else None})
    return out


def _display(props: dict) -> dict:
    return {"shape": props.get("Shape"), "subtype": props.get("Subtype"),
            "legend": props.get("LegendVisibleness") == "Show", "legendPosition": props.get("LegendPosition"),
            "dataLabels": (props.get("DataLabel") or "").startswith("Show"),
            "nestedLabels": props.get("xAxLbNestedLabel") == "true", "source": ".mstr export"}


def build_spec(d: Dumper, did: str, project_id: str) -> dict:
    r = d.results
    obj = r.get("dashboard_object", {})
    definition = r.get("dashboard_instance_definition") or r.get("dashboard_definition") or {}
    is_document = "layouts" in definition
    if is_document:
        definition = document_as_dossier(definition)
    # A new instance can open in the user's last viewed state; the saved definition has the authored selections.
    saved_selectors = {s["key"]: s for ch in r.get("dashboard_definition", {}).get("chapters", [])
                       for pg in ch.get("pages", []) for s in pg.get("selectors", [])}
    mstr_data, chart_props, colors, layouts = _mstr_file_details(d.raw_dir / "dashboard.mstr")
    metric_colors = {c["objectId"]: c["color"] for c in colors if c["elementKey"] is None}

    datasets, object_to_dataset = [], {}
    for ds in definition.get("datasets", []):
        dsid = ds["id"]
        # A report dataset reads a cube: its data comes from that cube, its objects are the report's own subset.
        cid = ((r.get(f"report_{dsid}") or {}).get("source") or {}).get("id") or dsid
        cube_obj = r.get(f"cube_{cid}_object", {})
        if cid == dsid:
            avail = r.get(f"cube_{cid}_v2", {}).get("definition", {}).get("availableObjects", {})
        else:
            avail = _objects(ds.get("availableObjects", []))
        sql, engine_steps = _split_sql(r.get(f"cube_{cid}_sqlview"))
        attrs = [{"id": a["id"], "name": a["name"], "forms": _forms(a)}
                 for a in avail.get("attributes", [])]
        metrics = [{"id": m["id"], "name": m["name"], "dataType": m.get("dataType")}
                   for m in avail.get("metrics", [])]
        for o in attrs + metrics:
            object_to_dataset.setdefault(o["id"], set()).add(dsid)
        schema_based = cube_obj and cube_obj.get("extType") != DATA_IMPORT
        datasets.append({
            "id": dsid,
            "name": ds.get("name"),
            "sourceCube": {"id": cid, "name": cube_obj.get("name")} if cid != dsid else None,
            "cubeType": CUBE_SUBTYPES.get(cube_obj.get("subtype"), cube_obj.get("subtype")),
            "sourceType": CUBE_EXT_TYPES.get(cube_obj.get("extType"), "Project schema"),
            "path": "\\".join(a["name"] for a in sorted(cube_obj.get("ancestors", []),
                                                         key=lambda a: -a["level"])),
            "attributes": attrs,
            "metrics": metrics,
            "derivedElements": {k: avail.get(k) for k in ("customGroups", "consolidations")
                                if avail.get(k)},
            "sourceSql": sql,
            "analyticalEngineSteps": engine_steps,
            # Schema cubes: generated column names (WJXBFS1...) mapped through the modeling API in the transform
            "schema": schema_model(d, attrs, metrics, sql, r.get(f"cube_{cid}_v2", {}).get("definition", {})
                                   .get("availableObjects", {}).get("attributes", [])) if schema_based else None,
        })

    # Cube holding each attribute, to name selected elements as users see them
    cube_of_dataset = {ds["id"]: (ds["sourceCube"] or {}).get("id") or ds["id"] for ds in datasets}
    cube_of_attr = {o: cube_of_dataset[next(iter(dss))] for o, dss in object_to_dataset.items()}

    def shown(selection: dict | None, selector: dict) -> dict | None:
        attr = (selector.get("source") or {}).get("id")
        return displayed_selection(d, selection, attr, cube_of_attr.get(attr))

    chapters, graphs = [], []
    for ch in definition.get("chapters", []):
        pages = []
        for pg in ch.get("pages", []):
            visuals = []
            for viz in pg.get("visualizations", []):
                vdef = r.get(f"viz_{ch['key']}_{viz['key']}", {})
                grid = vdef.get("definition", {}).get("grid", {})
                row_attrs, row_metrics = _template_units(grid.get("rows", []))
                col_attrs, col_metrics = _template_units(grid.get("columns", []))
                used_ids = {u["id"] for u in row_attrs + col_attrs + row_metrics + col_metrics}
                used_ds = sorted(set().union(*(object_to_dataset.get(i, set()) for i in used_ids))
                                 if used_ids else set())
                # Element colours are only editable for an attribute in 'Color By', so an attribute of this
                # visual with custom element colours is taken as its colour-by attribute.
                values = _element_values(grid.get("rows", []) + grid.get("columns", []))
                color_by = None
                for a in col_attrs + row_attrs:
                    matched = [{"value": values[(a["id"], c["elementKey"])], "color": c["color"]}
                               for c in colors if c["objectId"] == a["id"] and (a["id"], c["elementKey"]) in values]
                    if matched:
                        color_by = {"attributeId": a["id"], "attribute": a["name"], "colors": matched,
                                    "source": "inferred from element colours in the .mstr export"}
                        break
                visual = {
                    "key": viz["key"],
                    "name": viz.get("name"),
                    "type": viz.get("visualizationType"),
                    "isGrid": vdef.get("isGrid"),
                    "datasets": used_ds,
                    "rows": row_attrs,
                    "columns": col_attrs,
                    "metrics": row_metrics + col_metrics,
                    "metricsPosition": grid.get("metricsPosition"),
                    "sorting": grid.get("sorting"),
                    "thresholds": grid.get("thresholds"),
                    "colorBy": color_by,
                    "metricColors": [{"metricId": m["id"], "metric": m["name"], "color": metric_colors[m["id"]]}
                                     for m in row_metrics + col_metrics if m["id"] in metric_colors],
                    "display": None,
                    "definitionRetrieved": bool(vdef),
                }
                visuals.append(visual)
                if visual["type"] in GRAPH_TYPES:
                    graphs.append(visual)
            selectors = [{
                "key": s.get("key"),
                "name": s.get("name"),
                "selectorType": s.get("selectorType"),
                "displayStyle": s.get("displayStyle"),
                "source": s.get("source"),
                "targets": [t.get("key") for t in s.get("targets", [])],
                "multiSelect": s.get("multiSelectionAllowed"),
                "hasAllOption": s.get("hasAllOption"),
                "summary": saved_selectors.get(s.get("key"), s).get("summary"),
                "currentSelection": shown(saved_selectors.get(s.get("key"), s).get("currentSelection"), s),
            } for s in pg.get("selectors", [])]
            pages.append({"key": pg["key"], "name": pg.get("name"),
                          "visualizations": visuals, "selectors": selectors,
                          "layout": layouts.get(pg["key"])})  # free-form positions from the .mstr export
        chapters.append({"key": ch["key"], "name": ch.get("name"),
                         "chapterFilters": [{**f, "currentSelection": shown(f.get("currentSelection"), f)}
                                            for f in ch.get("filters", [])],
                         "pages": pages})

    # The PDF export has one page per dashboard page, in chapter/page order.
    gaps, pdf = [n for ch in definition.get("chapters", []) for n in ch.get("notes", [])], d.raw_dir / "dashboard.pdf"
    if is_document:
        gaps.append("Report Services document: text boxes, images, lines and HTML containers are not converted, and "
                    "the API does not say which grids MicroStrategy displays as graphs or where objects sit. Set "
                    "chart types, titles and positions in 1_input/overrides/<ID>.json, using dashboard.pdf.")
    for n, pg in enumerate((pg for ch in chapters for pg in ch["pages"]), start=1):
        pg["referencePdfPage"] = n if pdf.exists() else None
    if not pdf.exists():
        gaps.append(f"No reference rendering: PDF export failed ({d.errors.get('dashboard_pdf', 'not run')}). "
                    "Add a screenshot of each page to compare the Power BI report with.")
    # The .mstr file lists graph property sets in dashboard order; only pair them when the counts agree.
    if len(chart_props) == len(graphs):
        for visual, props in zip(graphs, chart_props):
            visual["display"] = _display(props)
    elif graphs:
        gaps.append(f"Chart settings (legend, data labels, subtype): {len(chart_props)} property set(s) in the "
                    f".mstr export for {len(graphs)} chart(s); not applied.")
    visual_metrics = [m for ch in chapters for pg in ch["pages"] for v in pg["visualizations"] for m in v["metrics"]]
    derived = derived_metrics(d, mstr_data, visual_metrics,
                              {m["id"]: m["name"] for ds in datasets for m in ds["metrics"]})
    gaps += [f"Derived metric '{m['name']}' (created in the dashboard): formula not readable from the .mstr "
             "export; write its DAX manually." for m in derived if not m["formula"]]

    return {
        "source": {
            "platform": "MicroStrategy",
            "baseUrl": d.conn.base_url,
            "projectId": project_id,
            "dashboardId": did,
            "dashboardName": obj.get("name") or definition.get("name"),
            "path": "\\".join(a["name"] for a in sorted(obj.get("ancestors", []),
                                                         key=lambda a: -a["level"])),
            "owner": (obj.get("owner") or {}).get("name"),
            "dateModified": obj.get("dateModified"),
        },
        "prompts": r.get("dashboard_prompts") or [],
        "datasets": datasets,
        "derivedMetrics": derived,
        "chapters": chapters,
        "knownGaps": [
            "Database connection of Data Import cubes is embedded and not exposed by the REST API; "
            "confirm the source DB/warehouse manually.",
            "Metric formulas of Data Import (managed) metrics are not exposed; derive them from sourceSql "
            "(default aggregation is Sum). 'Row Count - <table>' metrics are auto-generated by Data Import.",
            "Visual formatting is not returned by the public REST API. Colour-by, element and metric colours, "
            "legend, data labels and free-form page positions are read from the .mstr export (undocumented "
            "format, best effort); fonts and axis title texts are not converted.",
        ] + gaps,
    }


def write_markdown(spec: dict, path: Path) -> None:
    s = spec["source"]
    out = [f"# {s['dashboardName']} - MicroStrategy dashboard spec", "",
           f"- Dashboard ID: `{s['dashboardId']}`",
           f"- Project ID: `{s['projectId']}`",
           f"- Path: {s['path']}",
           f"- Owner: {s['owner']} | Modified: {s['dateModified']}",
           f"- Prompts: {len(spec['prompts'])}", ""]

    out += ["## Datasets", ""]
    for ds in spec["datasets"]:
        out += [f"### {ds['name']} (`{ds['id']}`)", "",
                f"- Type: {ds['cubeType']} / {ds['sourceType']}",
                f"- Path: {ds['path']}", "", "**Attributes**", "",
                "| Attribute | Forms (category, data type) |", "|---|---|"]
        for a in ds["attributes"]:
            forms = "; ".join(f"{f['name']} ({f['category']}, {f['dataType']})" for f in a["forms"])
            out.append(f"| {a['name']} | {forms} |")
        out += ["", "**Metrics**", "", "| Metric | Data type |", "|---|---|"]
        out += [f"| {m['name']} | {m['dataType']} |" for m in ds["metrics"]]
        if ds["derivedElements"]:
            out += ["", f"**Derived elements:** `{json.dumps(ds['derivedElements'])}`"]
        out += ["", "**Source SQL**", "", "```sql", ds["sourceSql"] or "-- not available", "```", ""]
        if ds["analyticalEngineSteps"]:
            out += ["**Analytical engine steps**", "", "```", ds["analyticalEngineSteps"], "```", ""]

    if spec.get("derivedMetrics"):
        out += ["## Metrics created in the dashboard", ""]
        out += [f"- **{m['name']}** (`{m['id']}`): {m['formulaText'] or 'formula not found'}"
                for m in spec["derivedMetrics"]]
        out.append("")
    out += ["## Layout", ""]
    for ch in spec["chapters"]:
        out += [f"### Chapter: {ch['name']} (`{ch['key']}`)", ""]
        if ch["chapterFilters"]:
            out += [f"Chapter filters: `{json.dumps(ch['chapterFilters'])}`", ""]
        for pg in ch["pages"]:
            out += [f"#### Page: {pg['name']} (`{pg['key']}`)", ""]
            if pg.get("referencePdfPage"):
                out += [f"Rendering: `raw/dashboard.pdf` page {pg['referencePdfPage']}", ""]
            for v in pg["visualizations"]:
                fmt = lambda xs: ", ".join(x["name"] for x in xs) or "-"
                out += [f"- **{v['name']}** (`{v['key']}`) - type `{v['type']}`",
                        f"  - Rows: {fmt(v['rows'])}",
                        f"  - Columns: {fmt(v['columns'])}",
                        f"  - Metrics: {fmt(v['metrics'])}",
                        f"  - Datasets: {', '.join(v['datasets']) or '-'}"]
                for attr in v["rows"] + v["columns"]:
                    out.append(f"  - `{attr['name']}` shows forms: "
                               f"{', '.join(f['name'] for f in attr['formsShown'])}")
                if v["thresholds"]:
                    out.append(f"  - Thresholds: `{json.dumps(v['thresholds'])}`")
                if v.get("colorBy"):
                    cb = v["colorBy"]
                    out.append(f"  - Colour by `{cb['attribute']}` ({cb['source']}): "
                               + ", ".join(f"{c['value']} {c['color']}" for c in cb["colors"]))
                if v.get("metricColors"):
                    out.append("  - Metric colours (.mstr export): "
                               + ", ".join(f"{c['metric']} {c['color']}" for c in v["metricColors"]))
                if v.get("display"):
                    out.append(f"  - Display (.mstr export): `{json.dumps(v['display'])}`")
            for sel in pg["selectors"]:
                out += [f"- **Selector: {sel['name']}** - `{sel['selectorType']}` shown as "
                        f"`{sel['displayStyle']}`, source `{(sel['source'] or {}).get('name')}`, "
                        f"targets {sel['targets']}, multi-select={sel['multiSelect']}, "
                        f"all-option={sel['hasAllOption']}",
                        f"  - Current: {sel['summary']}"]
            out.append("")

    out += ["## Known gaps", ""] + [f"- {g}" for g in spec["knownGaps"]]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard_id")
    ap.add_argument("--project-id", default=None)
    args = ap.parse_args()
    did = args.dashboard_id

    out_dir = extract_dir(did)
    # Write to a local staging folder; the previous extract is only replaced when this run succeeds.
    staging = staging_dir("extract")
    conn = get_connection(project_id=args.project_id)
    d = Dumper(conn, staging)
    try:
        print(f"Project: {conn.project_id}\n\n== Dashboard {did} ==")
        d.call("dashboard_object", "GET", f"/api/objects/{did}", params={"type": 55})
        # Dashboards (dossiers) and Report Services documents share the object subtype; only a dossier has a
        # dossier definition.
        definition = d.call("dashboard_definition", "GET", f"/api/v2/dossiers/{did}/definition")
        is_document = definition is None
        if is_document:
            definition = d.call("dashboard_definition", "GET", f"/api/documents/{did}/definition")
        kind = "documents" if is_document else "dossiers"
        if definition is None:
            reason = d.errors.get("dashboard_definition", "unknown error")
            raise SystemExit(f"Dashboard definition could not be read from MicroStrategy: {reason} "
                             f"(previous extract kept; this run's responses are in {staging})")
        cubes_used = d.call("dashboard_cubes_used", "GET", f"/api/documents/{did}/cubes")
        d.call("dashboard_prompts", "GET", f"/api/documents/{did}/prompts")

        # --- Execute an instance to get per-visualization definitions ---------
        inst = d.call("dashboard_instance", "POST", f"/api/{kind}/{did}/instances", json={})
        mid = (inst or {}).get("mid")
        if mid:
            try:
                if is_document:  # layouts play the part of chapters, also in the visualization endpoint
                    structure = document_as_dossier(definition)
                else:
                    structure = d.call("dashboard_instance_definition", "GET",
                                       f"/api/v2/dossiers/{did}/instances/{mid}/definition") or definition
                for ch in structure.get("chapters", []):
                    for pg in ch.get("pages", []):
                        for viz in pg.get("visualizations", []):
                            d.call(f"viz_{ch['key']}_{viz['key']}", "GET",
                                   f"/api/v2/dossiers/{did}/instances/{mid}/chapters/{ch['key']}"
                                   f"/visualizations/{viz['key']}",
                                   params={"offset": 0, "limit": VIZ_SAMPLE_ROWS})
                # The dashboard file holds what the REST definition lacks (colours, legend, data labels).
                d.download("dashboard_mstr", "dashboard.mstr", "POST",
                           f"/api/documents/{did}/instances/{mid}/mstr",
                           headers={"Accept": "application/octet-stream"})
                d.export_pdf(did, mid)  # how MicroStrategy draws each page: the target for the Power BI look
            finally:
                d.call("dashboard_instance_delete", "DELETE", f"/api/documents/{did}/instances/{mid}")

        # --- Datasets / cubes ------------------------------------------------
        # A dataset is a cube, or a report that reads a cube (documents): fetch the cube behind each report.
        dataset_ids = set()
        for ds in (definition or {}).get("datasets", []):
            dsid = ds.get("id")
            o = d.call(f"dataset_{dsid}_object", "GET", f"/api/objects/{dsid}", params={"type": 3}) if dsid else None
            if o and o.get("subtype") not in CUBE_SUBTYPES:
                report = d.call(f"report_{dsid}", "GET", f"/api/v2/reports/{dsid}")
                dsid = ((report or {}).get("source") or {}).get("id") or dsid
            if dsid:
                dataset_ids.add(dsid)
        if isinstance(cubes_used, list):
            dataset_ids |= {c["id"] for c in cubes_used if isinstance(c, dict) and c.get("id")}
        table_ids = set()
        for cid in sorted(dataset_ids):
            print(f"\n== Dataset {cid} ==")
            d.call(f"cube_{cid}_object", "GET", f"/api/objects/{cid}", params={"type": 3})
            d.call(f"cube_{cid}_v2", "GET", f"/api/v2/cubes/{cid}")
            model = d.call(f"cube_{cid}_model", "GET", f"/api/model/cubes/{cid}",
                           params={"showExpressionAs": "tree", "showAdvancedProperties": "true"})
            d.call(f"cube_{cid}_sqlview", "GET", f"/api/v2/cubes/{cid}/sqlView")
            table_ids |= find_ids(model or {}, is_table)

        for tid in sorted(table_ids):
            print(f"\n== Table {tid} ==")
            d.call(f"table_{tid}_model", "GET", f"/api/model/tables/{tid}",
                   params={"showExpressionAs": "tree"})

        spec = build_spec(d, did, conn.project_id)
        (staging / "migration_spec.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        write_markdown(spec, staging / "migration_spec.md")
    finally:
        (staging / "_calls.json").write_text(json.dumps(d.calls, indent=2), encoding="utf-8")
        conn.close()

    leftovers = mirror(staging, out_dir)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"\nSaved {len(d.calls)} responses to {out_dir}")
    if leftovers:
        print(f"Note: could not remove old files (in use): {', '.join(leftovers)}")


if __name__ == "__main__":
    main()
