#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod process;
mod settings;

use process::ManagedChild;
use serde_json::{json, Value};
use settings::{Model, Settings};
use std::{
    env,
    path::PathBuf,
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder},
    AppHandle, Emitter, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};
use tauri_plugin_dialog::DialogExt;

const LAUNCHER: &str = "launcher";
const STUDIO: &str = "studio";
const MODEL_KEY: &str = "PENSTOCK_DESKTOP_MODEL_KEY";
const ENDPOINT_KEY: &str = "PENSTOCK_DESKTOP_ENDPOINT_KEY";
const BOOTSTRAP_KEY: &str = "PENSTOCK_DESKTOP_BOOTSTRAP";
const PROVIDERS_KEY: &str = "PENSTOCK_DESKTOP_PROVIDERS";
const DOWNLOADS_KEY: &str = "PENSTOCK_DESKTOP_DOWNLOADS";

#[derive(Default)]
struct Desktop {
    operation: Mutex<()>,
    session: Mutex<Option<Session>>,
    endpoint_key: Mutex<Option<String>>,
    quitting: AtomicBool,
    transfer_cancel: AtomicBool,
}

struct Session {
    backend: ManagedChild,
    model: Option<ManagedChild>,
}

impl Drop for Session {
    fn drop(&mut self) {
        // The workspace stops first so a run cannot start against a closing model.
        self.backend.terminate();
        if let Some(model) = &mut self.model {
            model.terminate();
        }
    }
}

fn require_launcher(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == LAUNCHER {
        Ok(())
    } else {
        Err("Desktop setup commands are available only in the local launcher.".into())
    }
}

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path().app_local_data_dir().map_err(|err| format!("Desktop data folder unavailable: {err}"))
}

fn resource(app: &AppHandle, folder: &str, executable: &str) -> Result<PathBuf, String> {
    let name = if cfg!(windows) { format!("{executable}.exe") } else { executable.into() };
    let path = app.path().resource_dir()
        .map_err(|err| format!("Install resources are unavailable: {err}"))?.join(folder).join(name);
    if path.is_file() {
        Ok(path)
    } else {
        Err(format!("The installer is missing its bundled {executable} component. Reinstall Penstock Community."))
    }
}

fn command(app: &AppHandle) -> Result<Command, String> {
    let mut command = Command::new(resource(app, "backend", "penstock-backend")?);
    let node = resource(app, "node", "node")?;
    let path = env::join_paths(std::iter::once(node.parent().expect("node is in a folder").to_path_buf())
        .chain(env::split_paths(&env::var_os("PATH").unwrap_or_default())))
        .map_err(|_| "Could not configure the bundled Node.js path.")?;
    command.env("PATH", path).env("PYTHONUNBUFFERED", "1")
        .env("NoDefaultCurrentDirectoryInExePath", "1")
        .env_remove(BOOTSTRAP_KEY).env_remove(PROVIDERS_KEY).env_remove(DOWNLOADS_KEY)
        .env_remove(MODEL_KEY).env_remove(ENDPOINT_KEY);
    Ok(command)
}

fn model_command(app: &AppHandle, action: &str) -> Result<Command, String> {
    let mut cmd = command(app)?;
    cmd.arg("desktop-models").arg(action).arg("--cache").arg(data_dir(app)?.join("models"));
    Ok(cmd)
}

fn secret() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "The system could not create a session key.")?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn pack(id: &str) -> Result<u16, String> {
    match id {
        "smollm3-3b" => Ok(45371),
        "olmo-3-7b-think" => Ok(45372),
        _ => Err("Choose a model pack from the list.".into()),
    }
}

fn model_reply(
    mut child: ManagedChild, wait: Duration, cancel: &AtomicBool, progress: impl Fn(&Value),
) -> Result<Value, String> {
    let value = child.wait_for(wait, cancel, |line| {
        let Ok(value) = serde_json::from_str::<Value>(line) else { return Ok(None) };
        if let Some(error) = value.get("error").and_then(Value::as_str) {
            return Err(error.into());
        }
        if value.get("progress").is_some() {
            progress(&value);
            return Ok(None);
        }
        Ok(Some(value))
    })?;
    // Once the helper reports success, a concurrent cancel must not mask a committed import.
    child.wait_success(Duration::from_secs(10), &AtomicBool::new(false))?;
    Ok(value)
}

