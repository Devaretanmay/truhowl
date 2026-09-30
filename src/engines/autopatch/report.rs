use super::types::*;

pub fn render_markdown(plan: &MaintenancePlan) -> String {
    let mut out = String::new();

    // Header.
    out.push_str(&format!(
        "# AutoPatch: {} ({} -> {})\n\n",
        plan.api_name, plan.old_version, plan.new_version
    ));

    // Status badge.
    let badge = match plan.status {
        PlanStatus::Clean => "[STATUS: CLEAN] **No breaking changes detected.**",
        PlanStatus::NoImpact => {
            "[STATUS: NO_IMPACT] **Breaking changes detected but no codebase impact found.**"
        }
        PlanStatus::ActionRequired => {
            "[STATUS: ACTION_REQUIRED] **Action required: breaking changes affect your code.**"
        }
    };
    out.push_str(&format!("{badge}\n\n"));

    if plan.status == PlanStatus::Clean {
        return out;
    }

    // Summary table.
    out.push_str("## Summary\n\n");
    out.push_str("| Metric | Value |\n");
    out.push_str("| :--- | :--- |\n");
    out.push_str(&format!(
        "| Breaking changes | {} |\n",
        plan.breaking_changes
    ));
    out.push_str(&format!(
        "| Affected files | {} |\n",
        plan.total_affected_files
    ));
    out.push_str(&format!(
        "| Affected callsites | {} |\n",
        plan.total_affected_callsites
    ));
    out.push_str(&format!(
        "| Patch targets | {} |\n\n",
        plan.patch_targets.len()
    ));

    // Impacted endpoints.
    if !plan.impacted_endpoints.is_empty() {
        out.push_str("## Impacted Endpoints\n\n");
        for ie in &plan.impacted_endpoints {
            let severity_tag = match ie.severity {
                crate::engines::schema::BreakingSeverity::Breaking => "[BREAKING]",
                crate::engines::schema::BreakingSeverity::Warning => "[WARNING]",
                crate::engines::schema::BreakingSeverity::Info => "[INFO]",
            };
            out.push_str(&format!(
                "### {} `{} {}`\n\n",
                severity_tag,
                ie.method.to_uppercase(),
                ie.path
            ));

            // Precision breakdown — the key differentiator.
            if ie.total_sdk_references > 0 {
                out.push_str("**Impact Analysis:**\n\n");
                out.push_str("| Classification | Count |\n");
                out.push_str("| :--- | :--- |\n");
                out.push_str(&format!(
                    "| Total SDK references scanned | {} |\n",
                    ie.total_sdk_references
                ));
                out.push_str(&format!(
                    "| Confirmed affected (operation match) | {} |\n",
                    ie.confirmed_count
                ));
                out.push_str(&format!(
                    "| Correctly rejected (different operation) | {} |\n",
                    ie.false_positive_count
                ));
                out.push_str(&format!(
                    "| Unresolvable (import/type reference) | {} |\n\n",
                    ie.unresolvable_count
                ));
            }

            out.push_str("**Upstream changes:**\n\n");
            for summary in &ie.change_summary {
                out.push_str(&format!("- {summary}\n"));
            }
            out.push('\n');

            if !ie.affected_callsites.is_empty() {
                if ie.confirmed_count > 0 {
                    out.push_str("**Confirmed affected callsites:**\n\n");
                } else {
                    out.push_str(
                        "**Requires manual review (could not resolve to specific operation):**\n\n",
                    );
                }
                for ac in &ie.affected_callsites {
                    out.push_str(&format!(
                        "- `{}` L{}: `{}`\n",
                        ac.file_path,
                        ac.line_number,
                        ac.line_content.trim()
                    ));
                }
                out.push('\n');
            }
        }
    }

    // Patch targets.
    if !plan.patch_targets.is_empty() {
        out.push_str("## Patch Targets\n\n");
        out.push_str("| File | Lines | Reason |\n");
        out.push_str("| :--- | :--- | :--- |\n");
        for pt in &plan.patch_targets {
            let lines = pt
                .line_numbers
                .iter()
                .map(|l| l.to_string())
                .collect::<Vec<_>>()
                .join(", ");
            out.push_str(&format!(
                "| `{}` | {} | {} |\n",
                pt.file_path, lines, pt.reason
            ));
        }
        out.push('\n');
    }

    // Verification specs.
    if !plan.verification_specs.is_empty() {
        out.push_str("## Verification Requirements\n\n");
        for vs in &plan.verification_specs {
            out.push_str(&format!(
                "- `{} {}`: verify fields {}\n",
                vs.method.to_uppercase(),
                vs.endpoint,
                vs.fields_to_verify.join(", ")
            ));
        }
        out.push('\n');
    }

    // Footer.
    out.push_str("---\n");
    out.push_str("*Generated by Truhowl AutoPatch*\n");

    out
}

