pub mod contracts;
pub mod inventory;
pub mod manifest_lockfile;
mod planner;
pub mod policy;
pub mod report;
pub mod resolver;
mod types;
pub mod workflow;

pub use contracts::synthesize_contract_tests;
pub use inventory::{render_inventory, run_inventory, DepHealth, DiscoveredDep, Inventory};
pub use manifest_lockfile::{
    detect_package_manager, parse_cargo_lock, parse_package_json_manifest, parse_package_lock_json,
    parse_pnpm_lock, parse_yarn_lock, resolve_dependency, PackageManager, ResolvedDependency,
};
pub use planner::plan_maintenance;
pub use policy::{SafetyDecision, SafetyPolicy};
pub use report::{render_markdown, render_trust_report_cli};
pub use types::{
    AffectedCallsite, ImpactState, ImpactedEndpoint, MaintenancePlan, PatchTarget, PlanStatus,
    UncertaintyReason, UnresolvedCallsite, VerificationOutcome, VerificationSpec,
};
pub use workflow::{execution_order, parse_workflow, validate_workflow, WorkflowDef};
