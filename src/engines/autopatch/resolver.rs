use super::types::UncertaintyReason;
use crate::engines::ast::CallsiteKind;
use crate::engines::schema::ParsedSpec;
use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperationResolution {
    pub http_method: String,
    pub path: String,
}

#[derive(Debug, Clone, Default)]
pub struct SpecRouteIndex {
    routes: HashMap<String, OperationResolution>,
}

impl SpecRouteIndex {
    pub fn new() -> Self {
        Self {
            routes: HashMap::new(),
        }
    }

    pub fn from_parsed_spec(spec: &ParsedSpec) -> Self {
        let mut index = Self::new();
        for endpoint in spec.endpoints.values() {
            index.index_endpoint(&endpoint.path, &endpoint.method);
        }
        index
    }

    pub fn index_endpoint(&mut self, path: &str, http_method: &str) {
        let method_upper = http_method.to_uppercase();
        let raw_segments: Vec<&str> = path
            .split('/')
            .map(|s| s.trim_end_matches(".json"))
            .filter(|s| !s.is_empty() && !is_version_or_base_segment(s))
            .collect();

        if raw_segments.is_empty() {
            return;
        }

        let last_is_param = raw_segments
            .last()
            .map(|s| s.starts_with('{') && s.ends_with('}'))
            .unwrap_or(false);

        const ACTION_VERBS: &[&str] = &[
            "confirm", "capture", "cancel", "close", "pay", "finalize", "expire", "attach",
            "detach",
        ];

        let last_seg = raw_segments.last().copied().unwrap_or("");
        let is_sub_action = ACTION_VERBS.contains(&last_seg.to_ascii_lowercase().as_str());

        let sub_action = if is_sub_action { Some(last_seg) } else { None };

        let resource_segments: Vec<&str> = if let Some(act) = sub_action {
            raw_segments
                .iter()
                .copied()
                .filter(|s| !(s.starts_with('{') && s.ends_with('}')) && *s != act)
                .collect()
        } else if raw_segments.len() >= 2 && raw_segments[raw_segments.len() - 2].starts_with('{') {
            vec![raw_segments[raw_segments.len() - 1]]
        } else {
            raw_segments
                .iter()
                .copied()
                .filter(|s| !(s.starts_with('{') && s.ends_with('}')))
                .collect()
        };

        if resource_segments.is_empty() {
            return;
        }

        let camel_resource = to_camel_case_chain(&resource_segments);
        let snake_resource = to_snake_case_chain(&resource_segments);

        let actions = if let Some(act) = sub_action {
            vec![act.to_string()]
        } else {
            synthesize_actions(&method_upper, last_is_param)
        };

        let resolution = OperationResolution {
            http_method: method_upper,
            path: path.to_string(),
        };

        for act in actions {
            self.routes
                .insert(format!("{camel_resource}.{act}"), resolution.clone());
            self.routes
                .insert(format!("{snake_resource}.{act}"), resolution.clone());
        }
    }

    /// Number of indexed route patterns.
    pub fn len(&self) -> usize {
        self.routes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.routes.is_empty()
    }

    /// Look up an SDK method chain in the dynamic index.
    pub fn resolve(&self, pattern: &str) -> Option<OperationResolution> {
        let chain = strip_sdk_prefix(pattern.trim().trim_end_matches('(').trim());
        self.routes.get(chain).cloned()
    }
}

/// Helper: skip API version prefixes like v1, v2, api, 2010-04-01
fn is_version_or_base_segment(s: &str) -> bool {
    let lower = s.to_ascii_lowercase();
    lower == "v1"
        || lower == "v2"
        || lower == "v3"
        || lower == "api"
        || lower.starts_with("20") && lower.contains('-')
}

fn to_camel_case_chain(segments: &[&str]) -> String {
    segments
        .iter()
        .map(|s| to_camel_case(s))
        .collect::<Vec<_>>()
        .join(".")
}

