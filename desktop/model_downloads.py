"""Durable, validator-bound partial transfers of explicitly requested models."""
from hashlib import sha256
import hmac
from http.client import HTTPException
import json
import os
import re
import shutil
import time
from urllib import error, request

from models import CHUNK, MIB, ModelRedirect, direct_client, regular_file, trusted_download_url


CHECKPOINT_INTERVAL = 16 * MIB
MARGIN = 512 * MIB
META_LIMIT = 4096
ETAG = re.compile(r'"[!#-~]{0,256}"')
CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})")


class DownloadProtocolError(ValueError):
    pass


class DownloadInterrupted(ValueError):
    pass


def model_identity(pack):
    return {"id": pack.id, "revision": pack.revision, "sha256": pack.sha256,
            "bytes": pack.size, "repository": pack.repo, "filename": pack.filename}


def staged_paths(destination):
    return (destination.with_name(destination.name + ".download.partial"),
            destination.with_name(destination.name + ".download.json"))


def pending_metadata(sidecar):
    return sidecar.with_name(sidecar.name + ".tmp")


def discard(destination):
    partial, sidecar = staged_paths(destination)
    for path in (partial, sidecar, pending_metadata(sidecar)):
        if regular_file(path):
            path.unlink(missing_ok=True)


def load_checkpoint(destination, pack):
    partial, sidecar = staged_paths(destination)
    for path in (partial, sidecar):
        if os.path.lexists(path) and not regular_file(path):
            raise DownloadProtocolError("Download staging requires regular files in the model cache.")
    if not partial.exists() or not sidecar.exists():
        return None
    try:
        with sidecar.open("rb") as record:
            encoded = record.read(META_LIMIT + 1)
        if len(encoded) > META_LIMIT:
            raise ValueError()
        checkpoint = json.loads(encoded)
        size, etag, digest = checkpoint["offset"], checkpoint["etag"], checkpoint["prefixSha256"]
        if (set(checkpoint) != {"schema", "model", "offset", "etag", "prefixSha256"}
                or checkpoint["schema"] != 1 or checkpoint["model"] != model_identity(pack)
                or type(size) is not int or not 4 <= size <= pack.size
                or not isinstance(etag, str) or not ETAG.fullmatch(etag)
                or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
                or not size <= partial.stat().st_size <= pack.size):
            raise ValueError()
    except (TypeError, ValueError, KeyError, RecursionError):
        raise DownloadProtocolError("The saved download checkpoint is inconsistent. Retry to start a fresh download.") from None
    return checkpoint


def resume_bytes(destination, pack):
    try:
        checkpoint = load_checkpoint(destination, pack)
    except (OSError, DownloadProtocolError):
        return 0
    return checkpoint["offset"] if checkpoint else 0


def resume_digest(target, checkpoint, stop):
    digest = sha256()
    left = checkpoint["offset"]
    first = True
    while left:
        if stop.is_set():
            raise DownloadInterrupted("Download cancelled. Retry to continue the saved transfer.")
        chunk = target.read(min(CHUNK, left))
        if not chunk or first and not chunk.startswith(b"GGUF"):
            raise DownloadProtocolError("The saved model prefix is invalid. Retry to start a fresh download.")
        first = False
        digest.update(chunk)
        left -= len(chunk)
    if not hmac.compare_digest(digest.hexdigest(), checkpoint["prefixSha256"]):
        raise DownloadProtocolError("The saved model prefix checksum differs. Retry to start a fresh download.")
    # Bytes written after the last durable checkpoint are never used on retry.
    target.truncate(checkpoint["offset"])
    return digest


def save_checkpoint(target, sidecar, pack, offset, etag, digest):
    target.flush()
    os.fsync(target.fileno())
    record = {"schema": 1, "model": model_identity(pack), "offset": offset,
              "etag": etag, "prefixSha256": digest.hexdigest()}
    staged = pending_metadata(sidecar)
    try:
        with staged.open("xb") as metadata:
            metadata.write(json.dumps(record, separators=(",", ":")).encode("ascii"))
            metadata.flush()
            os.fsync(metadata.fileno())
        os.replace(staged, sidecar)
    finally:
        if regular_file(staged):
            staged.unlink(missing_ok=True)


def header(response, name):
    values = response.headers.get_all(name, [])
    if len(values) > 1:
        raise DownloadProtocolError(f"The model host sent conflicting {name} headers.")
    return values[0].strip() if values else None


