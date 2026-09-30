use napi_derive::napi;

#[napi]
pub fn version() -> String {
    truhowl_core::version().to_string()
}

#[napi]
pub fn sandbox_supported() -> bool {
    truhowl_core::sandbox::check_supported()
}

#[napi]
pub fn compress(content: String) -> String {
    truhowl_core::compress(&content)
}

fn parse<T: serde::de::DeserializeOwned>(input: &str) -> napi::Result<T> {
    serde_json::from_str(input).map_err(|e| napi::Error::from_reason(format!("invalid JSON: {e}")))
}

#[napi]
pub fn runtime_check_permission(policy_json: String, required_json: String) -> napi::Result<bool> {
    let policy: serde_json::Value = parse(&policy_json)?;
    let required: Vec<String> = parse(&required_json)?;
    Ok(truhowl_core::runtime::enforcer::check_permission_json(&policy, &required).is_ok())
}

#[napi]
pub fn runtime_check_command(cmd: String) -> napi::Result<bool> {
    Ok(truhowl_core::runtime::enforcer::check_command(&cmd).is_ok())
}

#[napi]
pub fn runtime_snapshot(
    workdir: String,
    snapshot_dir: String,
    exclude_json: Option<String>,
) -> napi::Result<i32> {
    let exclude: Vec<String> = match exclude_json {
        Some(raw) => parse(&raw)?,
        None => Vec::new(),
    };
    let set: std::collections::HashSet<String> = exclude.into_iter().collect();
    let mgr = truhowl_core::runtime::snapshot::SnapshotManager::new(
        &workdir,
        &snapshot_dir,
        if set.is_empty() { None } else { Some(set) },
    );
    Ok(mgr.snapshot().map_err(napi::Error::from_reason)? as i32)
}

#[napi]
pub fn runtime_restore(workdir: String, snapshot_dir: String) -> napi::Result<i32> {
    let mgr = truhowl_core::runtime::snapshot::SnapshotManager::new(&workdir, &snapshot_dir, None);
    Ok(mgr.restore().map_err(napi::Error::from_reason)? as i32)
}

#[napi]
pub fn runtime_validate(configs_json: String, edges_json: String) -> napi::Result<bool> {
    let edges: Vec<(String, String)> = match parse(&edges_json) {
        Ok(e) => e,
        Err(_) => return Ok(false),
    };
    let runtime = match build_runtime(&configs_json, &edges) {
        Ok(rt) => rt,
        Err(_) => return Ok(false),
    };
    Ok(runtime.run_order(None).is_ok())
}

#[napi]
pub fn runtime_can_route(configs_json: String, from: String, to: String) -> napi::Result<bool> {
    let runtime = build_runtime(&configs_json, &[])?;
    if runtime.config(&from).is_none() || runtime.config(&to).is_none() {
        return Err(napi::Error::from_reason("unknown source or target compartment"));
    }
    Ok(runtime.can_route(&from, &to).is_ok())
}

#[napi]
pub fn runtime_credential_rewrite(routes_json: String, path: String) -> napi::Result<Option<String>> {
    let routes: Vec<truhowl_core::runtime::credential::RouteConfig> = parse(&routes_json)?;
    for r in &routes {
        if r.matches(&path) {
            return Ok(Some(r.rewrite_path(&path)));
        }
    }
    Ok(None)
}

/// Resolve a credential source like `env:OPENAI_API_KEY`.
#[napi]
pub fn runtime_credential_resolve(source: String) -> napi::Result<String> {
    let route = truhowl_core::runtime::credential::RouteConfig {
        credential_source: source,
        ..Default::default()
    };
    Ok(route.resolve_credential())
}

fn build_runtime(configs: &str, edges: &[(String, String)]) -> napi::Result<truhowl_core::runtime::compartments::Runtime> {
    #[derive(serde::Deserialize)]
    struct Spec {
        #[serde(default)]
        configs: Vec<truhowl_core::runtime::compartments::CompartmentConfig>,
    }
    let spec: Spec = parse(configs)?;
    let mut rt = truhowl_core::runtime::compartments::Runtime::new();
    for cfg in spec.configs {
        rt.add(cfg).map_err(napi::Error::from_reason)?;
    }
    for (from, to) in edges {
        rt.edge(from, to).map_err(napi::Error::from_reason)?;
    }
    Ok(rt)
}

/// Opaque pre-built compartment runtime; not thread-safe.
#[napi]
pub struct Runtime {
    inner: truhowl_core::runtime::compartments::Runtime,
}

#[napi]
impl Runtime {
    #[napi(constructor)]
    pub fn new(configs_json: String, edges_json: String) -> napi::Result<Self> {
        let edges: Vec<(String, String)> = parse(&edges_json)?;
        Ok(Self {
            inner: build_runtime(&configs_json, &edges)?,
        })
    }

    #[napi]
    pub fn can_route(&self, from: String, to: String) -> napi::Result<bool> {
        if self.inner.config(&from).is_none() || self.inner.config(&to).is_none() {
            return Err(napi::Error::from_reason("unknown source or target compartment"));
        }
        Ok(self.inner.can_route(&from, &to).is_ok())
    }

    #[napi]
    pub fn run_order(&self, entry: Option<String>) -> napi::Result<Vec<String>> {
        self.inner
            .run_order(entry.as_deref())
            .map_err(napi::Error::from_reason)
    }

    #[napi]
    pub fn names(&self) -> Vec<String> {
        self.inner.names()
    }
}
