"""Back up a stopped Penstock workspace or restore it into a new directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from penstock import single_writer


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def backup(workspace, destination):
    workspace=workspace.resolve()
    if not (workspace/"owner.key").is_file() or destination.resolve().is_relative_to(workspace):
        raise ValueError("Select an initialized workspace and an archive outside it.")
    if destination.exists():
        raise ValueError("Choose a new backup filename.")
    manifest={}
    with single_writer(workspace), tempfile.TemporaryDirectory(dir=destination.parent) as stage:
        archive=Path(stage)/"backup.zip"
        with ZipFile(archive,"w",ZIP_DEFLATED) as out:
            for path in sorted(workspace.rglob("*")):
                name=path.relative_to(workspace).as_posix()
                if path.is_symlink():
                    raise ValueError("Move symlinked resources into the workspace before making a portable backup.")
                if not path.is_file() or name in (".server.lock",) or name.endswith("-shm"):
                    continue
                manifest[name]=digest(path)
                out.write(path,"workspace/"+name)
            out.writestr("manifest.json",json.dumps({"format":1,"sha256":manifest},sort_keys=True))
        os.chmod(archive,0o600)
        os.link(archive,destination)
    return len(manifest)


def restore(source, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore into a new directory.")
    with tempfile.TemporaryDirectory(dir=destination.parent) as stage, ZipFile(source) as archive:
        root=Path(stage)/"workspace"
        root.mkdir(mode=0o700)
        manifest=json.loads(archive.read("manifest.json"))
        names=archive.namelist()
        records=manifest["sha256"]
        if manifest.get("format")!=1 or "owner.key" not in records or len(names)!=len(set(names)) or set(names)!={"manifest.json",*("workspace/"+n for n in records)}:
            raise ValueError("Invalid or incomplete Penstock backup.")
        for name,checksum in records.items():
            path=PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or str(path)!=name or not path.parts or "\\" in name or ":" in name:
                raise ValueError("Invalid workspace path in backup.")
            final=root.joinpath(*path.parts)
            final.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with archive.open("workspace/"+name) as src, final.open("xb") as dst:
                shutil.copyfileobj(src,dst)
            final.chmod(0o600)
            if digest(final)!=checksum:
                raise ValueError("Backup checksum failed for "+name)
        if destination.exists():
            raise ValueError("The restore destination changed.")
        root.rename(destination)
    return len(records)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("backup","restore"))
    parser.add_argument("source",type=Path)
    parser.add_argument("destination",type=Path)
    args=parser.parse_args()
    try:
        print((backup if args.action=="backup" else restore)(args.source,args.destination),"files verified.")
    except Exception as exc:
        parser.exit(1,"Backup operation stopped: "+str(exc)+"\n")
