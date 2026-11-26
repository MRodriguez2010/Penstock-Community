"""Bound and resolve JSON values supplied to portable workflow blocks."""
import json

MAX_BYTES = 32768


def bound(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Workflow value exceeds 32 KiB.")


def check_reference(ref):
    if not isinstance(ref, dict) or set(ref) - {"node","output","path"} or not isinstance(ref.get("node"),str):
        raise ValueError("Choose a node output.")
    if ref.get("output","values") not in ("values","input","content"):
        raise ValueError("Unknown output.")
    path = ref.get("path",[])
    if not isinstance(path,list) or len(path)>16 or any(type(k) not in (str,int) or type(k) is int and k<0 for k in path):
        raise ValueError("Use a path of object keys and nonnegative array indexes.")


def resolve(ref, context):
    check_reference(ref)
    key = ref["node"]
    if key == "$input":
        value = context["input"]
    elif key == "$previous":
        value = context["previous"]
    elif key == "$variables":
        value = context.get("variables",{})
    elif key in context["outputs"]:
        value = context["outputs"][key]
    else:
        raise ValueError(f"Step {key} has no output on this run path.")
    for index in ref.get("path", []):
        if isinstance(value,dict) and isinstance(index,str) and index in value:
            value = value[index]
        elif isinstance(value,list) and type(index) is int and index<len(value):
            value = value[index]
        else:
            raise ValueError(f"Missing value at {index!r}.")
    return value


def pin(value, context):
    if isinstance(value,dict) and set(value)=={"ref"}:
        return resolve(value["ref"],context)
    if isinstance(value,dict) and set(value)=={"literal"}:
        return value["literal"]
    return value