fn to_snake_case_chain(segments: &[&str]) -> String {
    segments
        .iter()
        .map(|s| s.to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join(".")
}

fn to_camel_case(s: &str) -> String {
    let mut result = String::new();
    let mut capitalize_next = false;
    for (i, c) in s.chars().enumerate() {
        if c == '_' || c == '-' {
            capitalize_next = true;
        } else if capitalize_next {
            result.push(c.to_ascii_uppercase());
            capitalize_next = false;
        } else if i == 0 {
            result.push(c.to_ascii_lowercase());
        } else {
            result.push(c);
        }
    }
    result
}

fn synthesize_actions(method: &str, is_item: bool) -> Vec<String> {
    match (method, is_item) {
        ("POST", false) => vec!["create".into(), "new".into()],
        ("POST", true) => vec!["update".into()],
        ("GET", false) => vec!["list".into(), "all".into()],
        ("GET", true) => vec!["retrieve".into(), "get".into()],
        ("PUT" | "PATCH", _) => vec!["update".into(), "modify".into()],
        ("DELETE", _) => vec!["del".into(), "delete".into(), "cancel".into()],
        _ => vec![],
    }
}

/// Resolve a method call pattern to an HTTP operation for Stripe's SDK.
pub fn resolve_stripe_method(pattern: &str) -> Option<OperationResolution> {
    resolve_canonical_method(pattern)
}

/// Resolve a method pattern across standard canonical providers (Stripe, OpenAI, Anthropic, Twilio, GitHub).
pub fn resolve_canonical_method(pattern: &str) -> Option<OperationResolution> {
    let pattern = pattern.trim().trim_end_matches('(').trim();
    let chain = strip_sdk_prefix(pattern);

    let routes: &[(&str, &str, &str)] = &[
        ("charges.create", "POST", "/v1/charges"),
        ("charges.retrieve", "GET", "/v1/charges"),
        ("charges.update", "POST", "/v1/charges"),
        ("charges.list", "GET", "/v1/charges"),
        ("charges.capture", "POST", "/v1/charges"),
        ("refunds.create", "POST", "/v1/refunds"),
        ("refunds.retrieve", "GET", "/v1/refunds"),
        ("refunds.update", "POST", "/v1/refunds"),
        ("refunds.list", "GET", "/v1/refunds"),
        ("paymentIntents.create", "POST", "/v1/payment_intents"),
        ("paymentIntents.confirm", "POST", "/v1/payment_intents"),
        ("paymentIntents.capture", "POST", "/v1/payment_intents"),
        ("paymentIntents.cancel", "POST", "/v1/payment_intents"),
        ("paymentIntents.retrieve", "GET", "/v1/payment_intents"),
        ("paymentIntents.list", "GET", "/v1/payment_intents"),
        ("paymentIntents.update", "POST", "/v1/payment_intents"),
        ("setupIntents.create", "POST", "/v1/setup_intents"),
        ("setupIntents.retrieve", "GET", "/v1/setup_intents"),
        ("setupIntents.confirm", "POST", "/v1/setup_intents"),
        ("customers.create", "POST", "/v1/customers"),
        ("customers.retrieve", "GET", "/v1/customers"),
        ("customers.update", "POST", "/v1/customers"),
        ("customers.list", "GET", "/v1/customers"),
        ("customers.del", "DELETE", "/v1/customers"),
        ("checkout.sessions.create", "POST", "/v1/checkout/sessions"),
        ("checkout.sessions.retrieve", "GET", "/v1/checkout/sessions"),
        ("checkout.sessions.list", "GET", "/v1/checkout/sessions"),
        ("checkout.sessions.expire", "POST", "/v1/checkout/sessions"),
        ("subscriptions.create", "POST", "/v1/subscriptions"),
        ("subscriptions.retrieve", "GET", "/v1/subscriptions"),
        ("subscriptions.update", "POST", "/v1/subscriptions"),
        ("subscriptions.cancel", "DELETE", "/v1/subscriptions"),
        ("subscriptions.list", "GET", "/v1/subscriptions"),
        ("prices.create", "POST", "/v1/prices"),
        ("prices.retrieve", "GET", "/v1/prices"),
        ("prices.update", "POST", "/v1/prices"),
        ("prices.list", "GET", "/v1/prices"),
        ("products.create", "POST", "/v1/products"),
        ("products.retrieve", "GET", "/v1/products"),
        ("products.update", "POST", "/v1/products"),
        ("products.list", "GET", "/v1/products"),
        ("invoices.create", "POST", "/v1/invoices"),
        ("invoices.retrieve", "GET", "/v1/invoices"),
        ("invoices.pay", "POST", "/v1/invoices"),
        ("invoices.finalize", "POST", "/v1/invoices"),
        ("invoices.list", "GET", "/v1/invoices"),
        (
            "billingPortal.sessions.create",
            "POST",
            "/v1/billing_portal/sessions",
        ),
        (
            "billingPortal.configurations.create",
            "POST",
            "/v1/billing_portal/configurations",
        ),
        ("webhooks.constructEvent", "POST", "/v1/webhook_endpoints"),
        (
            "webhooks.generateTestHeaderString",
            "POST",
            "/v1/webhook_endpoints",
        ),
        ("paymentMethods.create", "POST", "/v1/payment_methods"),
        ("paymentMethods.attach", "POST", "/v1/payment_methods"),
        ("paymentMethods.detach", "POST", "/v1/payment_methods"),
        ("paymentMethods.retrieve", "GET", "/v1/payment_methods"),
        ("paymentMethods.list", "GET", "/v1/payment_methods"),
        ("coupons.create", "POST", "/v1/coupons"),
        ("promotionCodes.create", "POST", "/v1/promotion_codes"),
        ("disputes.retrieve", "GET", "/v1/disputes"),
        ("disputes.update", "POST", "/v1/disputes"),
        ("disputes.close", "POST", "/v1/disputes"),
        ("events.retrieve", "GET", "/v1/events"),
        ("events.list", "GET", "/v1/events"),
        ("accounts.create", "POST", "/v1/accounts"),
        ("accounts.retrieve", "GET", "/v1/accounts"),
        ("accounts.update", "POST", "/v1/accounts"),
        ("messages.create", "POST", "/v1/messages"),
        ("completions.create", "POST", "/v1/complete"),
        ("chat.completions.create", "POST", "/v1/chat/completions"),
        ("embeddings.create", "POST", "/v1/embeddings"),
        ("models.list", "GET", "/v1/models"),
        ("models.retrieve", "GET", "/v1/models"),
        (
            "messages.create",
            "POST",
            "/2010-04-01/Accounts/{AccountSid}/Messages.json",
        ),
        (
            "calls.create",
            "POST",
            "/2010-04-01/Accounts/{AccountSid}/Calls.json",
        ),
        ("rest.repos.get", "GET", "/repos/{owner}/{repo}"),
        ("rest.pulls.create", "POST", "/repos/{owner}/{repo}/pulls"),
        ("rest.issues.create", "POST", "/repos/{owner}/{repo}/issues"),
    ];

    for (chain_prefix, http_method, path) in routes {
        if chain == *chain_prefix || chain.starts_with(&format!("{chain_prefix}(")) {
            return Some(OperationResolution {
                http_method: http_method.to_string(),
                path: path.to_string(),
            });
        }
    }

    None
}

/// Strip common SDK variable prefixes from a method chain string.
fn strip_sdk_prefix(s: &str) -> &str {
    const PREFIXES: &[&str] = &[
        "await stripe.",
        "await openai.",
        "await anthropic.",
        "await twilio.",
        "await octokit.",
        "await client.",
        "await this.stripe.",
        "await this.openai.",
        "await this.anthropic.",
        "await this.client.",
        "stripe.",
        "openai.",
        "anthropic.",
        "twilio.",
        "octokit.",
        "client.",
        "this.stripe.",
        "this.openai.",
        "this.anthropic.",
        "this.client.",
        "stripeClient.",
        "stripeApi.",
    ];
    for prefix in PREFIXES {
        if let Some(rest) = s.strip_prefix(prefix) {
            return rest;
        }
    }
    s
}

/// Determine the confidence level of a callsite match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MatchConfidence {
    /// Exact operation match: the callsite resolves directly to the changed endpoint.
    Confirmed,
    /// The callsite uses the same SDK but a different operation — provably unaffected.
    FalsePositive,
    /// Reference cannot be resolved with certainty, with typed reason and explanation.
    Unresolved(UncertaintyReason, String),
    /// Legacy fallback alias.
    Unresolvable,
}

