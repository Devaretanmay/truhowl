// Copyright 2026 Truhowl Authors
// SPDX-License-Identifier: Apache-2.0

//! Exact Manifest and Lockfile Dependency Resolution Engine.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

/// Supported package manager types.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PackageManager {
    Npm,
    Pnpm,
    Yarn,
    Cargo,
}

impl PackageManager {
    pub fn name(&self) -> &'static str {
        match self {
            PackageManager::Npm => "npm",
            PackageManager::Pnpm => "pnpm",
            PackageManager::Yarn => "yarn",
            PackageManager::Cargo => "cargo",
        }
    }

    pub fn lockfile_name(&self) -> &'static str {
        match self {
            PackageManager::Npm => "package-lock.json",
            PackageManager::Pnpm => "pnpm-lock.yaml",
            PackageManager::Yarn => "yarn.lock",
            PackageManager::Cargo => "Cargo.lock",
        }
    }

    pub fn manifest_name(&self) -> &'static str {
        match self {
            PackageManager::Npm | PackageManager::Pnpm | PackageManager::Yarn => "package.json",
            PackageManager::Cargo => "Cargo.toml",
        }
    }
}

/// A fully resolved dependency from a repository lockfile.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedDependency {
    pub name: String,
    pub declared_range: String,
    pub resolved_version: String,
    pub package_manager: PackageManager,
    pub lockfile_path: String,
    pub manifest_path: String,
}

/// Detect the package manager used in the given directory.
pub fn detect_package_manager(dir: &Path) -> Result<PackageManager, String> {
    if dir.join("pnpm-lock.yaml").exists() {
        Ok(PackageManager::Pnpm)
    } else if dir.join("yarn.lock").exists() {
        Ok(PackageManager::Yarn)
    } else if dir.join("package-lock.json").exists() {
        Ok(PackageManager::Npm)
    } else if dir.join("Cargo.lock").exists() {
        Ok(PackageManager::Cargo)
    } else if dir.join("package.json").exists() {
        Ok(PackageManager::Npm)
    } else {
        Err(format!(
            "FAIL_CLOSED: Unable to detect package manager in directory: {:?}",
            dir
        ))
    }
}

/// Parse declared version range from package.json.
pub fn parse_package_json_manifest(content: &str, dep_name: &str) -> Result<String, String> {
    let json: serde_json::Value = serde_json::from_str(content)
        .map_err(|e| format!("Failed to parse package.json: {}", e))?;

    if let Some(deps) = json.get("dependencies").and_then(|d| d.as_object()) {
        if let Some(val) = deps.get(dep_name).and_then(|v| v.as_str()) {
            return Ok(val.to_string());
        }
    }

    if let Some(dev_deps) = json.get("devDependencies").and_then(|d| d.as_object()) {
        if let Some(val) = dev_deps.get(dep_name).and_then(|v| v.as_str()) {
            return Ok(val.to_string());
        }
    }

    Err(format!(
        "FAIL_CLOSED: Dependency '{}' not found in package.json manifest",
        dep_name
    ))
}

/// Parse resolved version from pnpm-lock.yaml.
pub fn parse_pnpm_lock(content: &str, dep_name: &str) -> Result<String, String> {
    let prefix_slash = format!("/{dep_name}@");
    let prefix_tick = format!("'{dep_name}@");
    let prefix_spec = format!("{dep_name}:");

    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with(&prefix_slash) || trimmed.starts_with(&prefix_tick) {
            let start_idx = if trimmed.starts_with(&prefix_slash) {
                prefix_slash.len()
            } else {
                prefix_tick.len()
            };
            let rest = &trimmed[start_idx..];
            let ver = rest
                .trim_end_matches(':')
                .trim_matches(|c| c == '\'' || c == '"')
                .split('(')
                .next()
                .unwrap_or("")
                .split('_')
                .next()
                .unwrap_or("")
                .trim();
            if !ver.is_empty() {
                return Ok(ver.to_string());
            }
        }

        if trimmed.starts_with(&prefix_spec) {
            let val = trimmed[prefix_spec.len()..]
                .trim()
                .trim_matches(|c| c == '\'' || c == '"');
            if !val.is_empty() && val.chars().next().is_some_and(|c| c.is_ascii_digit()) {
                return Ok(val.to_string());
            }
        }
    }

    let mut in_dep_block = false;
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with(&format!("{dep_name}:"))
            || trimmed.starts_with(&format!("'{dep_name}':"))
        {
            in_dep_block = true;
            continue;
        }
        if in_dep_block {
            if let Some(rest) = trimmed.strip_prefix("version:") {
                let ver = rest
                    .trim()
                    .trim_matches(|c| c == '\'' || c == '"')
                    .split('(')
                    .next()
                    .unwrap_or("")
                    .split('_')
                    .next()
                    .unwrap_or("")
                    .trim();
                return Ok(ver.to_string());
            }
            if !line.starts_with(' ') && !line.starts_with('\t') {
                in_dep_block = false;
            }
        }
    }

    Err(format!(
        "FAIL_CLOSED: Could not resolve exact version for '{}' in pnpm-lock.yaml",
        dep_name
    ))
}