fn call_model(
    app: &AppHandle, action: &str, input: Option<&Value>, key: Option<&str>, cancel: &AtomicBool,
) -> Result<Value, String> {
    let mut command = model_command(app, action)?;
    if let Some(key) = key {
        command.env(MODEL_KEY, key);
    }
    let mut child = ManagedChild::spawn(command)?;
    if let Some(input) = input {
        child.send_input(&serde_json::to_vec(input).map_err(|_| "Invalid endpoint profile.")?)?;
    }
    model_reply(child, Duration::from_secs(60), cancel, |_| {})
}

#[cfg(target_arch = "x86_64")]
fn supports_avx2_runner() -> bool {
    std::is_x86_feature_detected!("sse4.2")
        && std::is_x86_feature_detected!("avx")
        && std::is_x86_feature_detected!("avx2")
        && std::is_x86_feature_detected!("fma")
        && std::is_x86_feature_detected!("f16c")
        && std::is_x86_feature_detected!("bmi1")
        && std::is_x86_feature_detected!("bmi2")
        && std::is_x86_feature_detected!("lzcnt")
}

#[cfg(not(target_arch = "x86_64"))]
fn supports_avx2_runner() -> bool {
    false
}

fn start_model(
    app: &AppHandle, id: &str, cancel: &AtomicBool,
) -> Result<(ManagedChild, Value, String), String> {
    let port = pack(id)?;
    let key = secret()?;
    let mut command = model_command(app, "serve")?;
    command.arg("--pack").arg(id).arg("--llama").arg(resource(app, "inference", "llama-server")?)
        .env(MODEL_KEY, &key);
    if supports_avx2_runner() {
        if let Ok(optimized) = resource(app, "inference", "llama-server-avx2") {
            command.arg("--llama-avx2").arg(optimized);
        }
    }
    let mut child = ManagedChild::spawn(command)?;
    child.wait_for(Duration::from_secs(150), cancel, |line| {
        let Ok(value) = serde_json::from_str::<Value>(line) else { return Ok(None) };
        if let Some(error) = value.get("error").and_then(Value::as_str) {
            return Err(error.into());
        }
        Ok((value.get("ready").and_then(Value::as_bool) == Some(true)).then_some(()))
    })?;
    let endpoint = json!({
        "protocol": "chat-completions", "model": id,
        "url": format!("http://127.0.0.1:{port}/v1/chat/completions"),
    });
    Ok((child, endpoint, key))
}

fn profile(model: &Model) -> Option<Value> {
    match model {
        Model::Endpoint { url, protocol, model, .. } => Some(json!({"url": url, "protocol": protocol, "model": model})),
        _ => None,
    }
}

fn check_credential(credential: Option<String>) -> Result<Option<String>, String> {
    let credential = credential.filter(|value| !value.is_empty());
    if credential.as_ref().is_some_and(|value|
        value.len() > 4096 || !value.bytes().all(|byte| byte.is_ascii_graphic())
    ) {
        return Err("Enter an API token without spaces or line breaks.".into());
    }
    Ok(credential)
}

fn require_consent(value: &Value, remote_consent: bool) -> Result<(), String> {
    let url = value.get("url").and_then(Value::as_str).ok_or("Enter a complete endpoint URL.")?;
    let url = Url::parse(url).map_err(|_| "Enter a complete endpoint URL.")?;
    let is_loopback = matches!(url.host_str(), Some("127.0.0.1" | "[::1]" | "::1"));
    if is_loopback || remote_consent {
        Ok(())
    } else {
        Err("Confirm that selected requests can leave this device before using a remote endpoint.".into())
    }
}

fn no_session(desktop: &Desktop) -> Result<(), String> {
    if desktop.session.lock().map_err(|_| "Desktop session state is unavailable.")?.is_some() {
        Err("Close the Studio before changing desktop settings.".into())
    } else {
        Ok(())
    }
}

