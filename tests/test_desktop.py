"""Regression tests for the opt-in desktop model and workspace contract."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import threading
from unittest import TestCase, mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "desktop"))
from connections import Connections

spec = importlib.util.spec_from_file_location("penstock_desktop_models", ROOT / "desktop/models.py")
models = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = models
spec.loader.exec_module(models)


class DesktopBuild(TestCase):
    def test_x64_build_stages_both_runners_and_arm_stages_baseline(self):
        spec = importlib.util.spec_from_file_location("desktop_stage", ROOT / "desktop/build/stage.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        for machine in ("x86_64", "arm64"):
            with self.subTest(machine=machine), TemporaryDirectory() as directory:
                root = Path(directory)
                staging = root / "stage"
                (staging / "licenses").mkdir(parents=True)

                def build_command(program, *args, cwd=None):
                    if program == "git" and args[0] == "checkout":
                        (cwd / "LICENSE").write_text("license fixture", encoding="utf-8")
                    if program == "cmake" and args[0] == "--build":
                        destination = Path(args[1]) / "bin" / stage.exec_name("llama-server")
                        destination.parent.mkdir(parents=True)
                        destination.touch()

                with mock.patch.object(stage, "STAGE", staging), \
                        mock.patch.object(stage.platform, "machine", return_value=machine), \
                        mock.patch.object(stage, "run", side_effect=build_command) as run:
                    stage.build_inference(root)
                runners = {"llama-server"}
                if machine == "x86_64":
                    runners.add("llama-server-avx2")
                self.assertEqual({p.name for p in (staging / "inference").iterdir()},
                                 {stage.exec_name(name) for name in runners})
                configs = [c.args for c in run.call_args_list if c.args[:2] == ("cmake", "-S")]
                self.assertEqual(len(configs), len(runners))
                for config, mode in zip(configs, ("OFF", "ON")):
                    self.assertIn("-DGGML_NATIVE=OFF", config)
                    for cpu_option in ("AVX", "AVX2", "SSE42", "FMA", "F16C", "BMI2"):
                        self.assertIn(f"-DGGML_{cpu_option}={mode}", config)


class DesktopModels(TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.bytes = b"GGUFtest bytes"
        self.pack = replace(next(iter(models.PACKS.values())), size=len(self.bytes),
                            sha256=sha256(self.bytes).hexdigest())

    def test_manifest_models_are_pinned_and_ports_are_distinct(self):
        self.assertEqual(set(models.PACKS), {"smollm3-3b", "olmo-3-7b-think"})
        self.assertEqual(len({pack.port for pack in models.PACKS.values()}), 2)
        for pack in models.PACKS.values():
            self.assertRegex(pack.revision, r"^[a-f0-9]{40}$")
            self.assertRegex(pack.sha256, r"^[a-f0-9]{64}$")
            self.assertIn(f"/resolve/{pack.revision}/", pack.url)
            self.assertTrue(models.trusted_download_url(pack.url))

    def test_only_known_https_download_hosts_can_redirect(self):
        for url in (
            "https://us.aws.cdn.hf.co/signed?file=abc",
            "https://cas-bridge.xethub.hf.co/model",
        ):
            self.assertTrue(models.trusted_download_url(url))
        for url in (
            "http://huggingface.co/model", "https://huggingface.co.evil.test/model",
            "https://evil.test@huggingface.co/model", "https://huggingface.co:80/model",
            "https://127.0.0.1/model"
        ):
            self.assertFalse(models.trusted_download_url(url))
            with self.assertRaises(ValueError):
                models.ModelRedirect().redirect_request(None, None, 302, "", {}, url)

    def test_stream_rejects_wrong_length_bytes_and_magic(self):
        for text in (b"not a model", self.bytes[:-1], self.bytes + b"!", b"GGUFbad bytes!"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                models.verify_stream(self.pack, BytesIO(text), self.stop)

    def test_offline_import_is_verified_and_has_no_network(self):
        with TemporaryDirectory() as directory, mock.patch.object(models, "direct_client") as network, \
                mock.patch.object(models, "emit"):
            root = Path(directory)
            incoming = root / "download.gguf"
            incoming.write_bytes(self.bytes)
            cache = root / "cache"
            result = models.install(cache, self.pack, incoming, self.stop)
            self.assertEqual(result["installed"], self.pack.id)
            self.assertEqual(models.verified_file(cache, self.pack, self.stop).read_bytes(), self.bytes)
            self.assertEqual(len(list(cache.iterdir())), 1)
            network.assert_not_called()

    def test_failed_import_keeps_cached_file_and_cleans_staging(self):
        with TemporaryDirectory() as directory, mock.patch.object(models, "emit"):
            root = Path(directory)
            incoming = root / "bad.gguf"
            incoming.write_bytes(b"GGUFbad bytes!")
            cache = root / "cache"
            cache.mkdir()
            cached = models.cache_file(cache, self.pack)
            cached.write_bytes(b"previous data")
            with self.assertRaises(ValueError):
                models.install(cache, self.pack, incoming, self.stop)
            self.assertEqual(cached.read_bytes(), b"previous data")
            self.assertEqual(list(cache.iterdir()), [cached])

    def test_endpoint_configuration_does_not_make_a_request(self):
        profile = {"url": "https://models.example.test/v1/chat/completions",
                   "model": "example", "protocol": "chat-completions"}
        with mock.patch.object(models, "direct_client") as network:
            self.assertEqual(models.validate_endpoint(profile), profile)
            for url in ("http://models.example.test/v1", "https://token@models.example.test/v1",
                        "https://models.example.test/v1?token=x", "https://models.example.test/v1#secret"):
                with self.assertRaises(ValueError):
                    models.validate_endpoint({**profile, "url": url})
            network.assert_not_called()

    def test_custom_endpoint_test_keeps_ordinary_request_even_for_pack_alias_and_port(self):
        profile = {"url": "http://127.0.0.1:45371/v1/chat/completions",
                   "model": self.pack.id, "protocol": "chat-completions"}
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=BytesIO(
            b'{"choices":[{"message":{"content":"OK"}}]}'
        ))
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(models, "direct_client", return_value=opener):
            self.assertEqual(models.probe(profile, "key-abc"), {"verified": True})
        args, options = opener.open.call_args
        self.assertEqual(args[0].get_header("Authorization"), "Bearer key-abc")
        self.assertEqual(json.loads(args[0].data), {
            "model": self.pack.id, "messages": [{"role": "user", "content": "Reply with the word OK."}],
            "stream": False, "max_tokens": 64,
        })
        self.assertEqual(options["timeout"], 45)

    def test_managed_pack_probe_caps_reasoning_and_requires_visible_answer(self):
        opener = mock.MagicMock()
        with mock.patch.object(models, "direct_client", return_value=opener):
            for pack in models.PACKS.values():
                with self.subTest(pack=pack.id):
                    body = b'{"choices":[{"message":{"content":"OK","reasoning_content":"details"}}]}'
                    opener.open.return_value.__enter__.return_value = BytesIO(body)
                    self.assertEqual(models.probe_pack(pack, "x" * 32), {"verified": True})
                    args, options = opener.open.call_args
                    self.assertEqual(args[0].full_url, f"http://127.0.0.1:{pack.port}/v1/chat/completions")
                    self.assertEqual(args[0].get_header("Authorization"), "Bearer " + "x" * 32)
                    payload = json.loads(args[0].data)
                    self.assertEqual(payload["max_tokens"], 48)
                    self.assertEqual(payload["reasoning_budget_tokens"], 0)
                    if pack.id == "smollm3-3b":
                        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
                    else:
                        self.assertNotIn("chat_template_kwargs", payload)
                    self.assertEqual(options["timeout"], 90)
                    body = b'{"choices":[{"message":{"content":"","reasoning_content":"details"}}]}'
                    opener.open.return_value.__enter__.return_value = BytesIO(body)
                    with self.assertRaisesRegex(ValueError, "returned no answer"):
                        models.probe_pack(pack, "x" * 32)

    def test_explicit_endpoint_probe_refuses_redirects(self):
        handler = models.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 307, "", {}, "http://127.0.0.1/other"))

    def test_occupied_local_port_receives_no_managed_session_key(self):
        exited = mock.Mock()
        exited.stdout = BytesIO(b"could not bind HTTP server socket\n")
        exited.poll.return_value = 1
        with TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "PENSTOCK_DESKTOP_MODEL_KEY": "F" * 64
        }), mock.patch.object(models.subprocess, "Popen", return_value=exited), \
                mock.patch.object(models, "direct_client") as network:
            cache = Path(directory)
            models.cache_file(cache, self.pack).write_bytes(self.bytes)
            with self.assertRaisesRegex(ValueError, "port may already be in use"):
                models.serve(cache, self.pack, Path("fixture-runner"), self.stop)
        network.return_value.open.assert_not_called()

    def test_optimized_runtime_selection_and_baseline_fallback(self):
        baseline, optimized, model = Path("baseline"), Path("optimized"), Path("pinned.gguf")
        with mock.patch.object(models, "verified_file", return_value=model) as verify, \
                mock.patch.object(models, "run_local_model") as run, \
                mock.patch.dict(os.environ, {"PENSTOCK_DESKTOP_MODEL_KEY": "F" * 64}):
            models.serve(Path("cache"), self.pack, baseline, self.stop, optimized=optimized)
            run.assert_called_once_with(model, self.pack, optimized, "F" * 64, self.stop, "avx2")
            verify.assert_called_once()
        with mock.patch.object(models, "verified_file", return_value=model) as verify, \
                mock.patch.object(models, "run_local_model", side_effect=[models.RunnerStartError(), None]) as run, \
                mock.patch.dict(os.environ, {"PENSTOCK_DESKTOP_MODEL_KEY": "F" * 64}):
            models.serve(Path("cache"), self.pack, baseline, self.stop, optimized=optimized)
            self.assertEqual([call.args[-1] for call in run.call_args_list], ["avx2", "baseline"])
            verify.assert_called_once()

    def test_cancellation_or_bound_listener_error_never_falls_back(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                def fail(*_args):
                    if cancel:
                        self.stop.set()
                        raise models.RunnerStartError()
                    raise ValueError("listener authentication failed")

                with mock.patch.object(models, "verified_file", return_value=Path("model")), \
                        mock.patch.object(models, "run_local_model", side_effect=fail) as run, \
                        mock.patch.dict(os.environ, {"PENSTOCK_DESKTOP_MODEL_KEY": "F" * 64}):
                    args = (Path("cache"), self.pack, Path("baseline"), self.stop)
                    if cancel:
                        models.serve(*args, optimized=Path("optimized"))
                    else:
                        with self.assertRaisesRegex(ValueError, "listener authentication failed"):
                            models.serve(*args, optimized=Path("optimized"))
                    run.assert_called_once()


class DesktopProvider(TestCase):
    def test_overlay_preserves_existing_workspace_configuration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "connections.toml"
            raw = ('[providers.original]\nprotocol="ollama"\n'
                   'url="http://127.0.0.1:11434/api/chat"\nmodel="existing"\n')
            path.write_text(raw, encoding="utf-8")
            connections = Connections(path.parent)
            profile = {"protocol": "chat-completions", "model": "smollm3-3b",
                       "url": "http://127.0.0.1:45371/v1/chat/completions",
                       "key_env": "PENSTOCK_DESKTOP_MODEL_KEY"}
            selection = json.dumps({"desktop-smollm3-3b": profile})
            connections.add_desktop_providers(selection)
            self.assertEqual(connections.providers["desktop-smollm3-3b"], profile)
            self.assertIn("original", connections.providers)
            self.assertEqual(path.read_text("utf-8"), raw)
            with self.assertRaisesRegex(ValueError, "already defines"):
                connections.add_desktop_providers(selection)

    def test_longer_completion_timeout_requires_exact_launcher_owned_pack(self):
        name = "desktop-smollm3-3b"
        profile = {"protocol": "chat-completions", "model": "smollm3-3b",
                   "url": "http://127.0.0.1:45371/v1/chat/completions", "key_env": "PENSTOCK_DESKTOP_MODEL_KEY"}
        with TemporaryDirectory() as directory, mock.patch("connections.post") as post, \
                mock.patch.dict(os.environ, {"PENSTOCK_DESKTOP_MODEL_KEY": "F" * 64}):
            path = Path(directory) / "connections.toml"
            path.write_text("", encoding="utf-8")
            post.return_value = {"choices": [{"message": {"content": "OK"}}]}
            for change in ({"url": "https://host.invalid/v1/chat/completions"},
                           {"url": "http://127.0.0.1:45372/v1/chat/completions"},
                           {"model": "external"}, {"key_env": "CUSTOM_KEY"}):
                connections = Connections(path.parent)
                with self.assertRaisesRegex(ValueError, "bundled loopback runtime"):
                    connections.add_desktop_providers(json.dumps({name: {**profile, **change}}))
            connections = Connections(path.parent)
            connections.add_desktop_providers(json.dumps({name: profile}))
            self.assertEqual(connections.complete(name, "System", "Prompt"), "OK")
            self.assertEqual(post.call_args.kwargs, {"timeout": 300})
            self.assertNotIn("response_format", post.call_args.args[1])
            self.assertEqual(connections.complete(name, "System", "Prompt", draft=True), "OK")
            payload = post.call_args.args[1]
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(payload["reasoning_effort"], "none")
            connections = Connections(path.parent)
            olmo = {**profile, "model": "olmo-3-7b-think",
                    "url": "http://127.0.0.1:45372/v1/chat/completions"}
            connections.add_desktop_providers(json.dumps({"desktop-olmo-3-7b-think": olmo}))
            self.assertEqual(connections.complete("desktop-olmo-3-7b-think", "System", "Prompt", draft=True), "OK")
            payload = post.call_args.args[1]
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(payload["reasoning_budget_tokens"], 128)
            self.assertNotIn("chat_template_kwargs", payload)
            connections = Connections(path.parent)
            connections.add_desktop_providers(json.dumps({"desktop-endpoint": profile}))
            self.assertEqual(connections.complete("desktop-endpoint", "System", "Prompt", draft=True), "OK")
            self.assertEqual(post.call_args.kwargs, {})
            self.assertEqual(set(post.call_args.args[1]), {"model", "messages", "stream"})
            path.write_text(f"[providers.{name}]\n" + "".join(f'{key}="{value}"\n' for key, value in profile.items()),
                            encoding="utf-8")
            connections = Connections(path.parent)
            self.assertEqual(connections.complete(name, "System", "Prompt"), "OK")
            self.assertEqual(post.call_args.kwargs, {})