impl MatchConfidence {
    pub fn is_confirmed(&self) -> bool {
        matches!(self, Self::Confirmed)
    }

    pub fn is_false_positive(&self) -> bool {
        matches!(self, Self::FalsePositive)
    }

    pub fn is_unresolved(&self) -> bool {
        matches!(self, Self::Unresolvable | Self::Unresolved(_, _))
    }

    pub fn uncertainty_reason(&self) -> Option<UncertaintyReason> {
        match self {
            Self::Unresolved(r, _) => Some(*r),
            Self::Unresolvable => Some(UncertaintyReason::InsufficientEvidence),
            _ => None,
        }
    }
}

/// Check if a concrete URL matches an OpenAPI path template with parameter variables (e.g. {AccountSid}).
pub fn url_matches_templated_path(url: &str, templated_path: &str) -> bool {
    let clean_url = url.split('?').next().unwrap_or(url);
    let path = if let Some(pos) = clean_url.find("://") {
        if let Some(slash_pos) = clean_url[pos + 3..].find('/') {
            &clean_url[pos + 3 + slash_pos..]
        } else {
            clean_url
        }
    } else {
        clean_url
    };

    let url_segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    let template_segments: Vec<&str> = templated_path
        .split('/')
        .filter(|s| !s.is_empty())
        .collect();

    if url_segments.len() != template_segments.len() {
        return false;
    }

    for (us, ts) in url_segments.iter().zip(template_segments.iter()) {
        if ts.starts_with('{') && ts.ends_with('}') {
            continue;
        }
        if us != ts {
            return false;
        }
    }

    true
}

