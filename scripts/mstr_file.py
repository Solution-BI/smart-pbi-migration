"""Read chart settings the public REST API leaves out, from a dashboard's .mstr export.

The .mstr file (POST /api/documents/{id}/instances/{mid}/mstr) is a zip. Its <dashboard id>.delta member is
MicroStrategy's binary document definition: undocumented, so this is best effort. Two parts are readable:
  - chart properties as XML: <props><widgetProps><fmt> ... legend, data labels, subtype, nested labels
  - custom colours as JSON {"color": <Windows BGR int>}, each preceded by the attribute ID and the element
    key (integer keys only), or by the metric ID for metric colours
  - free-form page layouts as JSON {"floatChildren": ...}: position of every visual and selector in pixels
  - formulas of metrics created in the dashboard (derived metrics): the object IDs of their operands and
    functions, operands before their function (post-order), between the metric's name and 'TokenStream'
Callers must check element keys against the element lists returned by the REST API, and function IDs
against the server (GET /api/objects/{id}?type=11).
"""
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROPS = re.compile(rb"<props>.*?</props>", re.S)
COLOR = re.compile(rb'\{"color":(\d+)\}')
# Object ID: 09 07, then the 32-hex ID as DWORD, WORD, WORD (each little-endian, 05-prefixed words), 2 DWORDs
OBJECT_ID = re.compile(rb"\x09\x07(.{4})\x05(.{2})\x05(.{2})(.{4})(.{4})", re.S)
INT_ELEMENT_KEY = re.compile(rb"\x05\x14\x00\x08(.{8})", re.S)
NULL_ID = "0" * 32


def read_definition(mstr_file: Path) -> bytes:
    with zipfile.ZipFile(mstr_file) as z:
        name = next((n for n in z.namelist() if n.endswith(".delta")), None)
        return z.read(name) if name else b""


def _object_id(m: re.Match) -> str:
    d1, w_low, w_high, d3, d4 = (int.from_bytes(g, "little") for g in m.groups())
    return f"{d1:08X}{w_high:04X}{w_low:04X}{d3:08X}{d4:08X}"


def _encode_id(oid: str) -> bytes:
    d1, w_high, w_low, d3, d4 = (int(oid[a:b], 16) for a, b in ((0, 8), (8, 12), (12, 16), (16, 24), (24, 32)))
    return (b"\x09\x07" + d1.to_bytes(4, "little") + b"\x05" + w_low.to_bytes(2, "little")
            + b"\x05" + w_high.to_bytes(2, "little") + d3.to_bytes(4, "little") + d4.to_bytes(4, "little"))


def derived_metric_tokens(data: bytes, metric_id: str, name: str) -> list[str]:
    """Object IDs in a derived metric's formula, operands before their function; [] if not found."""
    label = name.encode("utf-8")
    for m in re.finditer(re.escape(_encode_id(metric_id)), data):
        head = data[m.end():m.end() + 400]  # the definition has the metric's name right after its ID
        if label not in head:
            continue
        start = m.end() + head.index(label) + len(label)
        end = data.find(b"TokenStream", start)
        if end > 0:
            return [i for i in (_object_id(x) for x in OBJECT_ID.finditer(data, start, end)) if i != NULL_ID]
    return []


def bgr_to_hex(value: int) -> str:
    return f"#{value & 0xFF:02X}{(value >> 8) & 0xFF:02X}{(value >> 16) & 0xFF:02X}"


def chart_properties(data: bytes) -> list[dict]:
    """Property sets of graph visualizations, in file order: {tag: value}."""
    charts = []
    for m in PROPS.finditer(data):
        try:
            fmt = ET.fromstring(m.group().decode("utf-8")).find("./widgetProps/fmt")
        except (ET.ParseError, UnicodeDecodeError):
            continue
        if fmt is not None and fmt.find("Shape") is not None:
            charts.append({el.tag: el.get("value") for el in fmt})
    return charts


def element_colors(data: bytes) -> list[dict]:
    """[{objectId, elementKey, color}] for custom colours in the dashboard: attribute elements (with a key) or
    metrics (elementKey None)."""
    found, start = [], 0
    for m in COLOR.finditer(data):
        block = data[start:m.start()]  # only bytes since the previous colour
        start = m.end()
        ids = [x for x in OBJECT_ID.finditer(block) if _object_id(x) != NULL_ID]
        if not ids:
            continue
        keys = INT_ELEMENT_KEY.findall(block, ids[-1].end())  # an element key follows its attribute's ID
        found.append({"objectId": _object_id(ids[-1]),
                      "elementKey": str(int.from_bytes(keys[-1], "little", signed=True)) if keys else None,
                      "color": bgr_to_hex(int(m.group(1)))})
    return found


def _px(value) -> float:
    """MicroStrategy layout size 'percent|pixels px' -> pixels."""
    return float(str(value).split("|")[-1].removesuffix("px"))


def page_layouts(data: bytes) -> dict[str, dict]:
    """Free-form page layouts: {page key: {width, height, items: {visual/selector key: {x, y, width, height}}}}."""
    pages = {}

    def walk(group: dict, dx: float, dy: float, size: tuple[float, float]) -> None:
        for item in group.get("children", []):
            x, y = dx + _px(item.get("left", 0)), dy + _px(item.get("top", 0))
            if item.get("t") == "g":
                walk(item, x, y, size)
            elif item.get("t") == "flc" and item.get("rk") and item.get("k"):
                page = pages.setdefault(item["rk"], {"width": size[0], "height": size[1], "items": {}})
                page["items"][item["k"]] = {"x": x, "y": y, "width": _px(item["width"]),
                                            "height": _px(item["height"])}

    for m in re.finditer(rb'\{"floatChildren"', data):
        end = data.find(b"\x00", m.start())
        try:
            layout = json.loads(data[m.start():end].decode("utf-8"))
        except ValueError:
            continue
        group = layout.get("floatChildren")
        if isinstance(group, dict) and group.get("width") and group.get("height"):
            walk(group, 0, 0, (_px(group["width"]), _px(group["height"])))
    return pages
