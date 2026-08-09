use serde::Serialize;

pub const SERVICE_STATE_EVENT: &str = "service-state";

#[derive(Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceConnection {
    base_url: String,
    token: String,
}

impl ServiceConnection {
    pub fn new(base_url: String, token: String) -> Self {
        Self { base_url, token }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn token(&self) -> &str {
        &self.token
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleStatus {
    Starting,
    Ready,
    Restarting,
    Stopping,
    Stopped,
    Failed,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceStateEvent {
    generation: u64,
    status: LifecycleStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    connection: Option<ServiceConnection>,
}

impl ServiceStateEvent {
    pub fn transition(generation: u64, status: LifecycleStatus) -> Self {
        Self {
            generation,
            status,
            connection: None,
        }
    }

    pub fn connected(generation: u64, connection: ServiceConnection) -> Self {
        Self {
            generation,
            status: LifecycleStatus::Ready,
            connection: Some(connection),
        }
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn status(&self) -> LifecycleStatus {
        self.status
    }

    pub fn connection(&self) -> Option<&ServiceConnection> {
        self.connection.as_ref()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_connection_serializes_only_camel_case_boundary_fields() {
        let token = "a".repeat(43);
        let connection = ServiceConnection::new("http://127.0.0.1:49152".to_owned(), token.clone());
        let serialized = serde_json::to_value(&connection).expect("connection must serialize");
        let object = serialized
            .as_object()
            .expect("connection boundary must be an object");

        assert!(object.len() == 2);
        assert!(
            object.get("baseUrl").and_then(|value| value.as_str())
                == Some("http://127.0.0.1:49152")
        );
        assert!(object.get("token").and_then(|value| value.as_str()) == Some(token.as_str()));
    }

    #[test]
    fn service_state_event_has_a_fixed_typed_shape_without_paths() {
        let event = ServiceStateEvent::transition(7, LifecycleStatus::Restarting);
        let serialized = serde_json::to_value(&event).expect("state event must serialize");
        let object = serialized
            .as_object()
            .expect("state event must be an object");

        assert!(SERVICE_STATE_EVENT == "service-state");
        assert!(object.len() == 2);
        assert!(object.get("generation").and_then(|value| value.as_u64()) == Some(7));
        assert!(object.get("status").and_then(|value| value.as_str()) == Some("restarting"));
        assert!(!object.contains_key("connection"));
        assert!(!object.contains_key("path"));
    }
}
