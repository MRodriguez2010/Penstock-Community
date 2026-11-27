# Penstock Community desktop

The desktop installer includes the workspace service, the Node.js worker runtime, and a CPU inference runner. Open Penstock Community and choose **Open Studio** to try the example workflow offline. Model setup is optional.

| System | Installer | Architecture |
| --- | --- | --- |
| Windows | NSIS setup executable with the WebView2 offline installer | x64 |
| Linux | Debian package or AppImage | x64 |
| macOS | Disk image | Apple Silicon or Intel |

Use the installer for your operating system and architecture. The desktop build workflow produces a SHA-256 list with the installer artifacts. OS installation prompts and package prerequisites apply. The AppImage may require FUSE support.

On Debian-compatible x64 desktops, open the downloaded `.deb` in the system software installer, choose **Install**, then open **Penstock Community** from Applications. It requires the GTK 3 and WebKitGTK 4.1 runtime packages (`libgtk-3-0` and `libwebkit2gtk-4.1-0`). Install these system packages before disconnecting a fresh machine. With them in place, both starter jobs run offline without a model download, API key, terminal or developer tools.

## Open or move a workspace

The launcher shows the selected workspace folder. The default is under the operating system’s Penstock Community app data folder. **Choose folder** accepts an empty folder for a fresh workspace or an existing Penstock workspace containing `connections.toml` and `owner.key`. Selecting an existing workspace keeps its directory, project data, and history in place. Close any other Penstock server that is using that same workspace before opening it here.

Studio starts a loopback-only service and signs in automatically inside its own window. Closing Studio returns to setup and stops the workspace service and any on-device model it started. Existing enabled schedules run while Studio is open. Quit Penstock Community to leave the app closed. To back up or move a workspace, close Studio, copy the workspace folder, and choose the copy from the launcher.

## Add a model

| Pack | GGUF download | Recommended available RAM | Context |
| --- | ---: | ---: | ---: |
| SmolLM3-3B | 1.92 GB | 6 GB | 4,096 tokens |
| Olmo 3 7B Think | 4.47 GB | 12 GB | 8,192 tokens |

1. Open **Model setup**. Choose a named download to contact the pinned model host, or **Import offline model** to verify a signed `.penstock-model` pack or the matching raw `.gguf` without network access. A fresh installed copy needs the model size plus 512 MiB free in the desktop data location.
2. Choose **Test pack** to verify the cached file, start the local runtime, and send a short test prompt. Choose **Use pack** to select it for the workspace. The active pack receives requests only when a configured workflow action uses it.
3. Open Studio. The selected model appears as `desktop-smollm3-3b` or `desktop-olmo-3-7b-think`. One model pack is active at a time. To use a different pack, close Studio, select it, and reopen. Versions that reference a particular provider need that provider to be available when run.

Penstock checks the exact byte length, SHA-256, and GGUF header of the immutable Q4_K_M file on download and import. A signed pack also requires a trusted Ed25519 publisher. Penstock checks the cached file again before starting inference. If a cached file fails verification, use **Repair download** or import it again.

**Resume** rechecks a saved download prefix and asks the model host for the remaining bytes. Resuming needs the remaining size plus 512 MiB free. If the host sends the full file, the launcher starts again and checks available space for the full model. **Discard partial download** frees the saved space. A transfer without a strong host validator or an interrupted offline import starts at the first byte on retry.

The launcher uses an AVX2 runtime on supported x64 processors and includes a baseline CPU runtime for other machines. It allows about two minutes for the runtime to start after model verification. **Test pack** allows up to 90 seconds for a short answer. Studio allows up to five minutes for an inference response from a bundled pack. The CPU runtime uses one request slot with a 1,024-token generation default and a 512-token reasoning budget. Inference speed and native hardware qualification vary by platform and machine. The managed ports are 45371 and 45372 on numeric loopback.

