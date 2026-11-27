"""Create Tauri resources on the matching native build host."""
from hashlib import file_digest
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "desktop/src-tauri/stage"
NODE_VERSION = "22.23.5"
LLAMA_REVISION = "171527522610996f5f0acf12334e185156ca8f44"
NATIVE = {
    ("Linux", "x86_64"): ("linux-x64", "tar.xz", "cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8"),
    ("Darwin", "arm64"): ("darwin-arm64", "tar.xz", "d0c68c321161bb2c6483efea9a343010dea8e5e7f60d73c6a7eba140016d662f"),
    ("Darwin", "x86_64"): ("darwin-x64", "tar.xz", "d68bf234a07781141dfca61ae4461c24560f70bd53223d76a41529eb02e8554e"),
    ("Windows", "AMD64"): ("win-x64", "zip", "4a191082e9706866830379d577d2cc8a50c776fcd9436a8d061b063ed0d9a0e3"),
}


def exec_name(name):
    return name + (".exe" if os.name == "nt" else "")


def run(*parts, cwd=ROOT):
    subprocess.run(parts, cwd=cwd, check=True)


def freeze_backend(scratch):
    paths = ("src", "examples", "server", "runtime", "node_modules")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "penstock-backend", "--distpath", str(scratch / "dist"),
        "--workpath", str(scratch / "pyinstaller"), "--specpath", str(scratch),
        "--paths", str(ROOT), "--paths", str(ROOT / "desktop"),
        "--paths", str(ROOT / "server"), "--collect-data", "certifi",
        "--collect-data", "tzdata",
        *(argument for path in paths for argument in ("--add-data", f"{ROOT / path}:{path}")),
        str(ROOT / "desktop/build/sidecar.py"),
    ], cwd=ROOT, check=True)
    shutil.copytree(scratch / "dist/penstock-backend", STAGE / "backend", symlinks=True)


def fetch_node(scratch):
    variant, extension, expected = NATIVE[platform.system(), platform.machine()]
    prefix = f"node-v{NODE_VERSION}-{variant}"
    name = f"{prefix}.{extension}"
    archive = scratch / name
    url = f"https://nodejs.org/dist/v{NODE_VERSION}/{name}"
    with urlopen(url, timeout=45) as response, archive.open("wb") as out:
        shutil.copyfileobj(response, out)
    with archive.open("rb") as input_file:
        if file_digest(input_file, "sha256").hexdigest() != expected:
            raise RuntimeError("The archived Node.js runtime did not match the pinned checksum.")
    staging = STAGE / "node"
    staging.mkdir()
    binary = exec_name("node")
    if extension == "zip":
        with zipfile.ZipFile(archive) as files:
            (staging / binary).write_bytes(files.read(f"{prefix}/{binary}"))
            (STAGE / "licenses/Node-LICENSE.txt").write_bytes(files.read(f"{prefix}/LICENSE"))
    else:
        with tarfile.open(archive) as files:
            for source, destination in (
                (f"{prefix}/bin/node", staging / binary),
                (f"{prefix}/LICENSE", STAGE / "licenses/Node-LICENSE.txt"),
            ):
                with files.extractfile(source) as input_file, destination.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)
        (staging / binary).chmod(0o755)


def build_inference(scratch):
    source = scratch / "llama.cpp"
    source.mkdir()
    run("git", "init", cwd=source)
    if os.name == "nt":
        run("git", "config", "--local", "core.longpaths", "true", cwd=source)
    run("git", "fetch", "--depth=1", "https://github.com/ggml-org/llama.cpp.git", LLAMA_REVISION, cwd=source)
    run("git", "checkout", "--detach", LLAMA_REVISION, cwd=source)
    inference = STAGE / "inference"
    inference.mkdir()
    built = compile_inference(source, scratch / "llama-build", avx2=False)
    binary = inference / exec_name("llama-server")
    shutil.copy2(built, binary)
    run(str(binary), "--version")
    if platform.machine() in {"x86_64", "AMD64"}:
        built = compile_inference(source, scratch / "llama-build-avx2", avx2=True)
        # The build host may have a different CPU than the installed machine.
        shutil.copy2(built, inference / exec_name("llama-server-avx2"))
    shutil.copyfile(source / "LICENSE", STAGE / "licenses/llama.cpp-LICENSE.txt")


def compile_inference(source, build, *, avx2):
    deployment = ("-DCMAKE_OSX_DEPLOYMENT_TARGET=12.0",) if platform.system() == "Darwin" else ()
    simd = "ON" if avx2 else "OFF"
    run(
        "cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF", "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_APP=OFF", "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
        "-DLLAMA_OPENSSL=OFF", "-DLLAMA_CONNECT=OFF", "-DGGML_NATIVE=OFF",
        "-DGGML_OPENMP=OFF", "-DGGML_METAL=OFF", "-DGGML_ACCELERATE=OFF",
        "-DGGML_LLAMAFILE=OFF",
        *(f"-DGGML_{feature}={simd}" for feature in ("SSE42", "AVX", "AVX2", "FMA", "F16C", "BMI2")),
        "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",
        *deployment,
    )
    run("cmake", "--build", str(build), "--target", "llama-server", "--config", "Release", "--parallel", "2")
    binary = exec_name("llama-server")
    return next(path for path in (build / "bin").rglob(binary) if path.is_file())


def main():
    if (platform.system(), platform.machine()) not in NATIVE:
        raise SystemExit("Use a supported native Linux x64, macOS x64/arm64, or Windows x64 runner.")
    if STAGE.exists():
        raise SystemExit(f"Remove or move the previous desktop build stage before staging again: {STAGE}")
    if not (ROOT / "node_modules").is_dir():
        raise SystemExit("Run npm ci --ignore-scripts --omit=dev in the repository first.")
    for path in (STAGE, STAGE / "licenses"):
        path.mkdir()
    shutil.copyfile(ROOT / "LICENSE", STAGE / "licenses/Penstock-Community-LICENSE.txt")
    with tempfile.TemporaryDirectory(prefix="penstock-package-") as temporary:
        scratch = Path(temporary)
        freeze_backend(scratch)
        fetch_node(scratch)
        build_inference(scratch)


if __name__ == "__main__":
    main()
