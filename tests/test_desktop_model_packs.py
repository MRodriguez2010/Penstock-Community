"""Exercise the offline package format and trust store with ephemeral signing identities."""
from contextlib import redirect_stdout
from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import model_packs
import models


class SignedModelPacks(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.cache = self.root / "models"
        self.trust = self.root / model_packs.TRUST_FOLDER
        self.bytes = b"GGUF temporary signed fixture, not a model"
        self.pack = replace(next(iter(models.PACKS.values())), size=len(self.bytes),
                            sha256=sha256(self.bytes).hexdigest())
        self.source = self.root / "tiny.gguf"
        self.source.write_bytes(self.bytes)
        self.signer = Ed25519PrivateKey.generate()
        self.publisher = model_packs.fingerprint(self.signer.public_key())
        self.public = self.root / "publisher-public.pem"
        self.write_public(self.public, self.signer)
        self.stop = threading.Event()

    def write_public(self, path, key):
        path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))

    def provision(self):
        return model_packs.provision(self.trust, self.public, self.publisher)

    def signed(self, name="signed.penstock-model"):
        destination = self.root / name
        return destination, model_packs.export_pack(self.pack, self.source, self.signer, destination)

    def rewrite(self, manifest, payload, *, key=None, name="bad.penstock-model"):
        raw = model_packs.canonical(manifest)
        header = model_packs.MAGIC + model_packs.LENGTH.pack(len(raw)) + raw
        signature = (key or self.signer).sign(model_packs.MAGIC + raw)
        path = self.root / name
        path.write_bytes(header + signature + payload)
        return path

    def test_export_provision_and_launch_import_entirely_offline(self):
        archive, exported = self.signed()
        with self.assertRaisesRegex(ValueError, "signer is unknown"):
            model_packs.verify_pack(self.pack, archive, self.trust)
        self.assertEqual(self.provision(), self.publisher)
        self.assertEqual(self.provision(), self.publisher)
        self.assertEqual(model_packs.verify_pack(self.pack, archive, self.trust), exported)
        self.assertEqual(exported["license"], "Apache-2.0")
        self.assertEqual(exported["sha256"], self.pack.sha256)
        self.assertEqual(exported["bytes"], self.pack.size)
        with patch.object(models, "direct_client") as network, patch.object(models, "emit"):
            installed = models.install(self.cache, self.pack, archive, self.stop)
            network.assert_not_called()
        self.assertEqual(installed["verification"], {"mode": "signed", "publisherFingerprint": self.publisher})
        self.assertEqual(models.cache_file(self.cache, self.pack).read_bytes(), self.bytes)

    def test_unapproved_signer_rejected_even_with_an_identical_cached_model(self):
        archive, _ = self.signed()
        with patch.object(models, "emit"):
            raw = models.install(self.cache, self.pack, self.source, self.stop)
            self.assertEqual(raw["verification"]["mode"], "pinned-gguf")
            with self.assertRaisesRegex(ValueError, "signer is unknown"):
                models.install(self.cache, self.pack, archive, self.stop)
        self.assertEqual(models.cache_file(self.cache, self.pack).read_bytes(), self.bytes)
        self.assertEqual(list(self.cache.iterdir()), [models.cache_file(self.cache, self.pack)])

    def test_fingerprint_confirmation_and_registered_key_identity(self):
        with self.assertRaisesRegex(ValueError, "reviewed publisher fingerprint"):
            model_packs.provision(self.trust, self.public, "0" * 64)
        self.assertFalse(self.trust.exists())
        self.provision()
        alternate = Ed25519PrivateKey.generate()
        self.write_public(self.trust / (self.publisher + ".pem"), alternate)
        archive, _ = self.signed()
        with self.assertRaisesRegex(ValueError, "does not match the pack's publisher fingerprint"):
            model_packs.verify_pack(self.pack, archive, self.trust)

    def test_signature_cannot_be_swapped_for_another_signer(self):
        self.provision()
        value = model_packs.manifest(self.pack, self.publisher)
        archive = self.rewrite(value, self.bytes, key=Ed25519PrivateKey.generate())
        with self.assertRaisesRegex(ValueError, "signature failed"):
            model_packs.verify_pack(self.pack, archive, self.trust)

    def test_a_valid_signature_cannot_override_the_model_or_license_pins(self):
        self.provision()
        expected = model_packs.manifest(self.pack, self.publisher)
        for field, replacement in (
            ("license", "MIT"), ("bytes", self.pack.size + 1), ("sha256", "0" * 64),
            ("modelId", "another-model"), ("sourceRevision", "0" * 40), ("filename", "other.gguf"),
        ):
            with self.subTest(field=field):
                archive = self.rewrite({**expected, field: replacement}, self.bytes)
                with self.assertRaisesRegex(ValueError, "does not match the selected"):
                    model_packs.verify_pack(self.pack, archive, self.trust)

    def test_prefix_and_payload_are_bounded_and_never_install_tampering(self):
        self.provision()
        manifest = model_packs.manifest(self.pack, self.publisher)
        with patch.object(models, "emit"):
            for bad in (self.bytes[:-1] + b"X", self.bytes[:-1], self.bytes + b"X"):
                archive = self.rewrite(manifest, bad)
                with self.assertRaisesRegex(ValueError, "SHA-256 or size|payload size differs"):
                    models.install(self.cache, self.pack, archive, self.stop)
                self.assertEqual(list(self.cache.iterdir()), [])
            archive.write_bytes(model_packs.MAGIC + model_packs.LENGTH.pack(model_packs.MAX_MANIFEST + 1))
            with self.assertRaisesRegex(ValueError, "manifest exceeds"):
                models.install(self.cache, self.pack, archive, self.stop)
            self.assertEqual(list(self.cache.iterdir()), [])

    def test_export_path_is_exclusive_and_bad_payload_removes_incomplete_export(self):
        destination, _ = self.signed()
        original = destination.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            model_packs.export_pack(self.pack, self.source, self.signer, destination)
        self.assertEqual(destination.read_bytes(), original)
        self.source.write_bytes(self.bytes[:-1] + b"X")
        other = self.root / "invalid.penstock-model"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            model_packs.export_pack(self.pack, self.source, self.signer, other)
        self.assertFalse(other.exists())

    def test_it_cli_workflow_with_ephemeral_signer_and_tiny_weights(self):
        private = self.root / "publisher-private.pem"
        private.write_bytes(self.signer.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        target = self.root / "transport.penstock-model"

        def command(*args):
            out = io.StringIO()
            with patch.object(sys, "argv", ["model_packs.py", *map(str, args)]), redirect_stdout(out):
                model_packs.main()
            return out.getvalue().strip()

        with patch.object(models, "PACKS", {self.pack.id: self.pack}), patch("model_packs.os.umask"):
            fingerprint = command("fingerprint", "--public-key", self.public)
            self.assertEqual(fingerprint, self.publisher)
            command("trust", "--public-key", self.public, "--fingerprint", fingerprint, "--app-data", self.root)
            command("export", "--model", self.pack.id, "--gguf", self.source, "--private-key", private,
                    "--output", target)
            verified = json.loads(command("verify", "--model", self.pack.id, "--pack", target,
                                          "--app-data", self.root))
        self.assertEqual(verified, model_packs.manifest(self.pack, self.publisher))

    def test_cancelled_transfer_does_not_replace_a_cached_model(self):
        self.provision()
        archive, _ = self.signed()
        self.cache.mkdir()
        current = models.cache_file(self.cache, self.pack)
        current.write_bytes(b"previous cached bytes")
        self.stop.set()
        with self.assertRaisesRegex(ValueError, "cancelled"):
            models.install(self.cache, self.pack, archive, self.stop)
        self.assertEqual(current.read_bytes(), b"previous cached bytes")
        self.assertEqual(list(self.cache.iterdir()), [current])