| Pack | Exact file | SHA-256 |
| --- | --- | --- |
| SmolLM3-3B | [Pinned GGUF file](https://huggingface.co/ggml-org/SmolLM3-3B-GGUF/blob/4965cb60b150737b68a0408c36aeefb65078f894/SmolLM3-Q4_K_M.gguf) | `8334b850b7bd46238c16b0c550df2138f0889bf433809008cc17a8b05761863e` |
| Olmo 3 7B Think | [Pinned GGUF file](https://huggingface.co/bartowski/allenai_Olmo-3-7B-Think-GGUF/blob/d5a1bf052bdb80f09cd79eee4d2037c3f8ced190/allenai_Olmo-3-7B-Think-Q4_K_M.gguf) | `4a4a31e4dd68e6ad4fdd876605265fadabd72d6b1aeefbe04a01531dbc176126` |

The original models, [SmolLM3-3B by Hugging Face](https://huggingface.co/HuggingFaceTB/SmolLM3-3B) and [Olmo 3 7B Think by AI2](https://huggingface.co/allenai/Olmo-3-7B-Think), are distributed under Apache 2.0. The GGUF repositories above identify the quantizations.

### Local CPU qualification

Both pinned packs generated a three-step approval workflow that passed Penstock’s real compiler. One qualification run used Linux under WSL2 on a Ryzen 7 5800X, with six logical CPUs, a 12 GiB memory ceiling and the packaged AVX2 inference runner. The task asked for a start, a human approval and a finish. Times below are single observations, in seconds, after downloading the weights.

| Configuration | Verify and start | Connection test | Valid workflow draft |
| --- | ---: | ---: | ---: |
| Managed SmolLM3-3B | 5.05 | 2.49 | 39.07 |
| Managed Olmo 3 7B Think | 13.82 | 8.28 | 83.23 |
| Independently configured local chat-completions endpoint using SmolLM3 | 3.63 | 2.90 | 38.28 |

Larger jobs, CPU instruction support, available memory and model choice affect latency. The app validates each draft before a person decides whether to save, test or publish it.

### Distribute signed model packs offline

Use [the model pack tool](../desktop/model_packs.py) on an IT workstation with Python and `cryptography` installed. The tool accepts Ed25519 PEM keys, including passphrase-protected private keys. Keep the private key on the signing workstation. Obtain and confirm the public fingerprint through your organization’s trusted channel before provisioning it.

```sh
python desktop/model_packs.py fingerprint --public-key publisher-public.pem
python desktop/model_packs.py export --model smollm3-3b --gguf SmolLM3-Q4_K_M.gguf --private-key publisher-private.pem --output smollm3.penstock-model
```

On a device that will accept the package, open **IT publisher keys and transfer details** under **Model setup** to find its trust folder. Pass that folder’s parent as `--app-data`. Replace `REVIEWED_64_HEX_FINGERPRINT` with the reviewed result from the fingerprint command.

```sh
python desktop/model_packs.py trust --app-data "/path/to/penstock-data" --public-key publisher-public.pem --fingerprint REVIEWED_64_HEX_FINGERPRINT
python desktop/model_packs.py verify --app-data "/path/to/penstock-data" --model smollm3-3b --pack smollm3.penstock-model
```

The `trust` command installs a file named with the lowercase fingerprint and `.pem` under `trusted-model-publishers`. A managed installation can provision that same reviewed public key file directly under the user’s trust folder. Removing the key blocks future imports signed solely by that publisher. Existing installed files still use the built-in model checksum.

The manifest signs the exact model ID, filename, source revision, SHA-256, byte count, and Apache-2.0 license identifier. Export and import stream the GGUF data, and the signed wrapper is smaller than 5 KiB. Distribute the applicable upstream license and notices with the pack. Allow room for both the source and export when they share a disk. Use a transport filesystem that permits files larger than 4 GiB for Olmo. The launcher accepts a signed pack through **Import offline model** and rejects an unknown signer, invalid signature, or mismatched payload even when the model is already installed.

### Bring your own endpoint

Enter the full Ollama chat, Chat Completions, or Anthropic Messages request URL and the exact model ID. Use HTTPS for a remote URL. Numeric loopback HTTP is accepted for local services such as `http://127.0.0.1:11434/api/chat`. **Send test request** explicitly sends the fixed prompt “Reply with the word OK.” **Use endpoint** saves the selected address and model as `desktop-endpoint`. Enter the optional API token again after quitting and reopening Penstock. The desktop launcher keeps tokens in process memory during use and leaves existing workspace connections in place.

A selected remote endpoint can receive workflow data when an Agent or drafting action uses it. A workflow or enabled schedule that already references a configured provider follows that workspace’s existing connection settings.

## Build and qualify installers

Run the [desktop build workflow](../.github/workflows/desktop-build.yml) on supported native Windows, Linux, and macOS runners. It freezes the same workspace service into the app, stages a version- and digest-pinned Node.js binary, builds a pinned llama.cpp revision, and uses the checked-in Rust dependency lockfile. The workflow uploads installer files and checksums as build artifacts. The default build creates preview artifacts. For a signed candidate, a maintainer dispatches the workflow on the default branch with **sign** enabled. It checks Windows Authenticode including the timestamp, macOS Developer ID and notarization for the application and disk image, and a detached GPG signature over the Linux checksums. Downloads remain build artifacts until maintainers qualify installation and publish a release.

| Platform | Repository secrets | Repository variables |
| --- | --- | --- |
| Windows | `WINDOWS_PFX_BASE64`, `WINDOWS_PFX_PASSWORD` for a publisher identity that permits PFX import | `WINDOWS_TIMESTAMP_URL` |
| macOS | `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_ID`, `APPLE_PASSWORD` (app-specific password) | `APPLE_SIGNING_IDENTITY`, `APPLE_TEAM_ID` |
| Linux | `LINUX_SIGNING_KEY` (armored private key), optional `LINUX_SIGNING_PASSPHRASE` | `LINUX_SIGNING_FINGERPRINT` |

Distribute the Linux public key fingerprint through the project's trusted release channel. Match the publisher identity and verify the checksum before installation. [Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/) · [Tauri Windows signing](https://v2.tauri.app/distribute/sign/windows/)

For a native development checkout, install the platform’s [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), Python 3.13, Node.js 22, CMake, Git, and Rust 1.100.0. From the checkout root, build the Linux bundles as follows. Use `nsis` on Windows and `dmg` on macOS.

```sh
python -m pip install --only-binary=:all: -r desktop/build/requirements.txt
npm ci --ignore-scripts --omit=dev
python desktop/build/stage.py
python desktop/build/smoke.py
cd desktop
npx --yes --package @tauri-apps/cli@2.12.6 tauri build --bundles deb,appimage -- --locked
```

The packaged smoke test starts a temporary workspace, checks the one-time loopback session and calendar worker, confirms the service stops on pipe closure, and verifies that reinitialization preserves workspace files. To qualify a platform release, install its artifact, open Studio on a machine without the developer toolchain, run the offline example, reopen an existing workspace, test both model packs, and confirm the app and its child processes exit together. Check the model test separately on the supported hardware class.
