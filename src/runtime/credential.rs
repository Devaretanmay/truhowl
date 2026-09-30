use serde::{Deserialize, Serialize};

/// A single credential-injection rule.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RouteConfig {
    pub prefix: String,
    pub upstream: String,
    #[serde(default)]
    pub credential_source: String,
}

impl RouteConfig {
    pub fn resolve_credential(&self) -> String {
        if let Some(var_name) = self.credential_source.strip_prefix("env:") {
            std::env::var(var_name).unwrap_or_default()
        } else {
            String::new()
        }
    }

    pub fn matches(&self, path: &str) -> bool {
        path.starts_with(&self.prefix)
    }

    pub fn rewrite_path(&self, path: &str) -> String {
        let relative = &path[self.prefix.len()..];
        let relative = if relative.starts_with('/') {
            relative.to_string()
        } else {
            format!("/{relative}")
        };
        format!("{}{}", self.upstream.trim_end_matches('/'), relative)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn route() -> RouteConfig {
        RouteConfig {
            prefix: "/openai".to_string(),
            upstream: "https://api.openai.com".to_string(),
            credential_source: "env:TEST_TRUHOWL_KEY".to_string(),
        }
    }

    #[test]
    fn matches_prefix() {
        let r = route();
        assert!(r.matches("/openai/v1/chat"));
        assert!(!r.matches("/anthropic/v1"));
    }

    #[test]
    fn rewrites_path() {
        let r = route();
        assert_eq!(
            r.rewrite_path("/openai/v1/chat"),
            "https://api.openai.com/v1/chat"
        );
        assert_eq!(r.rewrite_path("/openai"), "https://api.openai.com/");
    }

    #[test]
    fn resolves_env_credential() {
        std::env::set_var("TEST_TRUHOWL_KEY", "sk-test-123");
        let r = route();
        assert_eq!(r.resolve_credential(), "sk-test-123");
        std::env::remove_var("TEST_TRUHOWL_KEY");
    }
}