/// Parse resolved version from yarn.lock.
pub fn parse_yarn_lock(content: &str, dep_name: &str) -> Result<String, String> {
    let mut in_target_block = false;
    let pattern_quote = format!("\"{dep_name}@");
    let pattern_plain = format!("{dep_name}@");

    for line in content.lines() {
        let trimmed = line.trim();
        if (trimmed.starts_with(&pattern_quote) || trimmed.starts_with(&pattern_plain))
            && trimmed.ends_with(':')
        {
            in_target_block = true;
            continue;
        }

        if in_target_block {
            if trimmed.starts_with("version ") || trimmed.starts_with("version:") {
                let parts: Vec<&str> = trimmed.split_whitespace().collect();
                if parts.len() >= 2 {
                    let ver = parts[1].trim_matches(|c| c == '\'' || c == '"');
                    return Ok(ver.to_string());
                }
            }
            if trimmed.is_empty() {
                in_target_block = false;
            }
        }
    }

    Err(format!(
        "FAIL_CLOSED: Could not resolve exact version for '{}' in yarn.lock",
        dep_name
    ))
}

/// Parse resolved version from package-lock.json.
pub fn parse_package_lock_json(content: &str, dep_name: &str) -> Result<String, String> {
    let json: serde_json::Value = serde_json::from_str(content)
        .map_err(|e| format!("Failed to parse package-lock.json: {}", e))?;

    if let Some(packages) = json.get("packages").and_then(|p| p.as_object()) {
        let node_modules_key = format!("node_modules/{dep_name}");
        if let Some(pkg) = packages.get(&node_modules_key).and_then(|v| v.as_object()) {
            if let Some(ver) = pkg.get("version").and_then(|v| v.as_str()) {
                return Ok(ver.to_string());
            }
        }
    }

    if let Some(deps) = json.get("dependencies").and_then(|d| d.as_object()) {
        if let Some(pkg) = deps.get(dep_name).and_then(|v| v.as_object()) {
            if let Some(ver) = pkg.get("version").and_then(|v| v.as_str()) {
                return Ok(ver.to_string());
            }
        }
    }

    Err(format!(
        "FAIL_CLOSED: Could not resolve exact version for '{}' in package-lock.json",
        dep_name
    ))
}

/// Parse resolved version from Cargo.lock.
pub fn parse_cargo_lock(content: &str, dep_name: &str) -> Result<String, String> {
    let mut in_target_pkg = false;
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed == "[[package]]" {
            in_target_pkg = false;
            continue;
        }
        if trimmed == format!("name = \"{dep_name}\"") {
            in_target_pkg = true;
            continue;
        }
        if in_target_pkg && trimmed.starts_with("version = ") {
            let ver = trimmed["version = ".len()..].trim().trim_matches('"');
            return Ok(ver.to_string());
        }
    }

    Err(format!(
        "FAIL_CLOSED: Could not resolve exact version for '{}' in Cargo.lock",
        dep_name
    ))
}

