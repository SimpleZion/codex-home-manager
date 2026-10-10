use serde::Serialize;

pub const LOCAL_API_BASE_URL: &str = "http://127.0.0.1:8765";

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopRuntimeStatus {
    pub runtime: &'static str,
    pub service_state: &'static str,
    pub api_base_url: &'static str,
}

impl Default for DesktopRuntimeStatus {
    fn default() -> Self {
        Self {
            runtime: "desktop",
            service_state: "starting",
            api_base_url: LOCAL_API_BASE_URL,
        }
    }
}

#[tauri::command]
pub fn desktop_runtime_status() -> DesktopRuntimeStatus {
    DesktopRuntimeStatus::default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_the_initial_desktop_contract() {
        let payload = serde_json::to_value(DesktopRuntimeStatus::default()).unwrap();

        assert_eq!(payload["runtime"], "desktop");
        assert_eq!(payload["serviceState"], "starting");
        assert_eq!(payload["apiBaseUrl"], "http://127.0.0.1:8765");
    }
}
