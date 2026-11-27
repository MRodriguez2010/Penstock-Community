"use strict";

const get = (id) => document.getElementById(id);
let view;
let busy = true;
let announcedTransferPercent = 0;

const invoke = (name, args = {}) => window.__TAURI__.core.invoke(name, args);
const size = (bytes) => (bytes / 1e9).toFixed(2) + " GB";
const savedSize = (bytes) => bytes < 1e9 ? (bytes / 1e6).toFixed(2) + " MB" : size(bytes);
const offline = () => ({ kind: "offline" });
const selectedPack = (id) => ({ kind: "pack", id });
const isSelected = (model) =>
  view.settings.model.kind === model.kind && (model.kind !== "pack" || view.settings.model.id === model.id);

function announce(message = "", error = false) {
  get("status").classList.toggle("error", error);
  get("status").textContent = message;
}

function button(label, action, disabled = false) {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = label;
  control.disabled = disabled;
  control.addEventListener("click", action);
  return control;
}

function render() {
  if (!view) return;
  const { model, workspace } = view.settings;
  const selected = model.kind === "pack" ? view.packs.find((item) => item.id === model.id) : null;
  get("selection").textContent = model.kind === "endpoint"
    ? "Endpoint · " + model.model
    : selected ? selected.name + " · on device" : "Offline starter";
  get("workspace-path").textContent = workspace;
  get("publisher-key-folder").textContent = view.publisherKeyFolder;
  get("reset").hidden = workspace === view.defaultWorkspace;
  for (const id of ["open", "choose", "reset", "save-endpoint", "test-endpoint"]) {
    get(id).disabled = busy;
  }
  get("use-offline").disabled = busy || isSelected(offline());

  const list = get("packs");
  list.replaceChildren();
  for (const pack of view.packs) {
    const row = document.createElement("article");
    row.className = "pack";
    const description = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = pack.name;
    const detail = document.createElement("p");
    detail.className = "metadata";
    detail.textContent = "Q4_K_M · " + size(pack.size) + " · " + pack.memory + " GB available RAM recommended";
    const availability = document.createElement("p");
    availability.className = "metadata";
    availability.textContent = pack.present ? "File present · verified when started"
      : "Download from Hugging Face or import a signed pack or matching GGUF.";
    if (pack.pendingBytes) {
      availability.textContent += " · " + savedSize(pack.pendingBytes) + " saved for retry";
    }
    description.append(title, detail, availability);
    const actions = document.createElement("div");
    actions.className = "controls";
    if (pack.present) {
      actions.append(
        button(isSelected(selectedPack(pack.id)) ? "Selected" : "Use pack",
          () => saveModel(selectedPack(pack.id)), busy || isSelected(selectedPack(pack.id))),
        button("Test pack", () => test(selectedPack(pack.id)), busy),
        button(pack.pendingBytes ? "Resume repair" : "Repair download", () => acquire(pack, false), busy),
        button("Import offline model", () => acquire(pack, true), busy)
      );
    } else {
      actions.append(
        button((pack.pendingBytes ? "Resume " : "Download ") + pack.name, () => acquire(pack, false), busy),
        button("Import offline model", () => acquire(pack, true), busy)
      );
    }
    if (pack.pendingBytes) {
      actions.append(button("Discard partial download", () => discardDownload(pack), busy));
    }
    row.append(description, actions);
    list.append(row);
  }
}

function restoreFocus(previous) {
  if (previous?.isConnected && !previous.disabled) {
    previous.focus();
  } else if (get("model-details").open) {
    get("model-details").querySelector("summary").focus();
  }
}

async function run(message, action) {
  if (busy) return;
  const previous = document.activeElement;
  busy = true;
  render();
  announce(message);
  try {
    await action();
  } catch (error) {
    announce(String(error), true);
  } finally {
    busy = false;
    render();
    restoreFocus(previous);
  }
}

