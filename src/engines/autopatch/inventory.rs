use crate::engines::ast::{locate_callsites, ScanConfig};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum DepHealth {
    Healthy,
    Behind,
    Deprecated,
    Retired,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderMeta {
    pub name: String,
    pub sdk_packages: Vec<String>,
    pub api_base_urls: Vec<String>,
    pub method_patterns: Vec<String>,
    pub latest_version: String,
    pub deprecation_deadline: String,
    pub migration_guide_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveredDep {
    pub provider: String,
    pub detected_version: String,
    pub latest_version: String,
    pub health: DepHealth,
    pub callsite_count: usize,
    pub affected_files: Vec<String>,
    pub deprecation_deadline: String,
    pub migration_guide_url: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Inventory {
    pub repo_root: String,
    pub dependencies: Vec<DiscoveredDep>,
    pub files_scanned: usize,
    pub total_callsites: usize,
}

impl Inventory {
    pub fn critical_count(&self) -> usize {
        self.dependencies
            .iter()
            .filter(|d| matches!(d.health, DepHealth::Deprecated | DepHealth::Retired))
            .count()
    }

    pub fn behind_count(&self) -> usize {
        self.dependencies
            .iter()
            .filter(|d| d.health == DepHealth::Behind)
            .count()
    }
}

/// Built-in provider registry. Returns known API providers with their
fn p(name: &str, pkgs: &[&str], urls: &[&str], methods: &[&str], ver: &str, deadline: &str, guide: &str) -> ProviderMeta {
    ProviderMeta {
        name: name.into(),
        sdk_packages: pkgs.iter().map(|s| s.to_string()).collect(),
        api_base_urls: urls.iter().map(|s| s.to_string()).collect(),
        method_patterns: methods.iter().map(|s| s.to_string()).collect(),
        latest_version: ver.into(),
        deprecation_deadline: deadline.into(),
        migration_guide_url: guide.into(),
    }
}

/// The built-in registry of known external API providers, containing their
/// SDK names, base URLs, method patterns, and version metadata.
pub fn builtin_providers() -> Vec<ProviderMeta> {
    vec![
        p("Stripe", &["stripe", "@stripe/stripe-node"], &["api.stripe.com"], &["charges.create", "paymentIntents.create", "customers.create", "refunds.create"], "2026-02-15", "", "https://docs.stripe.com/upgrades"),
        p("OpenAI", &["openai", "@openai/openai"], &["api.openai.com"], &["chat.completions.create", "responses.create", "completions.create", "embeddings.create"], "4.x", "", "https://platform.openai.com/docs/deprecations"),
        p("Anthropic", &["anthropic", "@anthropic-ai/sdk"], &["api.anthropic.com"], &["messages.create", "completions.create"], "2024-10-22", "", "https://docs.anthropic.com/en/docs/about-claude/model-deprecations"),
        p("Twilio", &["twilio"], &["api.twilio.com"], &["messages.create", "calls.create"], "5.x", "2026-04-28", "https://www.twilio.com/docs/global-infrastructure/api-domain-migration-guide"),
        p("GitHub", &["@octokit/rest", "octokit", "PyGithub"], &["api.github.com"], &[], "2022-11-28", "", "https://docs.github.com/en/rest/overview/api-versions"),
    ]
}

/// Run an inventory scan against a repo using the built-in provider registry.
///
/// For each provider, uses the AST callsite locator to find usages, then
/// assembles a DiscoveredDep with health status.
pub fn run_inventory(repo_root: &str) -> Inventory {
    run_inventory_with_providers(repo_root, &builtin_providers())
}

/// Run inventory with a custom provider list.
pub fn run_inventory_with_providers(repo_root: &str, providers: &[ProviderMeta]) -> Inventory {
    let mut dependencies = Vec::new();
    let mut total_callsites = 0;
    let mut total_files_scanned = 0;

    for provider in providers {
        let config = ScanConfig {
            sdk_names: provider.sdk_packages.clone(),
            api_base_urls: provider.api_base_urls.clone(),
            method_patterns: provider.method_patterns.clone(),
            ..Default::default()
        };

        let result = locate_callsites(repo_root, &config);
        total_files_scanned = total_files_scanned.max(result.files_scanned);

        if result.callsites.is_empty() {
            continue;
        }

        let affected_files = result.affected_files();
        let callsite_count = result.callsites.len();
        total_callsites += callsite_count;

        let health = if !provider.deprecation_deadline.is_empty() {
            DepHealth::Deprecated
        } else {
            DepHealth::Behind
        };

        dependencies.push(DiscoveredDep {
            provider: provider.name.clone(),
            detected_version: "detected".into(),
            latest_version: provider.latest_version.clone(),
            health,
            callsite_count,
            affected_files,
            deprecation_deadline: provider.deprecation_deadline.clone(),
            migration_guide_url: provider.migration_guide_url.clone(),
        });
    }

    Inventory {
        repo_root: repo_root.to_string(),
        dependencies,
        files_scanned: total_files_scanned,
        total_callsites,
    }
}

/// Render inventory as a human-readable report string.
pub fn render_inventory(inv: &Inventory) -> String {
    let mut out = String::new();
    out.push_str("=== External Dependency Inventory ===\n\n");
    out.push_str(&format!("Repo: {}\n", inv.repo_root));
    out.push_str(&format!("Files scanned: {}\n", inv.files_scanned));
    out.push_str(&format!("Total callsites: {}\n\n", inv.total_callsites));

    if inv.dependencies.is_empty() {
        out.push_str("No external API dependencies detected.\n");
        return out;
    }

    for dep in &inv.dependencies {
        let status_tag = match dep.health {
            DepHealth::Healthy => "[HEALTHY]",
            DepHealth::Behind => "[BEHIND]",
            DepHealth::Deprecated => "[DEPRECATED]",
            DepHealth::Retired => "[RETIRED]",
            DepHealth::Unknown => "[UNKNOWN]",
        };
        out.push_str(&format!(
            "  {} {} (latest: {})\n",
            status_tag, dep.provider, dep.latest_version
        ));
        out.push_str(&format!(
            "     Callsites: {}  Files: {}\n",
            dep.callsite_count,
            dep.affected_files.len()
        ));
        if !dep.deprecation_deadline.is_empty() {
            out.push_str(&format!(
                "     Deprecation deadline: {}\n",
                dep.deprecation_deadline
            ));
        }
        if !dep.migration_guide_url.is_empty() {
            out.push_str(&format!(
                "     Migration guide: {}\n",
                dep.migration_guide_url
            ));
        }
        out.push('\n');
    }

    let critical = inv.critical_count();
    if critical > 0 {
        out.push_str(&format!(
            "[ALERT] {} critical dependencies require immediate attention.\n",
            critical
        ));
        out.push_str("   Run `koyote autopatch` to generate verified migration PRs.\n");
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inventory_builtin_providers_has_entries() {
        let providers = builtin_providers();
        assert!(providers.len() >= 5);
        assert!(providers.iter().any(|p| p.name == "Stripe"));
        assert!(providers.iter().any(|p| p.name == "OpenAI"));
        assert!(providers.iter().any(|p| p.name == "Anthropic"));
    }

    #[test]
    fn inventory_scan_empty_dir() {
        let dir = std::env::temp_dir().join("koyote_inv_empty_test");
        let _ = std::fs::create_dir_all(&dir);
        let inv = run_inventory(dir.to_str().unwrap());
        assert!(inv.dependencies.is_empty());
        assert_eq!(inv.total_callsites, 0);
    }

    #[test]
    fn inventory_scan_finds_stripe() {
        let dir = std::env::temp_dir().join("koyote_inv_stripe_test");
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(
            dir.join("billing.ts"),
            "import Stripe from 'stripe';\nconst c = stripe.charges.create({ amount: 100 });\n",
        )
        .unwrap();

        let inv = run_inventory(dir.to_str().unwrap());
        assert!(!inv.dependencies.is_empty());
        let stripe = inv.dependencies.iter().find(|d| d.provider == "Stripe");
        assert!(stripe.is_some());
        assert!(stripe.unwrap().callsite_count >= 2);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn inventory_critical_count_detects_deprecated() {
        let inv = Inventory {
            repo_root: ".".into(),
            files_scanned: 10,
            total_callsites: 5,
            dependencies: vec![
                DiscoveredDep {
                    provider: "Twilio".into(),
                    detected_version: "4.x".into(),
                    latest_version: "5.x".into(),
                    health: DepHealth::Deprecated,
                    callsite_count: 3,
                    affected_files: vec!["sms.ts".into()],
                    deprecation_deadline: "2026-04-28".into(),
                    migration_guide_url: String::new(),
                },
                DiscoveredDep {
                    provider: "Stripe".into(),
                    detected_version: "2024-06-01".into(),
                    latest_version: "2026-02-15".into(),
                    health: DepHealth::Behind,
                    callsite_count: 2,
                    affected_files: vec!["pay.ts".into()],
                    deprecation_deadline: String::new(),
                    migration_guide_url: String::new(),
                },
            ],
        };
        assert_eq!(inv.critical_count(), 1);
        assert_eq!(inv.behind_count(), 1);
    }

    #[test]
    fn inventory_render_contains_providers() {
        let dir = std::env::temp_dir().join("koyote_inv_render_test");
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(
            dir.join("app.py"),
            "import openai\nclient = openai.chat.completions.create(model='gpt-4')\n",
        )
        .unwrap();

        let inv = run_inventory(dir.to_str().unwrap());
        let report = render_inventory(&inv);
        assert!(report.contains("OpenAI"));
        assert!(report.contains("External Dependency Inventory"));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn inventory_render_empty_shows_no_deps() {
        let inv = Inventory::default();
        let report = render_inventory(&inv);
        assert!(report.contains("No external API dependencies detected"));
    }

    #[test]
    fn inventory_scan_with_custom_providers() {
        let dir = std::env::temp_dir().join("koyote_inv_custom_test");
        let _ = std::fs::create_dir_all(&dir);
        std::fs::write(
            dir.join("client.ts"),
            "import { Acme } from 'acme-sdk';\nacme.widgets.create();\n",
        )
        .unwrap();

        let custom = vec![ProviderMeta {
            name: "Acme".into(),
            sdk_packages: vec!["acme-sdk".into()],
            api_base_urls: vec![],
            method_patterns: vec!["widgets.create".into()],
            latest_version: "3.0".into(),
            deprecation_deadline: "2026-12-01".into(),
            migration_guide_url: "https://acme.dev/migrate".into(),
        }];

        let inv = run_inventory_with_providers(dir.to_str().unwrap(), &custom);
        assert_eq!(inv.dependencies.len(), 1);
        assert_eq!(inv.dependencies[0].provider, "Acme");
        assert_eq!(inv.dependencies[0].health, DepHealth::Deprecated);

        std::fs::remove_dir_all(&dir).ok();
    }
}