async fn background<T: Send + 'static>(
    window: WebviewWindow, work: impl FnOnce(&AppHandle, &Desktop) -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    require_launcher(&window)?;
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Desktop>();
        let _lock = state.operation.try_lock().map_err(|_| "Another desktop setup operation is in progress.")?;
        work(&app, &state)
    })
    .await.map_err(|err| format!("Desktop setup did not finish: {err}"))?
}

#[tauri::command]
async fn desktop_state(window: WebviewWindow) -> Result<Value, String> {
    background(window, |app, desktop| {
        let data = data_dir(app)?;
        let settings = Settings::load(&data)?;
        let packs = call_model(app, "status", None, None, &desktop.quitting)?;
        Ok(json!({"settings": settings, "packs": packs["packs"], "publisherKeyFolder": packs["publisherKeyFolder"],
                  "defaultWorkspace": Settings::defaults(&data).workspace}))
    }).await
}

#[tauri::command]
async fn select_workspace(window: WebviewWindow, reset: bool) -> Result<Settings, String> {
    background(window, move |app, desktop| {
        no_session(desktop)?;
        let data = data_dir(app)?;
        let settings = Settings::load(&data)?;
        let path = if reset {
            Settings::defaults(&data).workspace
        } else {
            let Some(selected) = app.dialog().file().set_title("Choose an existing Penstock workspace or an empty folder")
                .blocking_pick_folder() else { return Ok(settings) };
            settings::existing_or_empty(&selected.into_path().map_err(|_| "Choose a local folder.")?)?
        };
        if path == settings.workspace {
            return Ok(settings);
        }
        let selection = Settings { workspace: path, model: Model::Offline };
        selection.save(&data)?;
        *desktop.endpoint_key.lock().map_err(|_| "Endpoint session state is unavailable.")? = None;
        Ok(selection)
    }).await
}

#[tauri::command]
async fn select_model(
    window: WebviewWindow, mut model: Model, remote_consent: bool, credential: Option<String>,
) -> Result<Settings, String> {
    background(window, move |app, desktop| {
        no_session(desktop)?;
        let data = data_dir(app)?;
        let mut settings = Settings::load(&data)?;
        let mut key = None;
        if let Some(profile) = profile(&model) {
            require_consent(&profile, remote_consent)?;
            call_model(app, "validate-endpoint", Some(&profile), None, &desktop.quitting)?;
            key = check_credential(credential)?;
            if let Model::Endpoint { requires_key, .. } = &mut model {
                *requires_key = key.is_some();
            }
        }
        if let Model::Pack { id } = &model {
            pack(id)?;
            let status = call_model(app, "status", None, None, &desktop.quitting)?;
            if !status["packs"].as_array().is_some_and(|packs| packs.iter().any(
                |pack| pack["id"] == id.as_str() && pack["present"] == true
            )) {
                return Err("Install or import this model pack before selecting it.".into());
            }
        }
        settings.model = model;
        settings.save(&data)?;
        *desktop.endpoint_key.lock().map_err(|_| "Endpoint session state is unavailable.")? = key;
        Ok(settings)
    }).await
}

#[tauri::command]
async fn acquire_model(window: WebviewWindow, id: String, offline: bool) -> Result<Value, String> {
    let progress_window = window.clone();
    background(window, move |app, desktop| {
        no_session(desktop)?;
        desktop.transfer_cancel.store(false, Ordering::Release);
        pack(&id)?;
        let mut command = model_command(app, "install")?;
        command.arg("--pack").arg(&id);
        if offline {
            let Some(file) = app.dialog().file().set_title("Choose a pinned GGUF or signed offline model pack")
                .add_filter("Penstock model files", &["gguf", "penstock-model"]).blocking_pick_file() else {
                return Ok(json!({"cancelled": true}));
            };
            command.arg("--offline").arg(file.into_path().map_err(|_| "Choose a local model file.")?);
        }
        let child = ManagedChild::spawn(command)?;
        model_reply(child, Duration::from_secs(60 * 60 * 3), &desktop.transfer_cancel, |value| {
            let _ = progress_window.emit("model-progress", value);
        })
    }).await
}

