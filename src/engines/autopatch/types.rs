use crate::engines::ast::Callsite;
use crate::engines::schema::BreakingSeverity;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub enum PlanStatus {
    #[default]
    Clean,
    NoImpact,
    ActionRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AffectedCallsite {
    pub file_path: String,
    pub line_number: usize,
    pub line_content: String,
    pub matched_pattern: String,
}

impl From<&Callsite> for AffectedCallsite {
    fn from(c: &Callsite) -> Self {
        Self {
            file_path: c.file_path.clone(),
            line_number: c.line_number,
            line_content: c.line_content.clone(),
            matched_pattern: c.matched_pattern.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum UncertaintyReason {
    ImportReference,
    TypeReference,
    DynamicMethodChain,
    DynamicEndpointConstruction,
    CustomWrapper,
    GenericHttpClient,
    GeneratedCode,
    ReflectionOrMetaprogramming,
    MissingSpecMapping,
    AmbiguousOperation,
    RuntimeOnlyDependency,
    InsufficientEvidence,
    UnsupportedLanguageConstruct,
}

impl UncertaintyReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::ImportReference => "ImportReference",
            Self::TypeReference => "TypeReference",
            Self::DynamicMethodChain => "DynamicMethodChain",
            Self::DynamicEndpointConstruction => "DynamicEndpointConstruction",
            Self::CustomWrapper => "CustomWrapper",
            Self::GenericHttpClient => "GenericHttpClient",
            Self::GeneratedCode => "GeneratedCode",
            Self::ReflectionOrMetaprogramming => "ReflectionOrMetaprogramming",
            Self::MissingSpecMapping => "MissingSpecMapping",
            Self::AmbiguousOperation => "AmbiguousOperation",
            Self::RuntimeOnlyDependency => "RuntimeOnlyDependency",
            Self::InsufficientEvidence => "InsufficientEvidence",
            Self::UnsupportedLanguageConstruct => "UnsupportedLanguageConstruct",
        }
    }

    pub fn default_explanation(&self) -> &'static str {
        match self {
            Self::ImportReference => "Import statement only; cannot determine which operation is called without callsite tracing.",
            Self::TypeReference => "Type definition or annotation; does not execute an HTTP operation at runtime.",
            Self::DynamicMethodChain => "Method invoked dynamically via variable or property access; static resolution cannot guarantee target.",
            Self::DynamicEndpointConstruction => "HTTP URL or path constructed dynamically at runtime.",
            Self::CustomWrapper => "User-defined wrapper function obscures the underlying API operation.",
            Self::GenericHttpClient => "Generic HTTP client (fetch/axios/requests) without statically determinable API contract.",
            Self::GeneratedCode => "Code is machine-generated; manual changes may be overwritten.",
            Self::ReflectionOrMetaprogramming => "Metaprogramming or reflection prevents static AST resolution.",
            Self::MissingSpecMapping => "No matching operation found in upstream OpenAPI specification.",
            Self::AmbiguousOperation => "Callsite matches multiple potential API operations with conflicting semantics.",
            Self::RuntimeOnlyDependency => "Dependency resolved dynamically at runtime.",
            Self::InsufficientEvidence => "Evidence threshold not met to safely confirm or reject impact.",
            Self::UnsupportedLanguageConstruct => "Language construct cannot be analyzed by current AST engine.",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UnresolvedCallsite {
    pub reason: UncertaintyReason,
    pub file_path: String,
    pub line_number: usize,
    pub source_text: String,
    pub provider: String,
    pub inferred_operation: Option<String>,
    pub confidence_evidence: String,
    pub why_autofix_disabled: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ImpactState {
    ConfirmedAffected,
    ProvablyUnaffected,
    Unresolved,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImpactedEndpoint {
    pub path: String,
    pub method: String,
    pub change_summary: Vec<String>,
    pub severity: BreakingSeverity,
    pub affected_callsites: Vec<AffectedCallsite>,
    pub confirmed_count: usize,
    pub false_positive_count: usize,
    pub unresolvable_count: usize,
    pub total_sdk_references: usize,
    #[serde(default)]
    pub unresolved_callsites: Vec<UnresolvedCallsite>,
    #[serde(default)]
    pub provably_unaffected_callsites: Vec<AffectedCallsite>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatchTarget {
    pub file_path: String,
    pub line_numbers: Vec<usize>,
    pub reason: String,
    pub upstream_change: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerificationSpec {
    pub endpoint: String,
    pub method: String,
    pub fields_to_verify: Vec<String>,
}

///   2. Find every affected callsite in the codebase.
///   3. Know which files to patch and why.
///   4. Generate contract tests to verify correctness.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MaintenancePlan {
    pub status: PlanStatus,
    pub api_name: String,
    pub old_version: String,
    pub new_version: String,
    pub breaking_changes: usize,
    pub total_affected_files: usize,
    pub total_affected_callsites: usize,
    pub impacted_endpoints: Vec<ImpactedEndpoint>,
    pub patch_targets: Vec<PatchTarget>,
    pub verification_specs: Vec<VerificationSpec>,
}

/// Verification outcome of an automated patch or test trial.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum VerificationOutcome {
    Verified,
    BehavioralDriftDetected { reason: String },
    InsufficientEvidence { explanation: String },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uncertainty_reason_taxonomy_has_at_least_ten_variants() {
        let reasons = vec![
            UncertaintyReason::ImportReference,
            UncertaintyReason::TypeReference,
            UncertaintyReason::DynamicMethodChain,
            UncertaintyReason::DynamicEndpointConstruction,
            UncertaintyReason::CustomWrapper,
            UncertaintyReason::GenericHttpClient,
            UncertaintyReason::GeneratedCode,
            UncertaintyReason::ReflectionOrMetaprogramming,
            UncertaintyReason::MissingSpecMapping,
            UncertaintyReason::AmbiguousOperation,
            UncertaintyReason::RuntimeOnlyDependency,
            UncertaintyReason::InsufficientEvidence,
            UncertaintyReason::UnsupportedLanguageConstruct,
        ];
        assert!(reasons.len() >= 10);
        for r in &reasons {
            assert!(!r.as_str().is_empty());
        }
    }
}