/// Assess whether a callsite is affected by a change to a specific endpoint,
/// checking both an optional dynamic SpecRouteIndex and canonical routes.
pub fn assess_impact_with_index(
    callsite_kind: &CallsiteKind,
    matched_pattern: &str,
    changed_http_method: &str,
    changed_path: &str,
    spec_index: Option<&SpecRouteIndex>,
) -> MatchConfidence {
    match callsite_kind {
        CallsiteKind::UrlReference => {
            if matched_pattern.contains(changed_path) || url_matches_templated_path(matched_pattern, changed_path) {
                MatchConfidence::Confirmed
            } else {
                MatchConfidence::FalsePositive
            }
        }

        CallsiteKind::Import => MatchConfidence::Unresolved(
            UncertaintyReason::ImportReference,
            "Import statement only; cannot determine executed HTTP operation without callsite tracing.".into(),
        ),

        CallsiteKind::TypeReference => MatchConfidence::Unresolved(
            UncertaintyReason::TypeReference,
            "Type reference or interface declaration; does not execute an HTTP operation at runtime.".into(),
        ),

        CallsiteKind::MethodCall => {
            // First check dynamic spec index if provided
            let resolution = if let Some(index) = spec_index {
                index.resolve(matched_pattern).or_else(|| resolve_canonical_method(matched_pattern))
            } else {
                resolve_canonical_method(matched_pattern)
            };

            match resolution {
                Some(res) => {
                    if res.path == changed_path
                        && res.http_method.eq_ignore_ascii_case(changed_http_method)
                    {
                        MatchConfidence::Confirmed
                    } else {
                        MatchConfidence::FalsePositive
                    }
                }
                None => {
                    if matched_pattern.contains('[') || matched_pattern.contains('$') || matched_pattern.contains("getattr") {
                        MatchConfidence::Unresolved(
                            UncertaintyReason::DynamicMethodChain,
                            "Method invocation path is resolved dynamically via variable indexing or runtime property access.".into(),
                        )
                    } else if matched_pattern.contains("request") || matched_pattern.contains("call") || matched_pattern.contains("send") {
                        MatchConfidence::Unresolved(
                            UncertaintyReason::CustomWrapper,
                            "Custom wrapper function or generic transport layer obscures underlying HTTP endpoint.".into(),
                        )
                    } else {
                        MatchConfidence::Unresolved(
                            UncertaintyReason::MissingSpecMapping,
                            format!("Method pattern '{matched_pattern}' has no matching operation in OpenAPI specification or canonical routes."),
                        )
                    }
                }
            }
        }
    }
}