/// Render a high-trust, clinical CLI report distinguishing ConfirmedAffected,
/// ProvablyUnaffected, and Unresolved (with typed breakdown).
pub fn render_trust_report_cli(plan: &MaintenancePlan) -> String {
    let mut out = String::new();
    out.push_str(
        "================================================================================\n",
    );
    out.push_str(&format!("UPSTREAM CONTRACT DRIFT: {}\n", plan.api_name));
    out.push_str(
        "================================================================================\n\n",
    );

    let total_confirmed: usize = plan
        .impacted_endpoints
        .iter()
        .map(|ie| ie.confirmed_count)
        .sum();
    let total_unaffected: usize = plan
        .impacted_endpoints
        .iter()
        .map(|ie| ie.false_positive_count)
        .sum();
    let total_unresolved: usize = plan
        .impacted_endpoints
        .iter()
        .map(|ie| ie.unresolvable_count)
        .sum();

    // 1. CONFIRMED AFFECTED
    out.push_str(&format!(
        "[CONFIRMED AFFECTED] {} callsites\n\n",
        total_confirmed
    ));
    if total_confirmed == 0 {
        out.push_str("  (None)\n\n");
    } else {
        for ie in &plan.impacted_endpoints {
            if ie.confirmed_count > 0 {
                out.push_str(&format!("{} {}\n", ie.method.to_uppercase(), ie.path));
                for ac in &ie.affected_callsites {
                    out.push_str(&format!("  {}:L{}\n", ac.file_path, ac.line_number));
                }
                out.push_str("  Evidence:\n");
                out.push_str("  - SDK operation resolved\n");
                out.push_str("  - HTTP method matched\n");
                out.push_str("  - Endpoint path matched\n");
                out.push_str("  - Changed parameter or response field verified\n");
                out.push_str("  - Surgical patch rule available\n\n");
                out.push_str("  Action:\n");
                out.push_str("  Surgical patch generated & queued for isolated verification.\n\n");
            }
        }
    }

    out.push_str("---\n\n");

    // 2. PROVABLY UNAFFECTED
    out.push_str(&format!(
        "[PROVABLY UNAFFECTED] {} callsites\n\n",
        total_unaffected
    ));
    if total_unaffected == 0 {
        out.push_str("  (None)\n\n");
    } else {
        for ie in &plan.impacted_endpoints {
            if ie.false_positive_count > 0 {
                out.push_str(&format!(
                    "Target Endpoint: {} {}\n",
                    ie.method.to_uppercase(),
                    ie.path
                ));
                for ac in &ie.provably_unaffected_callsites {
                    out.push_str(&format!(
                        "  {}:L{} (`{}`)\n",
                        ac.file_path, ac.line_number, ac.matched_pattern
                    ));
                }
                out.push_str("  Reason:\n");
                out.push_str("  SDK reference resolves to a different API operation.\n\n");
                out.push_str("  Action:\n");
                out.push_str("  Zero change required. Proactively rejected from patch queue.\n\n");
            }
        }
    }

    out.push_str("---\n\n");

    // 3. UNRESOLVED: HUMAN REVIEW REQUIRED
    out.push_str(&format!(
        "[UNRESOLVED: HUMAN REVIEW REQUIRED] {} references\n\n",
        total_unresolved
    ));
    if total_unresolved == 0 {
        out.push_str("  (None)\n\n");
    } else {
        let mut reason_counts: std::collections::BTreeMap<String, usize> =
            std::collections::BTreeMap::new();
        for ie in &plan.impacted_endpoints {
            for u in &ie.unresolved_callsites {
                *reason_counts
                    .entry(u.reason.as_str().to_string())
                    .or_default() += 1;
            }
        }
        for (reason_name, count) in reason_counts {
            out.push_str(&format!("  {}: {}\n", reason_name, count));
        }
        out.push('\n');
        out.push_str("  Auto-fix:\n");
        out.push_str("  DISABLED (Evidence threshold not met. Quarantined for safety.)\n\n");
        out.push_str("  Reason:\n");
        out.push_str("  Static AST cannot prove target API contract without runtime traces.\n\n");
    }

    out.push_str(
        "================================================================================\n",
    );
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engines::schema::BreakingSeverity;

    fn sample_plan() -> MaintenancePlan {
        MaintenancePlan {
            status: PlanStatus::ActionRequired,
            api_name: "Payments API".into(),
            old_version: "2024-06-01".into(),
            new_version: "2026-02-15".into(),
            breaking_changes: 3,
            total_affected_files: 2,
            total_affected_callsites: 4,
            impacted_endpoints: vec![ImpactedEndpoint {
                path: "/v1/charges".into(),
                method: "post".into(),
                change_summary: vec![
                    "Parameter 'amount' type changed from integer to string".into(),
                    "Parameter 'description' was removed".into(),
                ],
                severity: BreakingSeverity::Breaking,
                confirmed_count: 2,
                false_positive_count: 14,
                unresolvable_count: 2,
                total_sdk_references: 18,
                unresolved_callsites: vec![UnresolvedCallsite {
                    reason: UncertaintyReason::ImportReference,
                    file_path: "src/billing.ts".into(),
                    line_number: 1,
                    source_text: "import Stripe from 'stripe'".into(),
                    provider: "Payments API".into(),
                    inferred_operation: None,
                    confidence_evidence: "Import statement".into(),
                    why_autofix_disabled: "Import only".into(),
                }],
                provably_unaffected_callsites: vec![],
                affected_callsites: vec![
                    AffectedCallsite {
                        file_path: "src/billing.ts".into(),
                        line_number: 12,
                        line_content:
                            "  const charge = await stripe.charges.create({ amount: 2000 });".into(),
                        matched_pattern: "charges.create".into(),
                    },
                    AffectedCallsite {
                        file_path: "src/api.py".into(),
                        line_number: 8,
                        line_content: "  resp = requests.post('https://api.stripe.com/v1/charges')"
                            .into(),
                        matched_pattern: "api.stripe.com".into(),
                    },
                ],
            }],
            patch_targets: vec![
                PatchTarget {
                    file_path: "src/billing.ts".into(),
                    line_numbers: vec![12],
                    reason: "POST /v1/charges has breaking changes".into(),
                    upstream_change: "amount type integer→string".into(),
                },
                PatchTarget {
                    file_path: "src/api.py".into(),
                    line_numbers: vec![8],
                    reason: "POST /v1/charges has breaking changes".into(),
                    upstream_change: "amount type integer→string".into(),
                },
            ],
            verification_specs: vec![VerificationSpec {
                endpoint: "/v1/charges".into(),
                method: "post".into(),
                fields_to_verify: vec!["parameters.amount".into(), "response.amount".into()],
            }],
        }
    }

    #[test]
    fn report_render_markdown_contains_header() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("AutoPatch: Payments API"));
        assert!(md.contains("2024-06-01"));
        assert!(md.contains("2026-02-15"));
    }

    #[test]
    fn report_render_markdown_contains_status_badge() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("Action required"));
    }

    #[test]
    fn report_render_markdown_contains_summary_table() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("Breaking changes | 3"));
        assert!(md.contains("Affected files | 2"));
        assert!(md.contains("Affected callsites | 4"));
    }

    #[test]
    fn report_render_markdown_contains_impacted_endpoints() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("POST /v1/charges"));
        assert!(md.contains("amount"));
        assert!(md.contains("src/billing.ts"));
    }

    #[test]
    fn report_render_markdown_contains_patch_targets() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("Patch Targets"));
        assert!(md.contains("src/billing.ts"));
        assert!(md.contains("src/api.py"));
    }

    #[test]
    fn report_render_markdown_contains_verification() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("Verification Requirements"));
        assert!(md.contains("parameters.amount"));
    }

    #[test]
    fn report_render_markdown_clean_plan_is_short() {
        let clean = MaintenancePlan {
            status: PlanStatus::Clean,
            api_name: "Test".into(),
            old_version: "1".into(),
            new_version: "2".into(),
            ..Default::default()
        };
        let md = render_markdown(&clean);
        assert!(md.contains("No breaking changes"));
        assert!(!md.contains("Patch Targets"));
    }

    #[test]
    fn report_render_json_is_valid() {
        let json = serde_json::to_string_pretty(&sample_plan()).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(
            parsed.get("api_name").unwrap().as_str().unwrap(),
            "Payments API"
        );
        assert_eq!(parsed.get("breaking_changes").unwrap().as_u64().unwrap(), 3);
    }

    #[test]
    fn report_render_markdown_footer() {
        let md = render_markdown(&sample_plan());
        assert!(md.contains("Generated by Truhowl AutoPatch"));
    }

    #[test]
    fn trust_report_renders_three_tiers() {
        let mut plan = sample_plan();
        plan.impacted_endpoints[0].provably_unaffected_callsites = vec![AffectedCallsite {
            file_path: "src/checkout.ts".into(),
            line_number: 5,
            line_content: "stripe.checkout.sessions.create()".into(),
            matched_pattern: "checkout.sessions.create".into(),
        }];
        plan.impacted_endpoints[0].unresolved_callsites = vec![UnresolvedCallsite {
            reason: UncertaintyReason::ImportReference,
            file_path: "src/import.ts".into(),
            line_number: 1,
            source_text: "import Stripe from 'stripe';".into(),
            provider: "Stripe".into(),
            inferred_operation: None,
            confidence_evidence: "Import statement only".into(),
            why_autofix_disabled: "Uncertainty threshold exceeded".into(),
        }];
        let rep = render_trust_report_cli(&plan);
        assert!(rep.contains("[CONFIRMED AFFECTED]"));
        assert!(rep.contains("[PROVABLY UNAFFECTED]"));
        assert!(rep.contains("[UNRESOLVED: HUMAN REVIEW REQUIRED]"));
    }
}