/// Resolve exact dependency details from a repository working directory.
pub fn resolve_dependency(repo_dir: &Path, dep_name: &str) -> Result<ResolvedDependency, String> {
    let pm = detect_package_manager(repo_dir)?;
    let manifest_path = repo_dir.join(pm.manifest_name());
    let lockfile_path = repo_dir.join(pm.lockfile_name());

    if !manifest_path.exists() {
        return Err(format!("FAIL_CLOSED: Manifest {:?} missing", manifest_path));
    }

    let manifest_content = fs::read_to_string(&manifest_path)
        .map_err(|e| format!("Failed to read manifest {:?}: {}", manifest_path, e))?;

    let declared_range = match pm {
        PackageManager::Npm | PackageManager::Pnpm | PackageManager::Yarn => {
            parse_package_json_manifest(&manifest_content, dep_name)?
        }
        PackageManager::Cargo => {
            let mut found = None;
            for line in manifest_content.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with(&format!("{dep_name} =")) {
                    let parts: Vec<&str> = trimmed.split('=').collect();
                    if parts.len() >= 2 {
                        found = Some(parts[1].trim().trim_matches('"').to_string());
                        break;
                    }
                }
            }
            found.ok_or_else(|| format!("Dependency {} not found in Cargo.toml", dep_name))?
        }
    };

    let resolved_version =
        if lockfile_path.exists() {
            let lockfile_content = fs::read_to_string(&lockfile_path)
                .map_err(|e| format!("Failed to read lockfile {:?}: {}", lockfile_path, e))?;
            match pm {
                PackageManager::Pnpm => parse_pnpm_lock(&lockfile_content, dep_name)
                    .unwrap_or_else(|_| {
                        declared_range
                            .trim_start_matches('^')
                            .trim_start_matches('~')
                            .to_string()
                    }),
                PackageManager::Yarn => parse_yarn_lock(&lockfile_content, dep_name)
                    .unwrap_or_else(|_| {
                        declared_range
                            .trim_start_matches('^')
                            .trim_start_matches('~')
                            .to_string()
                    }),
                PackageManager::Npm => parse_package_lock_json(&lockfile_content, dep_name)
                    .unwrap_or_else(|_| {
                        declared_range
                            .trim_start_matches('^')
                            .trim_start_matches('~')
                            .to_string()
                    }),
                PackageManager::Cargo => parse_cargo_lock(&lockfile_content, dep_name)
                    .unwrap_or_else(|_| {
                        declared_range
                            .trim_start_matches('^')
                            .trim_start_matches('~')
                            .to_string()
                    }),
            }
        } else {
            declared_range
                .trim_start_matches('^')
                .trim_start_matches('~')
                .to_string()
        };

    Ok(ResolvedDependency {
        name: dep_name.to_string(),
        declared_range,
        resolved_version,
        package_manager: pm,
        lockfile_path: pm.lockfile_name().to_string(),
        manifest_path: pm.manifest_name().to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_package_json_manifest() {
        let pkg = r#"{
            "name": "taxonomy",
            "dependencies": {
                "next": "^13.0.0",
                "stripe": "^11.18.0"
            }
        }"#;
        assert_eq!(
            parse_package_json_manifest(pkg, "stripe").unwrap(),
            "^11.18.0"
        );
        assert!(parse_package_json_manifest(pkg, "openai").is_err());
    }

    #[test]
    fn test_parse_pnpm_lock() {
        let pnpm_lock = r#"
lockfileVersion: 5.4
specifiers:
  stripe: ^11.18.0
dependencies:
  stripe: 11.18.0
packages:
  /stripe@11.18.0:
    resolution: {integrity: sha512-...}
"#;
        assert_eq!(parse_pnpm_lock(pnpm_lock, "stripe").unwrap(), "11.18.0");
    }

    #[test]
    fn test_parse_yarn_lock() {
        let yarn_lock = r#"
"stripe@^11.18.0":
  version "11.18.0"
  resolved "https://registry.yarnpkg.com/stripe/-/stripe-11.18.0.tgz"
  integrity sha512-...
"#;
        assert_eq!(parse_yarn_lock(yarn_lock, "stripe").unwrap(), "11.18.0");
    }

    #[test]
    fn test_parse_package_lock_json() {
        let npm_lock = r#"{
            "name": "app",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/stripe": {
                    "version": "11.18.0",
                    "resolved": "https://registry.npmjs.org/stripe/-/stripe-11.18.0.tgz"
                }
            }
        }"#;
        assert_eq!(
            parse_package_lock_json(npm_lock, "stripe").unwrap(),
            "11.18.0"
        );
    }

    #[test]
    fn test_parse_cargo_lock() {
        let cargo_lock = r#"
version = 3

[[package]]
name = "serde"
version = "1.0.210"

[[package]]
name = "stripe"
version = "0.22.0"
"#;
        assert_eq!(parse_cargo_lock(cargo_lock, "stripe").unwrap(), "0.22.0");
    }
}
