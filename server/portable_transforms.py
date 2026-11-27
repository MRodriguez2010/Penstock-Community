"""Bounded in-process data transformations and deterministic branching."""
import json
import math
import operator

import graph_values
from graph_spec import graph_id

NUMBER_OPS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
TEXT_OPS = {"contains": lambda left, right: right in left, "starts-with": str.startswith}
EQUALITY_OPS = {"equals": operator.eq, "differs": operator.ne}
COMPARISONS = NUMBER_OPS | TEXT_OPS | EQUALITY_OPS
BRANCH_PORTS = {"default", "error"}


def text_units(value):
    return len(value.encode("utf-16-le")) // 2


def counts(field, records):
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("Supply an array of up to 100 records.")
    size = len(json.dumps(records, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    if size > 15000:
        raise ValueError("Supply at most 15,000 UTF-8 bytes of records.")
    groups = {}
    for index, record in enumerate(records, 1):
        value = record.get(field) if isinstance(record, dict) else None
        if not isinstance(value, str) or text_units(value) > 160:
            raise ValueError(f"Record {index} needs a text value of up to 160 UTF-16 units in the selected field.")
        groups[value] = groups.get(value, 0) + 1
    return {
        "rowCount": len(records),
        "distinctCount": len(groups),
        "groups": [{"value": value, "count": count} for value, count in groups.items()],
    }


def validate_group(cfg):
    field = cfg["field"]
    if not isinstance(field, str) or not field.strip() or text_units(field) > 80:
        raise ValueError("Choose a grouping field of up to 80 UTF-16 units.")
    if "rows" not in cfg:
        return {"success", "error"}
    rows = cfg["rows"]
    if isinstance(rows, dict) and set(rows) == {"ref"}:
        graph_values.check_reference(rows["ref"])
    else:
        counts(field, rows["literal"] if isinstance(rows, dict) and set(rows) == {"literal"} else rows)
    return {"success", "error"}


def group(cfg, incoming):
    try:
        return "success", counts(cfg["field"], cfg.get("rows", incoming))
    except (ValueError, TypeError, RecursionError) as exc:
        return "error", {"message": str(exc)}


def scalar_type(value):
    kind = type(value)
    if kind is float and not math.isfinite(value):
        return None
    if kind in (int, float):
        return "number"
    if kind is str:
        return "text"
    if kind is bool:
        return "boolean"
    return None


def validate_ordered(cfg):
    branches = cfg["branches"]
    if not isinstance(branches, list) or not 1 <= len(branches) <= 8:
        raise ValueError("Supply one through eight ordered branches.")
    ports = set(BRANCH_PORTS)
    for index, branch in enumerate(branches, 1):
        required = {"id", "operator", "right"}
        if not isinstance(branch, dict) or not required <= set(branch) or set(branch) - required - {"label", "path"}:
            raise ValueError(f"Branch {index} needs an ID, comparison operator and right value.")
        identifier = graph_id(branch["id"])
        if identifier in ports:
            raise ValueError("Choose distinct branch IDs. Default and error are reserved.")
        ports.add(identifier)
        label = branch.get("label", identifier)
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("Supply a branch label of up to 80 characters.")
        graph_values.check_reference({"node": "$input", "path": branch.get("path", [])})
        op = branch["operator"]
        kind = scalar_type(branch["right"])
        if not isinstance(op, str) or op not in COMPARISONS or kind is None:
            raise ValueError(f"Choose a supported scalar comparison on branch {index}.")
        if op in NUMBER_OPS and kind != "number":
            raise ValueError("Numeric comparisons require a number.")
        if op in TEXT_OPS and kind != "text":
            raise ValueError("Text comparisons require text.")
    source = cfg.get("value")
    if isinstance(source, dict) and set(source) == {"ref"}:
        graph_values.check_reference(source["ref"])
    graph_values.bound(cfg)
    return ports


def ordered(cfg, incoming):
    source = cfg.get("value", incoming)
    for branch in cfg["branches"]:
        identifier = branch["id"]
        try:
            left = graph_values.resolve(
                {"node": "$input", "path": branch.get("path", [])}, {"input": source}
            )
            right = branch["right"]
            if scalar_type(left) != scalar_type(right):
                raise ValueError("The selected value has a different type.")
            op = branch["operator"]
            if COMPARISONS[op](left, right):
                return identifier, incoming
        except ValueError as exc:
            return "error", {"branch": identifier, "message": str(exc)}
    return "default", incoming
