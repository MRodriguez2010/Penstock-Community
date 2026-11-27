"""Sign, provision and verify offline packs for the pinned desktop GGUF models."""
import argparse
import getpass
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
from tempfile import NamedTemporaryFile
import threading

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


MAGIC = b"PENSTOCK-MODEL-PACK\0\x01"
LENGTH = struct.Struct(">I")
MAX_MANIFEST = 4096
SIGNATURE_BYTES = 64
MAX_KEY = 4096
EXTENSION = ".penstock-model"
TRUST_FOLDER = "trusted-model-publishers"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def key_id(value):
    if not isinstance(value, str) or re.fullmatch("[0-9a-fA-F]{64}", value) is None:
        raise ValueError("A publisher fingerprint must contain 64 hexadecimal characters.")
    return value.lower()


def fingerprint(key):
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sha256(raw).hexdigest()


def read_pem(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Choose a regular PEM key file.")
    with path.open("rb") as source:
        encoded = source.read(MAX_KEY + 1)
    if not encoded or len(encoded) > MAX_KEY:
        raise ValueError("The PEM key exceeds the supported size.")
    return encoded


def public_key(path):
    try:
        key = serialization.load_pem_public_key(read_pem(path))
    except ValueError:
        raise ValueError("Choose an Ed25519 public key in PEM format.") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Choose an Ed25519 public key in PEM format.")
    return key


def signing_key(path):
    encoded = read_pem(path)
    try:
        try:
            key = serialization.load_pem_private_key(encoded, password=None)
        except TypeError:
            password = getpass.getpass("Signing key passphrase: ").encode()
            key = serialization.load_pem_private_key(encoded, password=password)
    except (TypeError, ValueError):
        raise ValueError("Choose a readable Ed25519 private PEM key and confirm its passphrase.") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Choose an Ed25519 private key in PEM format.")
    return key


def trusted_key(store, publisher):
    if store.is_symlink():
        raise ValueError("The trusted publisher directory must be local.")
    try:
        key = public_key(store / (publisher + ".pem"))
    except FileNotFoundError:
        raise ValueError("This model pack signer is unknown. Ask IT to provision its public key.") from None
    if not hmac.compare_digest(fingerprint(key), publisher):
        raise ValueError("The registered public key does not match the pack's publisher fingerprint.")
    return key


def provision(store, source, expected):
    key = public_key(source)
    publisher = fingerprint(key)
    if not hmac.compare_digest(publisher, key_id(expected)):
        raise ValueError("The public key does not match the reviewed publisher fingerprint.")
    if store.is_symlink():
        raise ValueError("The trusted publisher directory must be local.")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store / (publisher + ".pem")
    encoded = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if path.exists() or path.is_symlink():
        trusted_key(store, publisher)
        return publisher
    staged = None
    try:
        with NamedTemporaryFile(dir=store, prefix=".publisher-", delete=False) as file:
            staged = Path(file.name)
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return publisher


def manifest(pack, publisher):
    return {
        "schema": 1, "modelId": pack.id, "filename": pack.filename, "sha256": pack.sha256,
        "bytes": pack.size, "license": pack.license, "sourceRepository": pack.repo,
        "sourceRevision": pack.revision, "publisherFingerprint": publisher,
    }


def read_signature(source, pack, store):
    if source.read(len(MAGIC)) != MAGIC:
        raise ValueError("This file is not a supported signed Penstock model pack.")
    field = source.read(LENGTH.size)
    if len(field) != LENGTH.size:
        raise ValueError("The signed model pack header is incomplete.")
    (length,) = LENGTH.unpack(field)
    if not 0 < length <= MAX_MANIFEST:
        raise ValueError("The signed model pack manifest exceeds the supported size.")
    raw = source.read(length)
    signature = source.read(SIGNATURE_BYTES)
    if len(raw) != length or len(signature) != SIGNATURE_BYTES:
        raise ValueError("The signed model pack manifest or signature is incomplete.")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("The signed model pack manifest is invalid.") from None
    if not isinstance(value, dict):
        raise ValueError("The signed model pack manifest is invalid.")
    publisher = key_id(value.get("publisherFingerprint"))
    if raw != canonical(manifest(pack, publisher)):
        raise ValueError("The signed manifest does not match the selected pinned model and license.")
    key = trusted_key(store, publisher)
    try:
        key.verify(signature, MAGIC + raw)
    except InvalidSignature:
        raise ValueError("The model pack's publisher signature failed verification.") from None
    return value


def check_payload_size(source, pack):
    if os.fstat(source.fileno()).st_size - source.tell() != pack.size:
        raise ValueError("The signed model pack payload size differs from its manifest.")


def export_pack(pack, source, key, output):
    from models import verify_stream

    if output.suffix.lower() != EXTENSION:
        raise ValueError(f"Name the export with the {EXTENSION} extension.")
    publisher = fingerprint(key.public_key())
    value = manifest(pack, publisher)
    raw = canonical(value)
    header = MAGIC + LENGTH.pack(len(raw)) + raw + key.sign(MAGIC + raw)
    if not source.is_file() or source.stat().st_size != pack.size:
        raise ValueError("Choose the exact pinned GGUF file for this model.")
    with source.open("rb") as gguf:
        if shutil.disk_usage(output.parent).free < pack.size + len(header):
            raise ValueError("The destination does not have enough free space for this signed pack.")
        try:
            stream = output.open("xb")
        except FileExistsError:
            raise ValueError("Choose a new export path. That file already exists.") from None
        created = os.fstat(stream.fileno())
        try:
            with stream:
                stream.write(header)
                verify_stream(pack, gguf, threading.Event(), target=stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                if os.path.samestat(output.lstat(), created):
                    output.unlink()
            except FileNotFoundError:
                pass
            raise
    return value


def verify_pack(pack, source, store):
    from models import verify_stream

    if not source.is_file():
        raise ValueError("Choose a local signed model pack file.")
    with source.open("rb") as container:
        value = read_signature(container, pack, store)
        check_payload_size(container, pack)
        verify_stream(pack, container, threading.Event())
    return value


def main():
    from models import PACKS

    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("fingerprint", help="Print the fingerprint of an Ed25519 public PEM key")
    show.add_argument("--public-key", required=True, type=Path)
    trust = actions.add_parser("trust", help="Provision a public key after reviewing its fingerprint")
    trust.add_argument("--public-key", required=True, type=Path)
    trust.add_argument("--fingerprint", required=True)
    trust.add_argument("--app-data", required=True, type=Path)
    export = actions.add_parser("export", help="Sign and export one of the approved GGUF models")
    export.add_argument("--model", choices=tuple(PACKS), required=True)
    export.add_argument("--gguf", required=True, type=Path)
    export.add_argument("--private-key", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    verify = actions.add_parser("verify", help="Check a signed pack entirely offline")
    verify.add_argument("--model", choices=tuple(PACKS), required=True)
    verify.add_argument("--pack", required=True, type=Path)
    verify.add_argument("--app-data", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "fingerprint":
            value = fingerprint(public_key(args.public_key))
        elif args.action == "trust":
            value = provision(args.app_data / TRUST_FOLDER, args.public_key, args.fingerprint)
        elif args.action == "export":
            value = export_pack(PACKS[args.model], args.gguf, signing_key(args.private_key), args.output)
        else:
            value = verify_pack(PACKS[args.model], args.pack, args.app_data / TRUST_FOLDER)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
