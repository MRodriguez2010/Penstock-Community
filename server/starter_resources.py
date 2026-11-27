"""Community project resource effects for the local starter runner."""
import json

import project_resources
import starter_recipes as recipes
import typed_tables


def intake_table(c, project):
    saved = c.execute("SELECT resource FROM starter_destinations WHERE project=? AND kind=?",
                      (project, recipes.INTAKE)).fetchone()
    if saved:
        return typed_tables.editable(c, project, saved["resource"])
    select_options = {
        "Priority": [priority.title() for priority in recipes.PRIORITIES],
        "Source": ["Sample", "User input"],
        "Decision": ["Approved"],
    }
    columns = [{"name": name, "type": kind, **({"options": select_options[name]} if kind == "select" else {})}
               for name, kind in recipes.INTAKE_COLUMNS]
    table = typed_tables.new_table(c, project, project, {"name": "Reviewed intake", "columns": columns})
    c.execute("INSERT INTO starter_destinations VALUES(?,?,?)", (project, recipes.INTAKE, table["id"]))
    return typed_tables.editable(c, project, table["id"])


def publish(c, project, kind, run):
    output = run["output"]
    if kind == recipes.BRIEF:
        file = project_resources.save_file(c, project, project, {
            "name": output["fileName"], "text": output["markdown"],
        })
        return {"type": "file", **file, "name": output["fileName"]}
    table = intake_table(c, project)
    columns = {column["name"]: column["id"] for column in table["schema"]}
    if not output["row"].keys() <= columns.keys():
        raise ValueError("The Reviewed intake table is missing a starter column. Restore the column before retrying.")
    values = {columns[name]: value for name, value in output["row"].items()}
    # A starter approval grants a local write. It does not authorize a cascade into event-triggered flows.
    c.create_function("run_lineage", 0, lambda: json.dumps(["starter-local"] * 8))
    record = typed_tables.append(c, project, project, {"id": table["id"], "values": values})
    return {"type": "table", "id": table["id"], "rowId": record["id"], "name": table["name"]}