async function acquire(pack, importFile) {
  await run(importFile ? "Choose the signed pack or matching GGUF file." : "Starting verified download…", async () => {
    const bar = get("transfer");
    bar.value = 0;
    announcedTransferPercent = 0;
    get("transfer-controls").hidden = false;
    get("cancel-transfer").disabled = false;
    try {
      const result = await invoke("acquire_model", { id: pack.id, offline: importFile });
      if (result.cancelled) {
        announce("Import cancelled.");
        return;
      }
      view = await invoke("desktop_state");
      announce(result.verification?.mode === "signed"
        ? pack.name + " passed publisher signature and SHA-256 verification. Choose Use pack to select it."
        : pack.name + " passed SHA-256 verification. Choose Use pack to select it.");
    } catch (error) {
      await invoke("desktop_state").then((state) => { view = state; }).catch(() => {});
      throw error;
    } finally {
      get("transfer-controls").hidden = true;
    }
  });
}

async function discardDownload(pack) {
  await run("Removing the partial download…", async () => {
    await invoke("discard_download", { id: pack.id });
    view = await invoke("desktop_state");
    announce("Partial download removed.");
  });
}

function remoteConsent() {
  return get("remote-consent").checked;
}

function endpoint() {
  return {
    kind: "endpoint",
    url: get("url").value.trim(),
    protocol: get("protocol").value,
    model: get("model-id").value.trim()
  };
}

async function saveModel(model) {
  await run("Saving model selection…", async () => {
    view.settings = await invoke("select_model", {
      model,
      remoteConsent: remoteConsent(),
      credential: model.kind === "endpoint" ? get("endpoint-token").value : null
    });
    get("endpoint-token").value = "";
    announce(model.kind === "offline" ? "Offline starter selected." : "The selected model will be available when Studio opens.");
  });
}

async function test(model) {
  await run(model.kind === "pack" ? "Verifying the file and starting the local runtime…" : "Sending a short test prompt…", async () => {
    await invoke("test_model", {
      model,
      remoteConsent: remoteConsent(),
      credential: model.kind === "endpoint" ? get("endpoint-token").value : null
    });
    announce("The model responded to the test prompt.");
  });
}

async function chooseWorkspace(reset) {
  await run("Reading workspace folder…", async () => {
    const previous = view.settings.workspace;
    view.settings = await invoke("select_workspace", { reset });
    if (view.settings.workspace !== previous) {
      get("endpoint-form").reset();
      announce("Workspace selected. You can open it offline or choose a model for this workspace.");
    } else {
      announce();
    }
  });
}

async function start() {
  try {
    await window.__TAURI__.event.listen("model-progress", ({ payload }) => {
      const bar = get("transfer");
      bar.value = 100 * payload.progress / payload.total;
      if (bar.value - announcedTransferPercent >= 5) {
        announcedTransferPercent = bar.value;
        announce("Read " + size(payload.progress) + " of " + size(payload.total) + ".");
      }
    });
    view = await invoke("desktop_state");
    if (view.settings.model.kind === "endpoint") {
      const model = view.settings.model;
      get("url").value = model.url;
      get("protocol").value = model.protocol;
      get("model-id").value = model.model;
      if (model.requires_key) {
        get("model-details").open = true;
        announce("Enter the saved endpoint’s API token and choose Use endpoint before opening Studio.");
      }
    }
    busy = false;
    render();
  } catch (error) {
    announce(String(error), true);
  }
}

get("choose").addEventListener("click", () => chooseWorkspace(false));
get("reset").addEventListener("click", () => chooseWorkspace(true));
get("open").addEventListener("click", () => run("Starting Studio…", async () => {
  await invoke("open_studio");
  announce();
}));
get("use-offline").addEventListener("click", () => saveModel(offline()));
get("cancel-transfer").addEventListener("click", async () => {
  get("cancel-transfer").disabled = true;
  announce("Cancelling transfer…");
  try {
    await invoke("cancel_transfer");
  } catch (error) {
    announce(String(error), true);
  }
});
get("endpoint-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveModel(endpoint());
});
get("test-endpoint").addEventListener("click", () => {
  if (get("endpoint-form").reportValidity()) test(endpoint());
});
start();
