"""Version-one inputs and deterministic results for the offline starters."""
from datetime import date
import re

BRIEF = "weekly-brief"
INTAKE = "reviewed-intake"

PRIORITIES = ("normal", "high", "urgent")
STATUSES = {"complete": "Completed", "in-progress": "In progress", "blocked": "Blocked"}
SAMPLE_UPDATES = (
    ("2026-11-12", "Confirmed the reporting scope", "Operations", "complete", "",
     "Use regional totals in the brief", "Operations notes · Nov 12"),
    ("2026-11-16", "Reconciled the weekly intake totals", "Operations", "complete", "",
     "", "Intake reconciliation · Nov 16"),
    ("2026-11-17", "Delivered the revised rollout checklist", "Delivery", "complete", "",
     "Pilot the checklist with two regions", "Rollout review · Nov 17"),
    ("2026-11-19", "Preparing the next coverage review", "Support", "in-progress", "2026-11-24",
     "", "Support coverage notes · Nov 19"),
    ("2026-11-20", "Waiting for regional cost estimates", "Finance", "blocked", "2026-11-20",
     "", "Regional estimates tracker · Nov 20"),
    ("2026-11-23", "Closed the support coverage review", "Support", "complete", "2026-11-24",
     "Extend coverage to the Northeast", "Support coverage review · Nov 23"),
    ("2026-11-24", "Verifying the refreshed forecast", "Finance", "in-progress", "2026-11-25",
     "", "Forecast review · Nov 24"),
    ("2026-11-26", "Published the rollout checklist internally", "Delivery", "complete", "",
     "", "Delivery changelog · Nov 26"),
)
SAMPLE_INTAKE = {
    "title": "Refresh the regional coverage report",
    "requester": "Casey Nguyen",
    "priority": "high",
    "notes": "Add the Northeast region before the next operations review.",
}
INTAKE_COLUMNS = (
    ("Request", "text"), ("Requested by", "text"), ("Priority", "select"),
    ("Notes", "text"), ("Source", "select"), ("Decision", "select"),
)


def samples():
    return {
        BRIEF: {
            "from": "2026-11-16",
            "through": "2026-11-22",
            "records": [dict(zip(("date", "title", "owner", "status", "due", "decision", "source"), record))
                        for record in SAMPLE_UPDATES],
        },
        INTAKE: dict(SAMPLE_INTAKE),
    }


DEFAULTS = {
    BRIEF: {
        "title": "Weekly executive brief",
        "reviewQuestion": "Save this brief to project files?",
        "includeOwners": True,
    },
    INTAKE: {
        "title": "Reviewed intake",
        "reviewQuestion": "Add this request to the Reviewed intake table?",
        "defaultPriority": "normal",
    },
}


def string(value, label, limit, optional=False):
    if not isinstance(value, str) or "\x00" in value or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"{label} needs {'up to' if optional else '1 to'} {limit} characters.")
    return value.strip()


def settings_for(kind, supplied):
    if not isinstance(supplied, dict) or set(supplied) != set(DEFAULTS[kind]):
        raise ValueError("Fill in all workflow settings.")
    settings = {
        "title": string(supplied["title"], "Workflow name", 100),
        "reviewQuestion": string(supplied["reviewQuestion"], "Review question", 300),
    }
    if kind == BRIEF:
        if type(supplied["includeOwners"]) is not bool:
            raise ValueError("Choose whether to show owners in the brief.")
        settings["includeOwners"] = supplied["includeOwners"]
    else:
        if supplied["defaultPriority"] not in PRIORITIES:
            raise ValueError("Choose Normal, High, or Urgent priority.")
        settings["defaultPriority"] = supplied["defaultPriority"]
    return settings


def day(value, label):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"Choose a valid {label}.")


def date_window(supplied):
    first = day(supplied.get("from"), "period start date")
    last = day(supplied.get("through"), "period end date")
    if first > last:
        raise ValueError("The period start must be on or before its end.")
    if (last - first).days > 30:
        raise ValueError("Choose a period of 31 days or fewer.")
    return first, last


