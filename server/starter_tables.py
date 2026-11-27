"""Read a bounded, project-scoped table snapshot for the executive brief."""
from datetime import date
import json

import starter_recipes as recipes
from typed_tables import table_exists


MAX_TABLES = 250
MAX_UPDATES = 40
HEADERS = {"date": "date", "update date": "date", "title": "title", "update": "title",
           "status": "status", "owner": "owner", "due": "due", "due date": "due",
           "decision": "decision", "source": "source"}
REQUIRED = {"date", "title", "status"}
STATUS_VALUES = {"complete": "complete", "completed": "complete", "in progress": "in-progress",
                 "in-progress": "in-progress", "blocked": "blocked"}


def normalized(value):
    return " ".join(value.split()).casefold()


def list_tables(c, project):
    tables = c.execute("""SELECT t.id,t.name FROM project_tables t JOIN typed_schemas s ON s.table_id=t.id
                          WHERE t.project=? ORDER BY t.name COLLATE NOCASE,t.id LIMIT ?""", (project,MAX_TABLES))
    return {"tables": [dict(table) for table in tables]}


def snapshot(c, project, table):
    if not table_exists(c, project, table):
        raise ValueError("Choose a saved table from this project.")
    selected = c.execute("""SELECT t.name,s.schema_json FROM project_tables t JOIN typed_schemas s ON s.table_id=t.id
                            WHERE t.project=? AND t.id=?""", (project,table)).fetchone()
    if selected is None:
        raise ValueError("Choose a saved table with a current project schema.")
    schema = json.loads(selected["schema_json"])
    positions = {}
    for index, column in enumerate(schema):
        field = HEADERS.get(normalized(column["name"]))
        if field is None:
            continue
        if field in positions:
            raise ValueError(f"Keep only one column for {field} in this brief source.")
        positions[field] = index
    if not REQUIRED <= positions.keys():
        raise ValueError("The table needs Date, Title or Update, and Status columns.")

    count = c.execute("""SELECT count(*) FROM (
        SELECT 1 FROM project_records r JOIN project_tables t ON t.id=r.table_id
        WHERE t.project=? AND r.table_id=? LIMIT ?)
        """, (project,table,MAX_UPDATES + 1)).fetchone()[0]
    if not 1 <= count <= MAX_UPDATES:
        raise ValueError("Choose a table with between 1 and 40 updates.")

    # Only recognized fields leave SQLite. Field aliases are drawn from HEADERS.
    columns = ",".join(f"json_extract(r.values_json, ?) AS {field}" for field in positions)
    paths = [f"$[{position}]" for position in positions.values()]
    cells = c.execute(f"""SELECT {columns} FROM project_records r JOIN project_tables t ON t.id=r.table_id
         JOIN record_versions v ON v.record=r.id WHERE t.project=? AND r.table_id=?
         AND CASE WHEN json_valid(r.values_json) THEN json_array_length(r.values_json) END=? ORDER BY r.rowid LIMIT ?""",
                      (*paths,project,table,len(schema),MAX_UPDATES)).fetchall()
    if len(cells) != count:
        raise ValueError("Some table rows no longer match this schema. Refresh the table before importing.")

    records = []
    for index, entry in enumerate(cells, start=1):
        record = dict(entry)
        if not all(isinstance(value, str) for value in record.values()):
            raise ValueError(f"Row {index} has a value that no longer matches the saved table schema.")
        record = {field: value.strip() for field, value in record.items()}
        record["status"] = STATUS_VALUES.get(normalized(record["status"]), "")
        record["source"] = record.get("source") or selected["name"]
        try:
            records.append(recipes.update_record(record))
        except ValueError as exc:
            raise ValueError(f"Row {index}: {exc}") from None
    records.sort(key=lambda item: item["date"])
    first = date.fromisoformat(records[0]["date"])
    last = date.fromisoformat(records[-1]["date"])
    start = date.fromordinal(max(first.toordinal(), last.toordinal() - 30))
    prepared = {"from": start.isoformat(), "through": last.isoformat(), "records": records}
    recipes.date_window(prepared)
    return {"name": selected["name"], "input": prepared}
