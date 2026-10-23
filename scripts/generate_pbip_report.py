"""Generate a Power BI project (PBIP) with the report pages, slicers and visuals of a migrated dashboard.

Reads 2_work/<ID>/2_transform/powerbi_build_spec.json and writes
3_powerbi/<first 8 chars of ID>/<Report name>.pbip plus its .Report and .SemanticModel folders.
Report settings, theme and file versions come from a template (scripts/templates/report, or a .pbix saved
by the local Power BI Desktop), so the files match that Desktop version. The semantic model 'definition'
folder (TMDL) is built from the same spec by build_model_tmdl.ps1.

Usage:
  .venv\\Scripts\\python.exe scripts\\generate_pbip_report.py <DASHBOARD_ID> [--template <folder|.pbix>] [--out <folder>]
"""
import argparse
import hashlib
import json
import re
import subprocess
import uuid
import zipfile
from pathlib import Path

from paths import TEMPLATES_DIR, powerbi_project_dir, transform_dir

HERE = Path(__file__).resolve().parent

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
# Power BI's default page width: its default text sizes then keep the proportions of a MicroStrategy page (about
# 1200 px wide). The page height follows each source page's proportions.
PAGE_W = 1280
SLICER_MODES = {"Dropdown": "Dropdown", "List": "Basic", "Tile": "Basic"}


def object_name(*parts: str) -> str:
    """Stable 20-character hex name (PBIR naming convention) so re-runs give the same files."""
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def text_lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def field_expr(ref: dict) -> dict:
    kind, prop = ("Measure", ref["measure"]) if "measure" in ref else ("Column", ref["column"])
    return {kind: {"Expression": {"SourceRef": {"Entity": ref["table"]}}, "Property": prop}}


def projection(ref: dict) -> dict:
    prop = ref.get("measure") or ref["column"]
    p = {"field": field_expr(ref), "queryRef": f"{ref['table']}.{prop}", "nativeQueryRef": prop}
    if ref.get("displayName"):
        p["displayName"] = ref["displayName"]
    return p


def filter_literal(value, dtype: str) -> str:
    if dtype == "dateTime":
        return f"datetime'{value}'"
    if dtype == "int64":
        return f"{int(value)}L"
    if dtype == "double":
        return f"{float(value)}D"
    if dtype == "boolean":
        return "true" if value else "false"
    return text_lit(str(value))


def slicer_filter(slicer: dict) -> dict:
    field, sel = slicer["field"], slicer["defaultSelection"]
    condition = {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": field["column"]}}],
        "Values": [[{"Literal": {"Value": filter_literal(v, slicer["dataType"])}}] for v in sel["values"]],
    }}
    if sel["mode"] == "exclude":
        condition = {"Not": {"Expression": condition}}
    return {"Version": 2, "From": [{"Name": "s", "Entity": field["table"], "Type": 0}],
            "Where": [{"Condition": condition}]}


def page_filter(f: dict, scope: str) -> dict:
    """Filter pane filter of a page (MicroStrategy chapter filter) or of the report (cube filter); scope names it."""
    out = {"name": object_name(scope, f["id"]), "field": field_expr(f["field"]),
           "type": "Categorical", "howCreated": "User"}
    if f["defaultSelection"]:
        out["filter"] = slicer_filter(f)
    general = {}
    if f["singleSelect"]:
        general["requireSingleSelect"] = lit("true")
    if f["defaultSelection"] and f["defaultSelection"]["mode"] == "exclude":
        general["isInvertedSelectionMode"] = lit("true")
    if general:
        out["objects"] = {"general": [{"properties": general}]}
    return out


def page_height(page: dict) -> int:
    """Power BI page height at PAGE_W wide, keeping the source page's proportions."""
    return round(page["height"] * PAGE_W / page["width"])


def scaled(pos: dict, page: dict) -> dict:
    s = PAGE_W / page["width"]  # same factor both ways, so shapes keep their proportions
    return {"x": round(pos["x"] * s), "y": round(pos["y"] * s),
            "width": round(pos["width"] * s), "height": round(pos["height"] * s)}


def title_objects(title: str) -> dict:
    return {"title": [{"properties": {"show": lit("true"), "text": lit(text_lit(title))}}]}


def visual_container(name: str, position: dict, z: int, visual: dict, annotations: dict) -> dict:
    return {
        "$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.13.0/schema.json",
        "name": name,
        "position": {**position, "z": z, "tabOrder": z},
        "visual": visual,
        "annotations": [{"name": k, "value": str(v)} for k, v in annotations.items()],
    }


