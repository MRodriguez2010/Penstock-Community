"""Write only a verified approved executive brief to the launcher's Downloads folder."""
import re

from project_resources import file_data, ident
from starter_api import dispatch


BRIEF_NAME = re.compile(r"executive-brief-[0-9]{4}-[0-9]{2}-[0-9]{2}-to-[0-9]{4}-[0-9]{2}-[0-9]{2}\.md")


def save_copy(workspace, project, run, folder):
    receipt = dispatch(workspace, "starter-read", {"project": project, "id": run})
    effect = receipt.get("effect") or {}
    name = effect.get("name", "")
    if (receipt["kind"] != "weekly-brief" or receipt["status"] != "approved" or effect.get("type") != "file"
            or not BRIEF_NAME.fullmatch(name) or receipt["output"].get("fileName") != name):
        raise ValueError("Approve a saved executive brief in this project first.")
    with workspace.data.db(project) as db:
        meta, raw = file_data(db, project, ident(effect["id"]))
    if (meta["name"], meta["version"]) != (name, effect["version"]):
        raise ValueError("The approved brief no longer matches the project file.")

    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for number in range(1000):
            filename = name if number == 0 else f"{name[:-3]} ({number}).md"
            target = folder / filename
            try:
                stream = target.open("xb")
            except FileExistsError:
                continue
            try:
                with stream:
                    stream.write(raw)
                    stream.flush()
            except OSError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return {"name": filename}
    except OSError:
        raise ValueError("Penstock could not save this copy to Downloads. Check that the folder is writable.") from None
    raise ValueError("Downloads already contains too many copies of this brief.")
