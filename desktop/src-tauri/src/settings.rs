use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Settings {
    pub workspace: PathBuf,
    pub model: Model,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum Model {
    Offline,
    Pack { id: String },
    Endpoint {
        url: String,
        protocol: String,
        model: String,
        #[serde(default)]
        requires_key: bool,
    },
}

impl Settings {
    pub fn defaults(data: &Path) -> Self {
        Self { workspace: data.join("workspace"), model: Model::Offline }
    }

    pub fn load(data: &Path) -> Result<Self, String> {
        match fs::read_to_string(data.join("desktop-settings.json")) {
            Ok(raw) => serde_json::from_str(&raw)
                .map_err(|_| "Desktop settings could not be read. Your workspace files are unchanged.".into()),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(Self::defaults(data)),
            Err(err) => Err(format!("Could not read desktop settings: {err}")),
        }
    }

    pub fn save(&self, data: &Path) -> Result<(), String> {
        fn save_inner(settings: &Settings, data: &Path) -> io::Result<()> {
            fs::create_dir_all(data)?;
            let mut file = NamedTempFile::new_in(data)?;
            serde_json::to_writer_pretty(file.as_file_mut(), settings)?;
            file.as_file_mut().write_all(b"\n")?;
            file.as_file().sync_all()?;
            file.persist(data.join("desktop-settings.json"))?;
            Ok(())
        }
        save_inner(self, data).map_err(|err| format!("Could not save desktop settings: {err}"))
    }
}

pub fn existing_or_empty(path: &Path) -> Result<PathBuf, String> {
    let path = path.canonicalize().map_err(|err| format!("Could not open the workspace folder: {err}"))?;
    let mut contents = fs::read_dir(&path).map_err(|err| format!("Could not read the workspace folder: {err}"))?;
    let is_existing = path.join("owner.key").is_file() && path.join("connections.toml").is_file();
    if is_existing || contents.next().is_none() {
        return Ok(path);
    }
    Err("Choose an empty folder or an existing Penstock workspace containing connections.toml and owner.key.".into())
}