def build_slicer(s: dict, page: dict, z: int) -> tuple[str, dict]:
    objects = {
        "data": [{"properties": {"mode": lit(text_lit(SLICER_MODES.get(s["style"], "Basic")))}}],
        "selection": [{"properties": {"singleSelect": lit("true" if s["singleSelect"] else "false"),
                                      "selectAllCheckboxEnabled": lit("true" if s["showSelectAll"] else "false")}}],
    }
    general = {}
    if s["style"] == "Tile":
        general["orientation"] = lit("1D")  # horizontal: a row of tiles, like MicroStrategy link/button bars
    if s.get("defaultSelection"):
        general["filter"] = {"filter": slicer_filter(s)}
    if general:
        objects["general"] = [{"properties": general}]
    visual = {"visualType": "slicer",
              "query": {"queryState": {"Values": {"projections": [projection(s["field"])]}}},
              "objects": objects,
              "visualContainerObjects": title_objects(s["title"])}
    name = object_name(page["mstrPageKey"], s["id"])
    return name, visual_container(name, scaled(s["position"], page), z, visual,
                                  {"mstrKey": s["id"], "mstrType": f"selector/{s['mstrDisplayStyle']}"})


def series_color(series: dict, value, color: str, dtype: str) -> dict:
    """dataPoint entry giving one legend value its colour."""
    return {"properties": {"fill": {"solid": {"color": lit(text_lit(color))}}},
            "selector": {"data": [{"scopeId": {"Comparison": {
                "ComparisonKind": 0, "Left": field_expr(series),
                "Right": {"Literal": {"Value": filter_literal(value, dtype)}}}}}]}}


def axis_title(text: str | None) -> dict:
    return {"showAxisTitle": lit("true"), **({"titleText": lit(text_lit(text))} if text else {})}


def card_value_size(position: dict) -> int:
    """Callout font size (pt) that fills a card the way a MicroStrategy KPI value does."""
    return max(14, round(min(position["height"] * 0.28, position["width"] * 0.9 / 6.4)))


def chart_objects(v: dict, position: dict) -> dict:
    objects = {}
    if v["visualType"] in ("card", "multiRowCard"):
        value = {"fontSize": lit(f"{card_value_size(position)}D")}
        if v.get("displayUnitsNone"):
            value["labelDisplayUnits"] = lit("1D")
        objects["labels"] = [{"properties": value}]
        if v.get("categoryLabel") is False:
            objects["categoryLabels"] = [{"properties": {"show": lit("false")}}]
        return objects
    if v.get("legend") is not None:
        props = {"show": lit("true" if v["legend"] else "false")}
        if v.get("legendPosition"):
            props["position"] = lit(text_lit(v["legendPosition"]))
        if v.get("legendTitle"):
            props.update(showTitle=lit("true"), titleText=lit(text_lit(v["legendTitle"])))
        objects["legend"] = [{"properties": props}]
    no_units = {"labelDisplayUnits": lit("1D")} if v.get("displayUnitsNone") else {}
    titles = v.get("axisTitles")
    if v.get("dataLabels"):
        vertical = {"labelOrientation": lit("1D")} if v.get("labelsVertical") else {}
        objects["labels"] = [{"properties": {"show": lit("true"), **no_units, **vertical}}]
    if no_units or titles:
        objects["valueAxis"] = [{"properties": {**no_units, **(axis_title(titles["value"]) if titles else {})}}]
    if titles:
        objects["categoryAxis"] = [{"properties": axis_title(titles["category"])}]
    if v.get("seriesColors"):
        series = v["fields"]["Series"][0]
        objects["dataPoint"] = [series_color(series, c["value"], c["color"], v["seriesDataType"])
                                for c in v["seriesColors"]]
    elif v.get("measureColors"):
        objects["dataPoint"] = [{"properties": {"fill": {"solid": {"color": lit(text_lit(c["color"]))}}},
                                 "selector": {"metadata": projection(c)["queryRef"]}} for c in v["measureColors"]]
    return objects


