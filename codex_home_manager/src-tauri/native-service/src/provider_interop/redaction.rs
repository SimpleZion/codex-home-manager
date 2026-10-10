use serde_json::{Map, Value};

const REDACTED: &str = "[REDACTED]";

pub fn redact_json(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(redact_object(object)),
        Value::Array(items) => Value::Array(items.iter().map(redact_json).collect()),
        Value::String(text) if secret_string(text) => Value::String(REDACTED.to_string()),
        _ => value.clone(),
    }
}

pub fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(object) => object.iter().any(|(key, item)| {
            (sensitive_key(key) && item.as_str() != Some(REDACTED)) || contains_secret(item)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        Value::String(text) => text != REDACTED && secret_string(text),
        _ => false,
    }
}

fn redact_object(object: &Map<String, Value>) -> Map<String, Value> {
    object
        .iter()
        .map(|(key, value)| {
            let redacted = if sensitive_key(key) && value.as_str() != Some(REDACTED) {
                Value::String(REDACTED.to_string())
            } else {
                redact_json(value)
            };
            (key.clone(), redacted)
        })
        .collect()
}

fn sensitive_key(key: &str) -> bool {
    let normalized: String = key
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    matches!(
        normalized.as_str(),
        "authorization"
            | "proxyauthorization"
            | "apikey"
            | "token"
            | "accesstoken"
            | "refreshtoken"
            | "experimentalbearertoken"
            | "password"
            | "secret"
            | "clientsecret"
            | "cookie"
            | "setcookie"
    )
}

fn secret_string(text: &str) -> bool {
    let trimmed = text.trim();
    let lower = trimmed.to_ascii_lowercase();
    lower.starts_with("bearer ")
        || lower.starts_with("basic ")
        || (lower.starts_with("sk-") && trimmed.len() >= 12)
}
