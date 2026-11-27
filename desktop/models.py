"""Native desktop model installation, testing and process supervision."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from urllib import error, request
from urllib.parse import urlsplit

import model_packs

MIB = 1024 ** 2
CHUNK = 4 * MIB


@dataclass(frozen=True)
class Pack:
    id: str
    name: str
    repo: str
    revision: str
    filename: str
    sha256: str
    size: int
    port: int
    context: int
    memory: int
    license: str

    @property
    def url(self):
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.filename}"


PACKS = {
    pack.id: pack for pack in (
        Pack("smollm3-3b", "SmolLM3-3B", "ggml-org/SmolLM3-3B-GGUF",
             "4965cb60b150737b68a0408c36aeefb65078f894", "SmolLM3-Q4_K_M.gguf",
             "8334b850b7bd46238c16b0c550df2138f0889bf433809008cc17a8b05761863e", 1915305312,
             45371, 4096, 6, "Apache-2.0"),
        Pack("olmo-3-7b-think", "Olmo 3 7B Think", "bartowski/allenai_Olmo-3-7B-Think-GGUF",
             "d5a1bf052bdb80f09cd79eee4d2037c3f8ced190", "allenai_Olmo-3-7B-Think-Q4_K_M.gguf",
             "4a4a31e4dd68e6ad4fdd876605265fadabd72d6b1aeefbe04a01531dbc176126", 4471600832,
             45372, 8192, 12, "Apache-2.0"),
    )
}


def trusted_download_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https" and parsed.port in (None, 443)
        and parsed.username is None and parsed.password is None
        and not parsed.fragment
        and (
            host == "huggingface.co" or host == "cdn-lfs.huggingface.co"
            or host in {"cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co", "cdn-lfs-eu-1.hf.co"}
            or host.endswith(".aws.cdn.hf.co")
            or host == "cas-bridge.xethub.hf.co"
        )
    )


class ModelRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_download_url(newurl):
            raise ValueError("The download tried to leave the trusted model hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def direct_client(*handlers):
    return request.build_opener(request.ProxyHandler({}), *handlers)


@contextmanager
def source_for(pack, offline, trust):
    if not offline.is_file():
        raise ValueError("Choose a local GGUF or signed model pack file.")
    with offline.open("rb") as source:
        prefix = source.read(len(model_packs.MAGIC))
        source.seek(0)
        if prefix == model_packs.MAGIC or offline.suffix.lower() == model_packs.EXTENSION:
            envelope = model_packs.read_signature(source, pack, trust)
            model_packs.check_payload_size(source, pack)
            proof = {"mode": "signed", "publisherFingerprint": envelope["publisherFingerprint"]}
        else:
            if os.fstat(source.fileno()).st_size != pack.size:
                raise ValueError("Choose the exact GGUF file for this pack. Its size does not match.")
            proof = {"mode": "pinned-gguf"}
        yield source, proof


def verify_stream(pack, source, stop, target=None, progress=None):
    digest = sha256()
    size = 0
    last_report = 0.0
    while True:
        if stop.is_set():
            raise ValueError("Model setup was cancelled.")
        chunk = source.read(min(CHUNK, pack.size - size + 1))
        if not chunk:
            break
        if size == 0 and not chunk.startswith(b"GGUF"):
            raise ValueError("Expected a GGUF model file.")
        size += len(chunk)
        if size > pack.size:
            raise ValueError("The model file exceeds the expected size.")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
        if progress is not None and time.monotonic() - last_report > 0.25:
            progress(size)
            last_report = time.monotonic()
    if size != pack.size or not secrets.compare_digest(digest.hexdigest(), pack.sha256):
        raise ValueError("The model file failed SHA-256 or size verification.")
    if progress is not None:
        progress(size)


def cache_file(cache, pack):
    return cache / pack.filename


def regular_file(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def verified_file(cache, pack, stop):
    path = cache_file(cache, pack)
    if not regular_file(path) or path.stat().st_size != pack.size:
        raise ValueError("Install or import this model pack first.")
    with path.open("rb") as source:
        verify_stream(pack, source, stop)
    return path


def emit(data):
    print(json.dumps(data, ensure_ascii=True), flush=True)


def install(cache, pack, offline, stop):
    from model_downloads import discard, download

    def progress(size):
        emit({"progress": size, "total": pack.size})

    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    trust = cache.parent / model_packs.TRUST_FOLDER
    destination = cache_file(cache, pack)
    if destination.is_symlink():
        raise ValueError("The model destination must be a regular file in the desktop model folder.")
    if regular_file(destination) and destination.stat().st_size == pack.size:
        try:
            verified_file(cache, pack, stop)
        except ValueError:
            if stop.is_set():
                raise
        else:
            proof = {"mode": "cached"}
            if offline is not None:
                # The selected import still needs its own signature and byte checks.
                with source_for(pack, offline, trust) as (source, proof):
                    verify_stream(pack, source, stop, progress=progress)
            discard(destination)
            return {"installed": pack.id, "bytes": pack.size, "verification": proof}
    if offline is None:
        proof = download(destination, pack, stop, progress)
        return {"installed": pack.id, "bytes": pack.size, "verification": proof}
    partial = cache / (pack.filename + ".penstock-partial")
    if regular_file(partial):
        partial.unlink()
    if shutil.disk_usage(cache).free < pack.size + 512 * MIB:
        raise ValueError("Free at least the model size plus 512 MiB in the desktop data location.")
    try:
        with source_for(pack, offline, trust) as (source, proof), partial.open("xb") as target:
            verify_stream(pack, source, stop, target, progress=progress)
            target.flush()
            os.fsync(target.fileno())
        if stop.is_set():
            raise ValueError("Model setup was cancelled.")
        os.replace(partial, destination)
        discard(destination)
    finally:
        if regular_file(partial):
            partial.unlink()
    return {"installed": pack.id, "bytes": pack.size, "verification": proof}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def validate_endpoint(profile):
    url, protocol, model = (profile.get(key, "") for key in ("url", "protocol", "model"))
    if not all(isinstance(value, str) for value in (url, protocol, model)):
        raise ValueError("Enter an endpoint, protocol and model ID.")
    if not 1 <= len(model.strip()) <= 128 or any(ord(c) < 32 for c in model):
        raise ValueError("Enter a model ID of 1–128 characters.")
    if len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Enter a complete endpoint URL.")
    endpoint = urlsplit(url)
    if (not endpoint.hostname or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment or not endpoint.path
            or endpoint.port == 0 or "\\" in url):
        raise ValueError("Use a full endpoint path without credentials, query or fragment.")
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1"}
    ):
        raise ValueError("Use HTTPS for remote endpoints or HTTP with 127.0.0.1 or ::1.")
    if protocol not in {"chat-completions", "ollama", "anthropic-messages"}:
        raise ValueError("Choose a supported model protocol.")
    return {"url": url, "protocol": protocol, "model": model.strip()}


def probe(profile, key=None, *, managed=False):
    profile = validate_endpoint(profile)
    protocol = profile["protocol"]
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": profile["model"],
        "messages": [{"role": "user", "content": "Reply with the word OK."}],
        "stream": False,
    }
    if protocol == "ollama":
        payload["options"] = {"num_predict": 64, "num_ctx": 2048}
    else:
        payload["max_tokens"] = 64
    if managed:
        payload.update(max_tokens=48, temperature=0, reasoning_budget_tokens=0)
        # SmolLM3 has a non-thinking mode. Olmo Think's template starts a
        # thinking turn, so let its parser end that turn with the zero budget.
        if profile["model"] == "smollm3-3b":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"
    if protocol == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    req = request.Request(profile["url"], data=json.dumps(payload).encode(), headers=headers)
    with direct_client(NoRedirect()).open(req, timeout=90 if managed else 45) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("The test response exceeded 64 KiB.")
    result = json.loads(data)
    if protocol == "ollama":
        answer = result["message"]["content"]
    elif protocol == "anthropic-messages":
        answer = " ".join(entry["text"] for entry in result["content"] if entry["type"] == "text")
    else:
        answer = result["choices"][0]["message"]["content"]
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("The model test returned no answer. Check the model and protocol.")
    return {"verified": True}


def probe_pack(pack, key):
    if not key or len(key) < 32:
        raise ValueError("Missing the desktop model session key.")
    profile = {"model": pack.id, "protocol": "chat-completions",
               "url": f"http://127.0.0.1:{pack.port}/v1/chat/completions"}
    return probe(profile, key, managed=True)


def watch_listener(stream, endpoint, listening):
    # The pinned runner reports its bound address only after the listener starts.
    marker = ("listening on " + endpoint).encode("ascii")
    for line in stream:
        if line.rstrip().endswith(marker):
            listening.set()


class RunnerStartError(ValueError):
    pass


def runner_threads():
    count = getattr(os, "process_cpu_count", os.cpu_count)() or 2
    if hasattr(os, "sched_getaffinity"):
        try:
            count = min(count, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text("ascii").split()
        if quota != "max":
            count = min(count, int(quota) // int(period))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return max(1, min(8, count))


def serve(cache, pack, executable, stop, *, optimized=None):
    model = verified_file(cache, pack, stop)
    key = os.environ.pop("PENSTOCK_DESKTOP_MODEL_KEY", None)
    if not key or len(key) < 32:
        raise ValueError("Missing the desktop model session key.")
    if optimized is not None and not stop.is_set():
        try:
            run_local_model(model, pack, optimized, key, stop, "avx2")
            return
        except RunnerStartError:
            # Retry only before the optimized runner announced its own listener.
            pass
    if not stop.is_set():
        run_local_model(model, pack, executable, key, stop, "baseline")


def run_local_model(model, pack, executable, key, stop, variant):
    env = {name: value for name, value in os.environ.items() if name in {
        "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR",
    }}
    env["LLAMA_API_KEY"] = key
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    threads = str(runner_threads())
    command = [
        str(executable), "--model", str(model), "--alias", pack.id,
        "--host", "127.0.0.1", "--port", str(pack.port), "--ctx-size", str(pack.context),
        "--parallel", "1", "--n-gpu-layers", "0", "--no-webui",
        "--threads", threads, "--threads-batch", threads,
        "--n-predict", "1024", "--reasoning-budget", "512", "--timeout", "310",
    ]
    endpoint = f"http://127.0.0.1:{pack.port}"
    try:
        child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, creationflags=flags)
    except OSError:
        raise RunnerStartError("The bundled model runtime could not start.") from None
    listening = threading.Event()
    log_reader = threading.Thread(target=watch_listener, args=(child.stdout, endpoint, listening), daemon=True)
    log_reader.start()
    try:
        client = direct_client(NoRedirect())
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not stop.is_set():
            if child.poll() is not None:
                message = "The model runtime exited. Its port may already be in use or memory may be low."
                if not listening.is_set():
                    raise RunnerStartError(message)
                raise ValueError(message)
            if not listening.is_set():
                stop.wait(0.25)
                continue
            try:
                req = request.Request(endpoint + "/v1/models", headers={"Authorization": "Bearer " + key})
                with client.open(req, timeout=1) as response:
                    data = json.loads(response.read(16385))
                if not any(item.get("id") == pack.id for item in data["data"]):
                    raise ValueError("The local model port is already serving a different model.")
                try:
                    with client.open(endpoint + "/v1/models", timeout=1):
                        raise ValueError("The local model port is already in use or missing session authentication.")
                except error.HTTPError as exc:
                    if exc.code != 401:
                        raise ValueError("The local model endpoint did not confirm authentication.") from None
                if child.poll() is not None:
                    raise ValueError("The model runtime exited before it was ready.")
                emit({"ready": True, "runner": variant})
                while not stop.wait(0.25) and child.poll() is None:
                    pass
                return
            except error.HTTPError as exc:
                if exc.code not in (401, 503):
                    raise ValueError("The local model endpoint failed its readiness check.") from None
            except (error.URLError, TimeoutError):
                pass
            stop.wait(0.25)
        if not stop.is_set():
            raise ValueError("The model runtime did not become ready within two minutes.")
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log_reader.join(timeout=2)
        child.stdout.close()


def parent_lifetime():
    stop = threading.Event()

    def watch():
        try:
            while os.read(sys.stdin.fileno(), 1):
                pass
        finally:
            stop.set()

    threading.Thread(target=watch, daemon=True).start()
    return stop


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "install", "discard-download", "serve",
                                          "validate-endpoint", "test-endpoint", "test-pack"))
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--pack", choices=tuple(PACKS))
    parser.add_argument("--offline", type=Path)
    parser.add_argument("--llama", type=Path)
    parser.add_argument("--llama-avx2", type=Path)
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        # Installed builds carry TLS roots independently of the user's Python installation.
        import certifi
        if getattr(sys, "frozen", False):
            os.environ["SSL_CERT_FILE"] = certifi.where()
        if args.action in ("validate-endpoint", "test-endpoint"):
            profile = json.load(sys.stdin)
            key = os.environ.pop("PENSTOCK_DESKTOP_MODEL_KEY", None)
            emit(validate_endpoint(profile) if args.action == "validate-endpoint" else probe(profile, key))
            return 0
        if args.action == "test-pack":
            emit(probe_pack(PACKS[args.pack], os.environ.pop("PENSTOCK_DESKTOP_MODEL_KEY", None)))
            return 0
        if args.cache is None:
            raise ValueError("The desktop model cache was not provided.")
        if args.action == "status":
            from model_downloads import resume_bytes
            emit({"publisherKeyFolder": str(args.cache.parent / model_packs.TRUST_FOLDER), "packs": [
                {**asdict(pack), "url": pack.url, "pendingBytes": resume_bytes(cache_file(args.cache, pack), pack), "present":
                 regular_file(cache_file(args.cache, pack)) and cache_file(args.cache, pack).stat().st_size == pack.size}
                for pack in PACKS.values()
            ]})
        elif args.action == "discard-download":
            from model_downloads import discard
            discard(cache_file(args.cache, PACKS[args.pack]))
            emit({"discarded": True})
        elif args.action == "install":
            emit(install(args.cache, PACKS[args.pack], args.offline, parent_lifetime()))
        elif args.action == "serve":
            serve(args.cache, PACKS[args.pack], args.llama, parent_lifetime(), optimized=args.llama_avx2)
        return 0
    except error.HTTPError as exc:
        emit({"error": f"The endpoint returned HTTP {exc.code}."})
    except (error.URLError, TimeoutError):
        emit({"error": "The endpoint did not respond. Check its address and availability."})
    except (OSError, TypeError, KeyError, AttributeError, IndexError) as exc:
        emit({"error": "Model setup could not complete. Check the endpoint, model file and available storage."})
    except ValueError as exc:
        emit({"error": str(exc)[:300]})
    return 1