def build_visual(v: dict, page: dict, z: int) -> tuple[str, dict]:
    name = object_name(page["mstrPageKey"], v["id"])
    query_state = {}
    for role, refs in v["fields"].items():
        projections = [projection(r) for r in refs]
        if role == "Category" and len(projections) > 1:
            for p in projections:
                p["active"] = True  # every level shown (expanded, nested labels), not just the top one
        query_state[role] = {"projections": projections}
    query = {"queryState": query_state}
    if v.get("sort"):
        query["sortDefinition"] = {"sort": [{"field": field_expr(r), "direction": "Ascending"} for r in v["sort"]],
                                   "isDefaultSort": True}
    visual = {"visualType": v["visualType"] or "tableEx", "query": query}
    position = scaled(v["position"], page)
    if (objects := chart_objects(v, position)):
        visual["objects"] = objects
    visual["visualContainerObjects"] = title_objects(v["title"])
    return name, visual_container(name, position, z, visual, {"mstrKey": v["id"], "mstrType": v["mstrType"]})


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_template(source: Path) -> dict[str, bytes]:
    """Report settings + theme (paths relative to the .Report folder), from a template folder or a .pbix."""
    if source.is_dir():
        return {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    with open(source, "rb") as fh, zipfile.ZipFile(fh) as z:
        return {n.removeprefix("Report/"): z.read(n) for n in z.namelist() if n.startswith("Report/")}


def safe_name(name: str) -> str:
    """Report name usable as a Windows folder name."""
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") or "Report"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard_id")
    ap.add_argument("--template", type=Path, default=TEMPLATES_DIR / "report",
                    help="report template: a .Report-style folder or a .pbix saved by Power BI Desktop")
    ap.add_argument("--out", type=Path, help="project folder (default 3_powerbi/<first 8 chars of ID>)")
    ap.add_argument("--no-model", action="store_true", help="skip building the semantic model (TMDL)")
    args = ap.parse_args()

    spec_path = transform_dir(args.dashboard_id) / "powerbi_build_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    report_name = safe_name(spec["report"]["name"])
    root = args.out or powerbi_project_dir(args.dashboard_id)
    report_dir = root / f"{report_name}.Report"
    model_dir = root / f"{report_name}.SemanticModel"
    if report_dir.exists() or model_dir.exists():
        raise SystemExit(f"{root} already has this project; remove it first.")

    template = read_template(args.template)
    for rel, data in template.items():
        if rel.startswith("definition/pages/") or rel in (".platform", "definition.pbir"):
            continue  # generated below
        target = report_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if spec["report"].get("filters"):  # MicroStrategy cube filters: report level ('Filters on all pages')
        report_json = report_dir / "definition" / "report.json"
        settings = json.loads(report_json.read_text(encoding="utf-8"))
        settings["filterConfig"] = {"filters": [page_filter(f, "report") for f in spec["report"]["filters"]]}
        write_json(report_json, settings)

    # --- Pages and visuals ---------------------------------------------------------------
    page_names = []
    for page in spec["report"]["pages"]:
        pname = object_name(page["mstrChapterKey"], page["mstrPageKey"])
        page_names.append(pname)
        page_dir = report_dir / "definition" / "pages" / pname
        page_json = {"$schema": f"{SCHEMA}/item/report/definition/page/2.1.0/schema.json",
                     "name": pname, "displayName": page["name"], "displayOption": "FitToPage",
                     "height": page_height(page), "width": PAGE_W}
        if page.get("filters"):
            page_json["filterConfig"] = {"filters": [page_filter(f, page["mstrPageKey"]) for f in page["filters"]]}
        names, z = {}, 0
        for s in page["slicers"]:
            vname, container = build_slicer(s, page, z)
            names[s["id"]], z = vname, z + 1000
            write_json(page_dir / "visuals" / vname / "visual.json", container)
        for v in page["visuals"]:
            vname, container = build_visual(v, page, z)
            names[v["id"]], z = vname, z + 1000
            write_json(page_dir / "visuals" / vname / "visual.json", container)
        interactions = [{"source": names[s["id"]], "target": names[t], "type": "NoFilter"}
                        for s in page["slicers"] for t in s["doesNotApplyTo"] if t in names]
        if interactions:
            page_json["visualInteractions"] = interactions
        write_json(page_dir / "page.json", page_json)
        print(f"  page '{page['name']}': {len(page['slicers'])} slicer(s), {len(page['visuals'])} visual(s)")

    write_json(report_dir / "definition" / "pages" / "pages.json",
               {"$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.1.0/schema.json",
                "pageOrder": page_names, "activePageName": page_names[0]})

    # --- Item files ----------------------------------------------------------------------
    write_json(report_dir / "definition.pbir",
               {"$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json", "version": "4.0",
                "datasetReference": {"byPath": {"path": f"../{report_name}.SemanticModel"}}})
    write_json(model_dir / "definition.pbism",
               {"$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
                "version": "4.0", "settings": {}})
    for folder, kind in ((report_dir, "Report"), (model_dir, "SemanticModel")):
        write_json(folder / ".platform",
                   {"$schema": f"{SCHEMA}/gitIntegration/platformProperties/2.0.0/schema.json",
                    "metadata": {"type": kind, "displayName": report_name},
                    "config": {"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{folder.name}"))}})
    write_json(root / f"{report_name}.pbip",
               {"$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
                "artifacts": [{"report": {"path": f"{report_name}.Report"}}],
                "settings": {"enableAutoRecovery": True}})
    print(f"Report written to {report_dir}")

    if not args.no_model:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(HERE / "build_model_tmdl.ps1"),
                        "-SpecPath", str(spec_path), "-OutFolder", str(model_dir / "definition")], check=True)
    print(f"Project ready: {root / (report_name + '.pbip')}")


if __name__ == "__main__":
    main()
