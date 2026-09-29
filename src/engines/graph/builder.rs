use std::path::Path;

use super::types::*;
use crate::engines::ast::{locate_callsites, ScanConfig};
use crate::engines::autopatch::inventory::{builtin_providers, ProviderMeta};
use crate::engines::autopatch::manifest_lockfile::resolve_dependency;

pub fn build_external_dependency_graph(
    repo_root: &str,
    providers: Option<&[ProviderMeta]>,
) -> ExternalDependencyGraph {
    let mut graph = ExternalDependencyGraph::default();
    let root = Path::new(repo_root);
    let default_providers = builtin_providers();
    let provider_list = providers.unwrap_or(&default_providers);

    for p in provider_list {
        let p_id = format!("provider:{}", p.name.to_lowercase());
        let p_node = ProviderNode {
            id: p_id.clone(),
            name: p.name.clone(),
            base_urls: p.api_base_urls.clone(),
            sdk_packages: p.sdk_packages.clone(),
            method_patterns: p.method_patterns.clone(),
            latest_version: p.latest_version.clone(),
            deprecation_deadline: p.deprecation_deadline.clone(),
            migration_guide_url: p.migration_guide_url.clone(),
        };
        graph.providers.push(p_node);

        let v_id = format!("version:{}:latest", p.name.to_lowercase());
        let v_node = VersionNode {
            id: v_id.clone(),
            provider_id: p_id.clone(),
            version: p.latest_version.clone(),
            release_date: "".into(),
            deprecation_deadline: p.deprecation_deadline.clone(),
            is_deprecated: !p.deprecation_deadline.is_empty(),
        };
        graph.versions.push(v_node);

        graph.edges.push(GraphEdge {
            from_id: p_id.clone(),
            to_id: v_id.clone(),
            kind: EdgeKind::Exposes,
            metadata: "latest_release".into(),
        });

        for m in &p.method_patterns {
            let c_id = format!("contract:{}:{}", p.name.to_lowercase(), m);
            let c_node = ContractNode {
                id: c_id.clone(),
                provider_id: p_id.clone(),
                version_id: v_id.clone(),
                method_pattern: m.clone(),
                http_method: "POST".into(),
                path: format!("/{}", m.replace('.', "/")),
                required_params: vec![],
                is_breaking_change: false,
                change_description: format!("Standard {} contract", m),
            };
            graph.contracts.push(c_node);

            graph.edges.push(GraphEdge {
                from_id: v_id.clone(),
                to_id: c_id.clone(),
                kind: EdgeKind::Exposes,
                metadata: m.clone(),
            });
        }
    }

    // 2. Scan Repository Manifests & Lockfiles
    for p in provider_list {
        for pkg in &p.sdk_packages {
            if let Ok(dep_info) = resolve_dependency(root, pkg) {
                let dep_id = format!("manifest_dep:{}:{}", dep_info.package_manager.name(), pkg);
                let dep_node = ManifestDepNode {
                    id: dep_id.clone(),
                    manifest_path: dep_info.manifest_path.clone(),
                    package_name: pkg.clone(),
                    declared_version: dep_info.declared_range.clone(),
                    resolved_version: dep_info.resolved_version.clone(),
                    package_manager: dep_info.package_manager.name().to_string(),
                };
                graph.manifest_deps.push(dep_node);

                let p_id = format!("provider:{}", p.name.to_lowercase());
                graph.edges.push(GraphEdge {
                    from_id: dep_id,
                    to_id: p_id,
                    kind: EdgeKind::Declares,
                    metadata: format!("v{}", dep_info.resolved_version),
                });
            }
        }
    }

    // 3. Locate AST Callsites across codebase
    for p in provider_list {
        let scan_cfg = ScanConfig {
            sdk_names: p.sdk_packages.clone(),
            api_base_urls: p.api_base_urls.clone(),
            method_patterns: p.method_patterns.clone(),
            ..ScanConfig::default()
        };

        let result = locate_callsites(repo_root, &scan_cfg);
        let p_id = format!("provider:{}", p.name.to_lowercase());

        for call in result.callsites {
            let call_id = format!("callsite:{}:{}:{}", call.file_path, call.line_number, call.column);
            let is_quarantine = call.matched_pattern.is_empty();
            let matched_contract = p.method_patterns.iter().find(|m| call.matched_pattern.contains(*m));

            let contract_id = matched_contract.map(|m| format!("contract:{}:{}", p.name.to_lowercase(), m));

            let call_node = CallsiteNode {
                id: call_id.clone(),
                file_path: call.file_path.clone(),
                line_number: call.line_number,
                column: call.column,
                line_content: call.line_content.clone(),
                matched_pattern: call.matched_pattern.clone(),
                is_quarantined: is_quarantine,
                target_contract_id: contract_id.clone(),
            };
            graph.callsites.push(call_node);

            // Edge: Callsite -> Provider
            graph.edges.push(GraphEdge {
                from_id: call_id.clone(),
                to_id: p_id.clone(),
                kind: EdgeKind::Invokes,
                metadata: call.matched_pattern.clone(),
            });

            // Edge: Callsite -> Contract if matched
            if let Some(c_id) = contract_id {
                graph.edges.push(GraphEdge {
                    from_id: call_id.clone(),
                    to_id: c_id,
                    kind: EdgeKind::Invokes,
                    metadata: "exact_contract_match".into(),
                });
            }

            // Check if this file is a wrapper / client factory
            let lower_path = call.file_path.to_ascii_lowercase();
            if lower_path.contains("/lib/")
                || lower_path.contains("/services/")
                || lower_path.contains("/utils/")
                || lower_path.contains("client.")
            {
                let wrap_id = format!("wrapper:{}", call.file_path);
                if !graph.wrappers.iter().any(|w| w.id == wrap_id) {
                    graph.wrappers.push(WrapperNode {
                        id: wrap_id.clone(),
                        file_path: call.file_path.clone(),
                        exported_symbol: call.matched_pattern.clone(),
                        wraps_package: p.name.clone(),
                    });
                    graph.edges.push(GraphEdge {
                        from_id: wrap_id,
                        to_id: p_id.clone(),
                        kind: EdgeKind::Wraps,
                        metadata: p.name.clone(),
                    });
                }
            }
        }
    }

    // 4. Synthesize Migration Nodes for detected drift
    for p in provider_list {
        let callsites_for_p: Vec<&CallsiteNode> = graph
            .callsites
            .iter()
            .filter(|c| {
                graph.edges.iter().any(|e| {
                    e.from_id == c.id
                        && e.to_id == format!("provider:{}", p.name.to_lowercase())
                        && e.kind == EdgeKind::Invokes
                })
            })
            .collect();

        if !callsites_for_p.is_empty() {
            let mig_id = format!("migration:{}:upgrade", p.name.to_lowercase());
            let is_stripe = p.name.eq_ignore_ascii_case("stripe");
            let is_openai = p.name.eq_ignore_ascii_case("openai");

            let desc = if is_stripe {
                "Convert integer charge amounts to string coercion for Stripe v13+/v22+ compatibility"
            } else if is_openai {
                let has_completion = callsites_for_p.iter().any(|c| c.line_content.contains("createCompletion") || c.matched_pattern.contains("createCompletion"));
                let has_chat = callsites_for_p.iter().any(|c| c.line_content.contains("createChatCompletion") || c.matched_pattern.contains("createChatCompletion"));
                if has_completion && !has_chat {
                    "Migrate createCompletion to completions.create for OpenAI v4+ SDK (preserving prompt semantics)"
                } else if has_chat && !has_completion {
                    "Migrate createChatCompletion to chat.completions.create for OpenAI v4+ SDK"
                } else {
                    "Migrate OpenAI v3 API to v4 SDK (createCompletion -> completions.create, createChatCompletion -> chat.completions.create)"
                }
            } else {
                "Upstream SDK version alignment"
            };

            let mig_node = MigrationNode {
                id: mig_id.clone(),
                provider_name: p.name.clone(),
                from_version: "v0".into(),
                to_version: p.latest_version.clone(),
                description: desc.into(),
                affected_callsites: callsites_for_p.iter().map(|c| c.id.clone()).collect(),
                is_merge_ready: is_stripe || is_openai,
            };
            graph.migrations.push(mig_node);

            for c in callsites_for_p {
                graph.edges.push(GraphEdge {
                    from_id: mig_id.clone(),
                    to_id: c.id.clone(),
                    kind: EdgeKind::Repairs,
                    metadata: "ast_transform_ready".into(),
                });
            }
        }
    }

    graph
}
