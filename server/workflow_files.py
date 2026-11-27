"""Workflow reads and integrity checks for the local project file store."""
import hmac
from pathlib import PurePosixPath
import re

import graph_values
import project_resources as resources
from store import row, rows


def identifier(value):
    try:
        return resources.ident(value)
    except (ValueError, TypeError):
        raise ValueError("Choose a saved project file ID.") from None


def file_name(value):
    name = resources.name(value, "file name")
    if PurePosixPath(name).name != name or "\\" in name or name.startswith("."):
        raise ValueError("Enter the saved file name without a folder or URL.")
    return name


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Enter {label} from {low:,} to {high:,}.")
    return value


def version_number(value):
    return integer(value, "a version number", 1, 1_000_000)


def search_text(value):
    return resources.bounded(value, "file name search", 180, empty=True)


def list_limit(value):
    return integer(value, "a result limit", 1, 100)


def latest_only(value):
    if type(value) is not bool:
        raise ValueError("Choose whether to list only the latest version of each file.")
    return value


def expected_hash(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-fA-F]{64}", value):
        raise ValueError("Enter the expected SHA-256 as 64 hexadecimal characters.")
    return value.lower()


def expected_size(value):
    return integer(value, "an expected byte count", 0, resources.MEDIA_LIMIT)


def check_source(value, validate):
    if isinstance(value, dict) and set(value) == {"ref"}:
        graph_values.check_reference(value["ref"])
    else:
        validate(value["literal"] if isinstance(value, dict) and set(value) == {"literal"} else value)


def check_lookup(cfg, *, exact):
    if "id" in cfg:
        if "version" in cfg or exact and "name" in cfg:
            raise ValueError("Choose a file ID or a file name and version.")
        check_source(cfg["id"], identifier)
        return
    if "name" not in cfg or exact and "version" not in cfg:
        raise ValueError("Choose a saved file ID or its exact name and version." if exact else
                         "Choose a saved file ID or name.")
    check_source(cfg["name"], file_name)
    if "version" in cfg:
        check_source(cfg["version"], version_number)


def validate_file(cfg):
    op = cfg["operation"]
    if op == "read":
        check_lookup(cfg, exact=False)
    elif op == "save":
        if "name" not in cfg:
            raise ValueError("Enter a name for the file to save.")
        check_source(cfg["name"], file_name)
    else:
        for key, validate in (("search", search_text), ("limit", list_limit), ("latestOnly", latest_only)):
            if key in cfg:
                check_source(cfg[key], validate)
    return {"success"}


def validate_verification(cfg):
    check_lookup(cfg, exact=True)
    check_source(cfg["expectedSha256"], expected_hash)
    check_source(cfg["expectedBytes"], expected_size)
    return {"success", "error"}


def choose_id(c, project, cfg):
    if "id" in cfg:
        return identifier(cfg["id"])
    name = file_name(cfg["name"])
    version = version_number(cfg["version"]) if "version" in cfg else None
    found = row(c, """SELECT id FROM project_files WHERE project=? AND name=?
                       AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1""",
                project, name, version, version)
    if found is None:
        raise resources.Missing("File or version unavailable in this project. Check the saved name and version.")
    return found["id"]


def file_metadata(record, size):
    return {**record, "bytes": size, "sha256": record["digest"]}


def list_files(c, project, cfg):
    query = search_text(cfg.get("search", "")).casefold()
    limit = list_limit(cfg.get("limit", 100))
    latest = latest_only(cfg.get("latestOnly", False))
    c.create_function("workflow_file_matches", 2, lambda name, substring: substring in name.casefold(),
                      deterministic=True)
    return rows(c, """SELECT id,name,version,digest,length(data) AS bytes FROM project_files f
                       WHERE f.project=? AND workflow_file_matches(name,?)
                       AND (NOT ? OR version=(SELECT max(g.version) FROM project_files g
                                              WHERE g.project=f.project AND g.name=f.name))
                       ORDER BY rowid DESC LIMIT ?""", project, query, latest, limit)


def execute_file(c, project, cfg, incoming):
    op = cfg["operation"]
    if op == "list":
        return list_files(c, project, cfg)
    if op == "read":
        record, raw = resources.file_data(c, project, choose_id(c, project, cfg))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Choose a saved UTF-8 text file for this read.") from None
        return {**record, "bytes": len(raw), "text": text}
    saved = resources.save_file(c, project, project, {"name": cfg["name"], "text": cfg.get("text", incoming)})
    record = row(c, """SELECT id,name,version,digest,length(data) AS bytes FROM project_files
                       WHERE project=? AND id=?""", project, saved["id"])
    return file_metadata(record, record["bytes"])


def verify(c, project, cfg):
    try:
        digest = expected_hash(cfg["expectedSha256"])
        size = expected_size(cfg["expectedBytes"])
        metadata, raw = resources.file_data(c, project, choose_id(c, project, cfg))
    except resources.Missing:
        return {"verified": False, "reason": "file-unavailable",
                "message": "Choose a saved file and exact version in this project."}
    except resources.Conflict:
        return {"verified": False, "reason": "stored-checksum",
                "message": "The stored file failed its checksum. Review or upload a new version."}
    except ValueError as error:
        return {"verified": False, "reason": "invalid-check", "message": str(error)}
    actual_digest = metadata["digest"]
    actual_size = len(raw)
    hash_matches = hmac.compare_digest(actual_digest, digest)
    size_matches = actual_size == size
    result = {"verified": hash_matches and size_matches, "id": metadata["id"], "name": metadata["name"],
              "version": metadata["version"], "sha256": actual_digest, "bytes": actual_size,
              "hashMatches": hash_matches, "sizeMatches": size_matches}
    if not result["verified"]:
        result.update(reason="mismatch", message="The saved file differs from the expected size or SHA-256. Check its version and expected values.")
    return result
