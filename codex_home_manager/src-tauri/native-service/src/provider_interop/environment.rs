use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::{
    adapters::{cc_switch, native_codex, AdapterInspection},
    InteropError,
};

#[derive(Clone, Debug)]
pub struct InspectRequest {
    pub codex_home: PathBuf,
    pub adapter_root: Option<PathBuf>,
}

impl InspectRequest {
    pub fn new(codex_home: impl AsRef<Path>, adapter_root: Option<&Path>) -> Self {
        Self {
            codex_home: codex_home.as_ref().to_path_buf(),
            adapter_root: adapter_root.map(Path::to_path_buf),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolvedEnvironment {
    pub codex_home: String,
    pub config_path: String,
    pub state_database_path: String,
}

pub fn inspect_environment(request: InspectRequest) -> Result<AdapterInspection, InteropError> {
    let codex_home = absolute_existing_directory(&request.codex_home, "codex_home_not_found")?;
    let native = native_codex::inspect(&codex_home)?;
    let adapter = cc_switch::inspect(request.adapter_root.as_deref(), &codex_home)?;
    Ok(AdapterInspection {
        environment: ResolvedEnvironment {
            codex_home: codex_home.to_string_lossy().into_owned(),
            config_path: native.config_path.to_string_lossy().into_owned(),
            state_database_path: native.database_path.to_string_lossy().into_owned(),
        },
        adapter,
        route: native.route,
        authentication: native.authentication,
        history_buckets: native.history_buckets,
    })
}

fn absolute_existing_directory(path: &Path, code: &str) -> Result<PathBuf, InteropError> {
    if !path.is_dir() {
        return Err(InteropError::new(
            code,
            "the selected directory does not exist",
        ));
    }
    path.canonicalize()
        .map_err(|_| InteropError::new(code, "the selected directory cannot be resolved"))
}