#[tauri::command]
async fn discard_download(window: WebviewWindow, id: String) -> Result<Value, String> {
    background(window, move |app, desktop| {
        no_session(desktop)?;
        pack(&id)?;
        let mut command = model_command(app, "discard-download")?;
        command.arg("--pack").arg(&id);
        model_reply(
            ManagedChild::spawn(command)?,
            Duration::from_secs(10),
            &desktop.quitting,
            |_| {},
        )
    }).await
}

#[tauri::command]
fn cancel_transfer(window: WebviewWindow) -> Result<(), String> {
    require_launcher(&window)?;
    window.state::<Desktop>().transfer_cancel.store(true, Ordering::Release);
    Ok(())
}

#[tauri::command]
async fn test_model(
    window: WebviewWindow, model: Model, remote_consent: bool, credential: Option<String>,
) -> Result<Value, String> {
    background(window, move |app, desktop| {
        no_session(desktop)?;
        let cancel = &desktop.quitting;
        match &model {
            Model::Pack { id } => {
                let (_runtime, _endpoint, key) = start_model(app, id, cancel)?;
                let mut command = model_command(app, "test-pack")?;
                command.arg("--pack").arg(id).env(MODEL_KEY, key);
                model_reply(ManagedChild::spawn(command)?, Duration::from_secs(100), cancel, |_| {})
            }
            Model::Endpoint { .. } => {
                let endpoint = profile(&model).expect("endpoint profile");
                require_consent(&endpoint, remote_consent)?;
                let key = check_credential(credential)?;
                call_model(app, "test-endpoint", Some(&endpoint), key.as_deref(), cancel)
            }
            Model::Offline => Err("Choose a model to test.".into()),
        }
    }).await
}

fn launch(app: &AppHandle, desktop: &Desktop) -> Result<(), String> {
    if let Some(studio) = app.get_webview_window(STUDIO) {
        return studio.set_focus().map_err(|err| err.to_string());
    }
    let data = data_dir(app)?;
    let settings = Settings::load(&data)?;
    let cancel = &desktop.quitting;

    if settings.workspace != Settings::defaults(&data).workspace {
        settings::existing_or_empty(&settings.workspace)?;
    }
    if !settings.workspace.join("owner.key").is_file() || !settings.workspace.join("connections.toml").is_file() {
        let mut init = command(app)?;
        init.arg("init").arg("--workspace").arg(&settings.workspace);
        ManagedChild::spawn(init)?.wait_success(Duration::from_secs(15), cancel)?;
    }

    let mut model = None;
    let mut providers = None;
    let mut credential = None;
    match &settings.model {
        Model::Offline => {}
        Model::Pack { id } => {
            let (child, mut endpoint, key) = start_model(app, id, cancel)?;
            endpoint["key_env"] = json!(MODEL_KEY);
            providers = Some(json!({ (format!("desktop-{id}")): endpoint }));
            model = Some(child);
            credential = Some((MODEL_KEY, key));
        }
        Model::Endpoint { requires_key, .. } => {
            let endpoint = profile(&settings.model).expect("endpoint profile");
            let mut endpoint = call_model(app, "validate-endpoint", Some(&endpoint), None, cancel)?;
            if *requires_key {
                let key = desktop.endpoint_key.lock().map_err(|_| "Endpoint session state is unavailable.")?
                    .clone().ok_or("Enter your endpoint API token and choose Use endpoint before opening Studio.")?;
                endpoint["key_env"] = json!(ENDPOINT_KEY);
                credential = Some((ENDPOINT_KEY, key));
            }
            providers = Some(json!({"desktop-endpoint": endpoint}));
        }
    }

    let token = secret()?;
    let mut backend = command(app)?;
    backend.arg("serve").arg("--workspace").arg(&settings.workspace)
        .args(["--bind", "127.0.0.1", "--port", "0"]).env(BOOTSTRAP_KEY, &token);
    let paths = app.path();
    if let Ok(folder) = paths.download_dir().or_else(|_| paths.home_dir().map(|home| home.join("Downloads"))) {
        if folder.is_absolute() {
            backend.env(DOWNLOADS_KEY, folder);
        }
    }
    if let Some(providers) = providers {
        backend.env(PROVIDERS_KEY, providers.to_string());
    }
    if let Some((name, key)) = credential {
        backend.env(name, key);
    }
    let mut backend = ManagedChild::spawn(backend)?;
    let port = backend.wait_for(Duration::from_secs(25), cancel, |line| {
        Ok(line.strip_prefix("Penstock listening on 127.0.0.1:")
            .and_then(|value| value.parse::<u16>().ok()).filter(|&value| value != 0))
    }).map_err(|err| format!("{err} Check that another Penstock server is not using this workspace."))?;
    let url = Url::parse(&format!("http://127.0.0.1:{port}/desktop/bootstrap/{token}"))
        .map_err(|_| "Could not create the local Studio connection.")?;

    let window = match WebviewWindowBuilder::new(app, STUDIO, WebviewUrl::External(url))
        .title("Penstock Community · Studio").inner_size(1280.0, 840.0).min_inner_size(440.0, 540.0)
        .visible(false)
        .on_navigation(move |url| {
            url.scheme() == "http" && url.host_str() == Some("127.0.0.1")
                && url.port() == Some(port) && url.username().is_empty() && url.password().is_none()
        })
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .build()
    {
        Ok(window) => window,
        Err(err) => return Err(format!("Could not open the Studio window: {err}")),
    };
    let mut session = desktop.session.lock().map_err(|_| "Desktop session state is unavailable.")?;
    *session = Some(Session { backend, model });
    if let Some(launcher) = app.get_webview_window(LAUNCHER) {
        let _ = launcher.hide();
    }
    let _ = window.show();
    let _ = window.set_focus();
    Ok(())
}