def response_offset(response, pack, checkpoint):
    if not trusted_download_url(response.geturl()):
        raise DownloadProtocolError("The model download left the trusted upstream hosts.")
    if (header(response, "Content-Encoding") or "identity").lower() != "identity":
        raise DownloadProtocolError("The model host returned an unexpected content encoding.")
    etag = header(response, "ETag")
    etag = etag if etag and ETAG.fullmatch(etag) else None
    coverage = header(response, "Content-Range")
    if response.status == 206:
        if checkpoint is None or etag != checkpoint["etag"]:
            raise DownloadProtocolError("The model host changed the resume validator. Retry to start a fresh download.")
        offset = checkpoint["offset"]
        span = CONTENT_RANGE.fullmatch(coverage or "")
        if not span or tuple(map(int, span.groups())) != (offset, pack.size - 1, pack.size):
            raise DownloadProtocolError("The model host sent an inconsistent Content-Range. Retry to start fresh.")
    elif response.status == 200 and coverage is None:
        offset = 0
    else:
        raise DownloadProtocolError("The model host did not return the requested file representation.")
    count = header(response, "Content-Length")
    if count is not None and count != str(pack.size - offset):
        raise DownloadProtocolError("The model host returned an unexpected byte count.")
    return offset, etag


def body(target, response, pack, sidecar, offset, etag, digest, stop, progress):
    saved = offset
    last_report = 0.0
    try:
        while offset < pack.size:
            if stop.is_set():
                raise DownloadInterrupted("Download cancelled.")
            try:
                chunk = response.read1(min(CHUNK, pack.size - offset))
            except (OSError, HTTPException) as exc:
                raise DownloadInterrupted("The model download was interrupted.") from exc
            if not chunk:
                raise DownloadInterrupted("The model host ended the transfer early.")
            target.write(chunk)
            digest.update(chunk)
            offset += len(chunk)
            if etag and offset - saved >= CHECKPOINT_INTERVAL:
                save_checkpoint(target, sidecar, pack, offset, etag, digest)
                saved = offset
            if time.monotonic() - last_report > 0.25:
                progress(offset)
                last_report = time.monotonic()
        if response.read(1):
            raise DownloadProtocolError("The model host returned extra bytes.")
    except DownloadInterrupted as exc:
        if etag and offset >= 4:
            save_checkpoint(target, sidecar, pack, offset, etag, digest)
            raise DownloadInterrupted(f"{exc} Retry to continue the saved transfer.") from None
        raise DownloadInterrupted(f"{exc} Retry to start a fresh transfer.") from None
    finally:
        # Non-resumable bodies are temporary even if the program was cancelled.
        if etag and offset == pack.size:
            save_checkpoint(target, sidecar, pack, offset, etag, digest)
    return digest


def download(destination, pack, stop, progress):
    partial, sidecar = staged_paths(destination)
    try:
        staged = pending_metadata(sidecar)
        if os.path.lexists(staged) and not regular_file(staged):
            raise DownloadProtocolError("Download staging requires regular files in the model cache.")
        staged.unlink(missing_ok=True)
        checkpoint = load_checkpoint(destination, pack)
        if checkpoint is None:
            discard(destination)
        offset = checkpoint["offset"] if checkpoint else 0
        resumed = offset
        digest = sha256()
        if stop.is_set():
            raise DownloadInterrupted("Download cancelled.")
        if offset == pack.size:
            with partial.open("r+b") as target:
                digest = resume_digest(target, checkpoint, stop)
        else:
            if not trusted_download_url(pack.url):
                raise DownloadProtocolError("The model download URL is outside the trusted upstream hosts.")
            headers = {"Accept-Encoding": "identity"}
            if offset:
                with partial.open("r+b") as target:
                    digest = resume_digest(target, checkpoint, stop)
                headers.update(Range=f"bytes={offset}-", **{"If-Range": checkpoint["etag"]})
            if shutil.disk_usage(destination.parent).free < pack.size - offset + MARGIN:
                raise ValueError("Free space for the remaining model bytes plus 512 MiB in the desktop data location.")
            opener = direct_client(ModelRedirect())
            try:
                source = opener.open(request.Request(pack.url, headers=headers), timeout=25)
            except error.HTTPError as exc:
                if offset and exc.code == 416:
                    exc.close()
                    raise DownloadProtocolError("The model host rejected the saved offset. Retry to start fresh.") from None
                raise
            with source:
                resumed, etag = response_offset(source, pack, checkpoint)
                if resumed == 0:
                    discard(destination)
                    digest = sha256()
                    if shutil.disk_usage(destination.parent).free < pack.size + MARGIN:
                        raise ValueError("Free the model size plus 512 MiB before restarting the full download.")
                with partial.open("ab" if resumed else "xb") as target:
                    digest = body(target, source, pack, sidecar, resumed, etag, digest, stop, progress)
                    target.flush()
                    os.fsync(target.fileno())
        if not hmac.compare_digest(digest.hexdigest(), pack.sha256):
            raise DownloadProtocolError("The complete model failed its pinned SHA-256 verification.")
        with partial.open("rb") as check:
            if check.read(4) != b"GGUF":
                raise DownloadProtocolError("Expected a GGUF model file.")
        if stop.is_set():
            raise DownloadInterrupted("Download cancelled. Retry to finish the verified transfer.")
        os.replace(partial, destination)
        discard(destination)
        progress(pack.size)
        return {"mode": "pinned-download", "resumedBytes": resumed}
    except DownloadProtocolError:
        discard(destination)
        raise
    finally:
        if not sidecar.exists():
            discard(destination)