def update_record(raw):
    if not isinstance(raw, dict):
        raise ValueError("Each update needs a date, title, and status.")
    when = day(raw.get("date"), "update date").isoformat()
    title = string(raw.get("title"), "Update title", 240)
    owner = string(raw.get("owner", ""), "Owner", 100, optional=True)
    source = string(raw.get("source", ""), "Source", 180, optional=True) or "User-entered update"
    decision = string(raw.get("decision", ""), "Decision", 240, optional=True)
    due = day(raw["due"], "due date").isoformat() if raw.get("due") else ""
    status = raw.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("Choose a status for each update.")
    return {"date": when, "title": title, "owner": owner, "status": status,
            "source": source, "decision": decision, "due": due}


def markdown(value):
    return re.sub(r"([\\`*_[\]<>#|])", r"\\\1", " ".join(value.splitlines()))


def brief(supplied, source, settings):
    first, last = date_window(supplied)
    raw = samples()[BRIEF]["records"] if source == "sample" else supplied.get("records")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 40:
        raise ValueError("Add between 1 and 40 updates for your brief.")
    records = sorted((update_record(record) for record in raw), key=lambda record: record["date"])
    start, end = first.isoformat(), last.isoformat()
    included = [v for v in records if start <= v["date"] <= end]
    counts = {status: sum(record["status"] == status for record in included) for status in STATUSES}
    decisions = [{"date": item["date"], "title": item["decision"], "source": item["source"]}
                 for item in included if item["decision"]]
    overdue = [{"due": item["due"], "title": item["title"], "source": item["source"]}
               for item in included if item["status"] != "complete" and item["due"] and item["due"] < end]
    lines = [f"# {markdown(settings['title'])}", "", f"Period: {start} to {end}",
             f"Source: {'Sample updates' if source == 'sample' else 'Project updates supplied for this run'}", "",
             f"{len(included)} updates in this period.", "", "## Decisions", ""]
    lines.extend(f"- {item['date']} — {markdown(item['title'])} (Source: {markdown(item['source'])})"
                 for item in decisions)
    if not decisions:
        lines.append("- No decisions were recorded.")
    lines += ["", "## Overdue items", ""]
    lines.extend(f"- Due {item['due']} — {markdown(item['title'])} (Source: {markdown(item['source'])})"
                 for item in overdue)
    if not overdue:
        lines.append("- No open items were due before the period end.")
    lines.append("")
    for status, label in STATUSES.items():
        lines += [f"## {label} ({counts[status]})", ""]
        for record in included:
            if record["status"] == status:
                owner = f" ({markdown(record['owner'])})" if settings["includeOwners"] and record["owner"] else ""
                lines.append(f"- {record['date']} — {markdown(record['title'])}{owner} "
                             f"(Source: {markdown(record['source'])})")
        if counts[status] == 0:
            lines.append("- No updates.")
        lines.append("")
    snapshot = {"from": start, "through": end, "records": records}
    copy = "\n".join(lines)
    if len(copy.encode("utf-8")) > 65536:
        raise ValueError("The brief is too long to save. Use fewer or shorter updates.")
    output = {
        "period": {"from": start, "through": end}, "counts": counts, "updates": included,
        "total": len(included), "decisions": decisions, "overdue": overdue,
        "fileName": f"executive-brief-{start}-to-{end}.md",
        "markdown": copy,
    }
    return snapshot, output


def intake(supplied, source, settings):
    values = SAMPLE_INTAKE if source == "sample" else supplied
    title = string(values.get("title"), "Request title", 180)
    requester = string(values.get("requester"), "Requester", 120)
    priority = values.get("priority", settings["defaultPriority"])
    if priority not in PRIORITIES:
        raise ValueError("Choose Normal, High, or Urgent priority.")
    notes = string(values.get("notes", ""), "Notes", 2000, optional=True)
    snapshot = {"title": title, "requester": requester, "priority": priority, "notes": notes}
    return snapshot, {"row": {
        "Request": title, "Requested by": requester, "Priority": priority.title(),
        "Notes": notes, "Source": "Sample" if source == "sample" else "User input", "Decision": "Approved",
    }}


def prepare(kind, supplied, source, settings):
    if not isinstance(supplied, dict):
        raise ValueError("Fill in the workflow inputs.")
    if source not in ("sample", "custom"):
        raise ValueError("Choose sample data or enter your own.")
    recipe = brief if kind == BRIEF else intake
    return recipe(supplied, source, settings)