#[tauri::command]
async fn open_studio(window: WebviewWindow) -> Result<(), String> {
    background(window, launch).await
}

fn show_launcher(app: &AppHandle) {
    if let Some(launcher) = app.get_webview_window(LAUNCHER) {
        let _ = launcher.show();
        let _ = launcher.set_focus();
    }
}

fn stop_studio(app: &AppHandle, reopen_launcher: bool) {
    let state = app.state::<Desktop>();
    let session = state.session.lock().ok().and_then(|mut session| session.take());
    if reopen_launcher && !state.quitting.load(Ordering::Acquire) {
        let app = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            drop(session);
            show_launcher(&app);
        });
    } else {
        drop(session);
    }
}

fn main() {
    tauri::Builder::default()
        .manage(Desktop::default())
        .menu(|app| {
            let submenu = SubmenuBuilder::new(app, "Penstock")
                .item(&MenuItemBuilder::with_id("desktop-settings", "Close Studio and return to setup").build(app)?)
                .item(&MenuItemBuilder::with_id("desktop-quit", "Quit Penstock").build(app)?)
                .build()?;
            MenuBuilder::new(app).item(&submenu).build()
        })
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(studio) = app.get_webview_window(STUDIO) {
                let _ = studio.set_focus();
            } else {
                show_launcher(app);
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            desktop_state, select_workspace, select_model, acquire_model, discard_download, cancel_transfer, test_model, open_studio
        ])
        .on_menu_event(|app, event| match event.id().as_ref() {
            "desktop-settings" => {
                if let Some(studio) = app.get_webview_window(STUDIO) {
                    let _ = studio.close();
                }
            }
            "desktop-quit" => app.exit(0),
            _ => {}
        })
        .on_window_event(|window, event| {
            if window.label() == STUDIO && matches!(event, WindowEvent::Destroyed) {
                stop_studio(window.app_handle(), true);
            }
            if window.label() == LAUNCHER && matches!(event, WindowEvent::Destroyed) {
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("could not start Penstock Community Desktop")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit) {
                let state = app.state::<Desktop>();
                state.quitting.store(true, Ordering::Release);
                state.transfer_cancel.store(true, Ordering::Release);
                stop_studio(app, false);
            }
        });
}