/// Backwards-compatible assess_impact using canonical routing tables.
pub fn assess_impact(
    callsite_kind: &CallsiteKind,
    matched_pattern: &str,
    changed_http_method: &str,
    changed_path: &str,
) -> MatchConfidence {
    assess_impact_with_index(
        callsite_kind,
        matched_pattern,
        changed_http_method,
        changed_path,
        None,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engines::ast::CallsiteKind;

    #[test]
    fn resolves_charges_create_to_correct_endpoint() {
        let r = resolve_stripe_method("charges.create").unwrap();
        assert_eq!(r.http_method, "POST");
        assert_eq!(r.path, "/v1/charges");
    }

    #[test]
    fn resolves_checkout_sessions_create_to_correct_endpoint() {
        let r = resolve_stripe_method("checkout.sessions.create").unwrap();
        assert_eq!(r.path, "/v1/checkout/sessions");
    }

    #[test]
    fn resolves_billing_portal_sessions_create() {
        let r = resolve_stripe_method("billingPortal.sessions.create").unwrap();
        assert_eq!(r.path, "/v1/billing_portal/sessions");
    }

    #[test]
    fn resolves_subscription_cancel_to_delete() {
        let r = resolve_stripe_method("subscriptions.cancel").unwrap();
        assert_eq!(r.http_method, "DELETE");
    }

    #[test]
    fn returns_none_for_unknown_chain() {
        assert!(resolve_stripe_method("something.unknown.method").is_none());
    }

    #[test]
    fn strips_await_stripe_prefix() {
        let r = resolve_stripe_method("await stripe.charges.create(").unwrap();
        assert_eq!(r.path, "/v1/charges");
    }

    #[test]
    fn strips_this_stripe_prefix() {
        let r = resolve_stripe_method("this.stripe.paymentIntents.create").unwrap();
        assert_eq!(r.path, "/v1/payment_intents");
    }

    #[test]
    fn method_call_to_charges_is_confirmed_for_charges_change() {
        let confidence = assess_impact(
            &CallsiteKind::MethodCall,
            "charges.create",
            "POST",
            "/v1/charges",
        );
        assert_eq!(confidence, MatchConfidence::Confirmed);
    }

    #[test]
    fn checkout_sessions_is_false_positive_for_charges_change() {
        let confidence = assess_impact(
            &CallsiteKind::MethodCall,
            "checkout.sessions.create",
            "POST",
            "/v1/charges",
        );
        assert_eq!(confidence, MatchConfidence::FalsePositive);
    }

    #[test]
    fn billing_portal_is_false_positive_for_charges_change() {
        let confidence = assess_impact(
            &CallsiteKind::MethodCall,
            "billingPortal.sessions.create",
            "POST",
            "/v1/charges",
        );
        assert_eq!(confidence, MatchConfidence::FalsePositive);
    }

    #[test]
    fn import_is_unresolvable() {
        let confidence = assess_impact(&CallsiteKind::Import, "stripe", "POST", "/v1/charges");
        assert!(confidence.is_unresolved());
        assert_eq!(
            confidence.uncertainty_reason(),
            Some(UncertaintyReason::ImportReference)
        );
    }

    #[test]
    fn url_reference_matches_correct_path() {
        let confidence = assess_impact(
            &CallsiteKind::UrlReference,
            "https://api.stripe.com/v1/charges",
            "POST",
            "/v1/charges",
        );
        assert_eq!(confidence, MatchConfidence::Confirmed);
    }

    #[test]
    fn url_reference_rejects_wrong_path() {
        let confidence = assess_impact(
            &CallsiteKind::UrlReference,
            "https://api.stripe.com/v1/customers",
            "POST",
            "/v1/charges",
        );
        assert_eq!(confidence, MatchConfidence::FalsePositive);
    }

    #[test]
    fn correctly_rejects_sixteen_false_positives() {
        let callsite_patterns = vec![
            (CallsiteKind::Import, "stripe"),
            (CallsiteKind::TypeReference, "Stripe"),
            (CallsiteKind::MethodCall, "checkout.sessions.create"),
            (CallsiteKind::MethodCall, "checkout.sessions.retrieve"),
            (CallsiteKind::MethodCall, "billingPortal.sessions.create"),
            (CallsiteKind::MethodCall, "subscriptions.create"),
            (CallsiteKind::MethodCall, "customers.create"),
            (CallsiteKind::MethodCall, "paymentIntents.create"),
            (CallsiteKind::MethodCall, "prices.list"),
            (CallsiteKind::MethodCall, "products.retrieve"),
            (CallsiteKind::MethodCall, "invoices.pay"),
            (CallsiteKind::MethodCall, "paymentMethods.attach"),
            (CallsiteKind::MethodCall, "webhooks.constructEvent"),
            (CallsiteKind::MethodCall, "events.list"),
            (CallsiteKind::MethodCall, "coupons.create"),
            (CallsiteKind::MethodCall, "promotionCodes.create"),
            // Only these should be confirmed:
            (CallsiteKind::MethodCall, "charges.create"),
            (CallsiteKind::MethodCall, "charges.retrieve"),
        ];

        let mut confirmed = 0;
        let mut false_positives = 0;
        let mut unresolvable = 0;

        for (kind, pattern) in &callsite_patterns {
            match assess_impact(kind, pattern, "POST", "/v1/charges") {
                MatchConfidence::Confirmed => confirmed += 1,
                MatchConfidence::FalsePositive => false_positives += 1,
                MatchConfidence::Unresolvable | MatchConfidence::Unresolved(..) => {
                    unresolvable += 1
                }
            }
        }

        assert_eq!(confirmed, 1, "only charges.create matches POST /v1/charges");
        assert_eq!(false_positives, 15, "15 SDK callsites correctly rejected (including charges.retrieve which is GET, not POST)");
        assert_eq!(unresolvable, 2, "import + type reference are unresolvable");
    }

    #[test]
    fn dynamic_spec_route_index_synthesizes_methods() {
        let mut index = SpecRouteIndex::new();
        index.index_endpoint("/v1/checkout/sessions", "POST");
        index.index_endpoint("/v1/charges/{id}", "GET");
        index.index_endpoint("/v1/subscriptions/{id}", "DELETE");
        index.index_endpoint("/v1/payment_intents/{id}/confirm", "POST");

        let r1 = index.resolve("checkout.sessions.create").unwrap();
        assert_eq!(r1.path, "/v1/checkout/sessions");
        assert_eq!(r1.http_method, "POST");

        let r2 = index.resolve("charges.retrieve").unwrap();
        assert_eq!(r2.path, "/v1/charges/{id}");
        assert_eq!(r2.http_method, "GET");

        let r3 = index.resolve("subscriptions.cancel").unwrap();
        assert_eq!(r3.path, "/v1/subscriptions/{id}");
        assert_eq!(r3.http_method, "DELETE");

        let r4 = index.resolve("paymentIntents.confirm").unwrap();
        assert_eq!(r4.path, "/v1/payment_intents/{id}/confirm");
        assert_eq!(r4.http_method, "POST");
    }

    #[test]
    fn resolves_openai_and_anthropic_canonical_methods() {
        let openai = resolve_canonical_method("openai.chat.completions.create").unwrap();
        assert_eq!(openai.path, "/v1/chat/completions");
        assert_eq!(openai.http_method, "POST");

        let anthropic = resolve_canonical_method("anthropic.messages.create").unwrap();
        assert_eq!(anthropic.path, "/v1/messages");
        assert_eq!(anthropic.http_method, "POST");
    }
}
