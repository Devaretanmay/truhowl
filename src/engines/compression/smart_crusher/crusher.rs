use std::sync::Arc;

use serde_json::Value;

use super::analyzer::SmartAnalyzer;
use super::classifier::{classify_array, ArrayType};
use super::compaction::{
    classify_cell, emit_opaque_ccr_marker, try_parse_json_container, CellClass, ClassifyConfig,
    CompactConfig, Compaction, CompactionStage, OpaqueKind,
};
use super::SmartCrusherConfig;

use super::crushers::{compute_k_split, crush_number_array, crush_object, crush_string_array};
use super::planning::SmartCrusherPlanner;

use super::types::{CompressionPlan, CompressionStrategy, CrushResult};
use crate::engines::compression::adaptive_sizer::compute_optimal_k;
use crate::engines::compression::anchor_selector::AnchorConfig;
use crate::engines::compression::anchor_selector::AnchorSelector;
use crate::engines::compression::bm25::BM25Scorer;
use crate::runtime::ccr::InMemoryCcrStore;

#[allow(dead_code)]
pub struct CrushArrayResult {
    pub items: Vec<Value>,
    pub strategy_info: String,
    pub ccr_hash: Option<String>,
    pub dropped_summary: String,
    pub compacted: Option<String>,
    pub compaction_kind: Option<&'static str>,
}

pub struct SmartCrusher {
    pub config: SmartCrusherConfig,
    pub anchor_selector: AnchorSelector,
    pub scorer: BM25Scorer,
    pub analyzer: SmartAnalyzer,

    pub compaction: Option<CompactionStage>,
    pub ccr_store: Option<Arc<InMemoryCcrStore>>,
}

impl SmartCrusher {
    pub fn new(config: SmartCrusherConfig) -> Self {
        let compact_cfg = CompactConfig {
            core_field_fraction: config.compaction_core_field_fraction,
            heterogeneous_core_ratio: config.compaction_heterogeneous_core_ratio,
            max_flatten_inner_keys: config.compaction_max_flatten_inner_keys,
            min_buckets: config.compaction_min_buckets,
            max_buckets: config.compaction_max_buckets,
            classify: ClassifyConfig {
                emit_opaque_markers: config.enable_ccr_marker && !config.lossless_only,
                ..ClassifyConfig::default()
            },
            ..CompactConfig::default()
        };
        let anchor_selector = AnchorSelector::new(AnchorConfig::default());
        let scorer = BM25Scorer::default();
        let analyzer = SmartAnalyzer::new(config.clone());
        let compaction = Some(CompactionStage::csv_schema(compact_cfg));
        let ccr_store: Option<Arc<InMemoryCcrStore>> = Some(Arc::new(InMemoryCcrStore::new()));
        SmartCrusher {
            config,
            anchor_selector,
            scorer,
            analyzer,
            compaction,
            ccr_store,
        }
    }

    #[allow(dead_code)]
    pub fn without_compaction(config: SmartCrusherConfig) -> Self {
        let anchor_selector = AnchorSelector::new(AnchorConfig::default());
        let scorer = BM25Scorer::default();
        let analyzer = SmartAnalyzer::new(config.clone());
        let ccr_store: Option<Arc<InMemoryCcrStore>> = Some(Arc::new(InMemoryCcrStore::new()));
        SmartCrusher {
            config,
            anchor_selector,
            scorer,
            analyzer,
            compaction: None,
            ccr_store,
        }
    }

    #[allow(dead_code)]
    pub fn with_scorer(config: SmartCrusherConfig, scorer: BM25Scorer) -> Self {
        let anchor_selector = AnchorSelector::new(AnchorConfig::default());
        let analyzer = SmartAnalyzer::new(config.clone());
        let ccr_store: Option<Arc<InMemoryCcrStore>> = Some(Arc::new(InMemoryCcrStore::new()));
        SmartCrusher {
            config,
            anchor_selector,
            scorer,
            analyzer,
            compaction: None,
            ccr_store,
        }
    }

    #[allow(dead_code)]
    pub fn ccr_store(&self) -> Option<&Arc<InMemoryCcrStore>> {
        self.ccr_store.as_ref()
    }

    fn planner(&self) -> SmartCrusherPlanner<'_> {
        SmartCrusherPlanner::new(
            &self.config,
            &self.anchor_selector,
            &self.scorer,
            &self.analyzer,
        )
    }

    pub fn execute_plan(&self, plan: &CompressionPlan, items: &[Value]) -> Vec<Value> {
        let mut indices = plan.keep_indices.clone();
        indices.sort_unstable();
        let mut kept: Vec<Value> = indices
            .into_iter()
            .filter(|&idx| idx < items.len())
            .map(|idx| items[idx].clone())
            .collect();

        if self.config.factor_out_constants && !plan.constant_fields.is_empty() && kept.len() >= 2 {
            let mut any_stripped = false;
            for item in kept.iter_mut() {
                if let Value::Object(map) = item {
                    for (key, constant) in &plan.constant_fields {
                        if map.get(key) == Some(constant) {
                            map.remove(key);
                            any_stripped = true;
                        }
                    }
                }
            }
            if any_stripped {
                let mut sentinel = serde_json::Map::new();
                sentinel.insert(
                    "_constant_fields".to_string(),
                    Value::Object(plan.constant_fields.clone().into_iter().collect()),
                );
                kept.insert(0, Value::Object(sentinel));
            }
        }

        kept
    }

    pub fn crush(&self, content: &str, query: &str, bias: f64) -> CrushResult {
        let (compressed, was_modified, info) = self.smart_crush_content(content, query, bias);
        let strategy = if info.is_empty() {
            "passthrough".to_string()
        } else {
            info
        };

        CrushResult {
            compressed,
            original: content.to_string(),
            was_modified,
            strategy,
        }
    }

    pub fn smart_crush_content(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String) {
        let Ok(parsed) = serde_json::from_str::<Value>(content) else {
            return (content.to_string(), false, String::new());
        };

        let (crushed, info) = self.process_value(&parsed, 0, query_context, bias);

        let result = serde_json::to_string(&crushed).unwrap();
        let was_modified = result != content.trim();
        (result, was_modified, info)
    }

    const MAX_PROCESS_DEPTH: usize = 50;

    pub fn process_value(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        if depth >= Self::MAX_PROCESS_DEPTH {
            return (value.clone(), String::new());
        }

        let mut info_parts: Vec<String> = Vec::new();

        match value {
            Value::Array(arr) => {
                let n = arr.len();
                if n >= self.config.min_items_to_analyze {
                    let arr_type = classify_array(arr);
                    match arr_type {
                        ArrayType::DictArray => {
                            let result = self.crush_array(arr, query_context, bias);
                            if let Some(rendered) = result.compacted {
                                info_parts.push(format!(
                                    "{}({}->len={})",
                                    result.strategy_info,
                                    n,
                                    rendered.len()
                                ));
                                return (Value::String(rendered), info_parts.join(","));
                            }
                            info_parts.push(format!(
                                "{}({}->{})",
                                result.strategy_info,
                                n,
                                result.items.len()
                            ));
                            let mut items = result.items;
                            if !result.dropped_summary.is_empty() {
                                let mut sentinel = serde_json::Map::new();
                                sentinel.insert(
                                    "_ccr_dropped".to_string(),
                                    Value::String(result.dropped_summary),
                                );
                                items.push(Value::Object(sentinel));
                            }
                            return (Value::Array(items), info_parts.join(","));
                        }
                        ArrayType::StringArray => {
                            let strs: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                            let (crushed, strategy) = crush_string_array(&strs, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let crushed_values: Vec<Value> =
                                crushed.into_iter().map(Value::String).collect();
                            return (Value::Array(crushed_values), info_parts.join(","));
                        }
                        ArrayType::NumberArray => {
                            let (crushed, strategy) = crush_number_array(arr, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        ArrayType::MixedArray => {
                            let (crushed, strategy) =
                                self.crush_mixed_array(arr, query_context, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        _ => {}
                    }
                }

                let mut processed: Vec<Value> = Vec::with_capacity(n);
                for item in arr {
                    let (p_item, p_info) = self.process_value(item, depth + 1, query_context, bias);
                    processed.push(p_item);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }
                (Value::Array(processed), info_parts.join(","))
            }
            Value::Object(map) => {
                let mut processed = serde_json::Map::new();
                for (k, v) in map {
                    let (p_val, p_info) = self.process_value(v, depth + 1, query_context, bias);
                    processed.insert(k.clone(), p_val);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }

                if processed.len() >= self.config.min_items_to_analyze {
                    let (crushed_dict, strategy) = crush_object(&processed, &self.config, bias);
                    if strategy != "object:passthrough" {
                        info_parts.push(strategy);
                        return (Value::Object(crushed_dict), info_parts.join(","));
                    }
                }

                (Value::Object(processed), info_parts.join(","))
            }
            Value::String(s) => self.process_string(s, depth, query_context, bias),
            _ => (value.clone(), String::new()),
        }
    }

    fn process_string(
        &self,
        s: &str,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        if let Some(parsed) = try_parse_json_container(s) {
            let (processed, sub_info) = self.process_value(&parsed, depth + 1, query_context, bias);
            if processed != parsed {
                let rendered = match &processed {
                    Value::String(rendered_str) => rendered_str.clone(),
                    _ => serde_json::to_string(&processed).unwrap_or_else(|_| s.to_string()),
                };
                let info = if sub_info.is_empty() {
                    "string_json".to_string()
                } else {
                    format!("string_json[{sub_info}]")
                };
                return (Value::String(rendered), info);
            }
        }

        let cfg = ClassifyConfig {
            emit_opaque_markers: self.config.enable_ccr_marker && !self.config.lossless_only,
            ..ClassifyConfig::default()
        };
        if let CellClass::Opaque(kind) = classify_cell(&Value::String(s.to_string()), &cfg) {
            let marker = emit_opaque_ccr_marker(s, &kind, self.ccr_store.as_ref());
            let kind_label = opaque_kind_label(&kind);
            return (Value::String(marker), format!("string_ccr:{kind_label}"));
        }

        (Value::String(s.to_string()), String::new())
    }

    pub fn crush_array(&self, items: &[Value], query_context: &str, bias: f64) -> CrushArrayResult {
        let item_strings: Vec<String> = items
            .iter()
            .map(|i| serde_json::to_string(i).unwrap_or_default())
            .collect();
        let item_str_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();

        let max_k = if self.config.max_items_after_crush > 0 {
            Some(self.config.max_items_after_crush)
        } else {
            None
        };
        let adaptive_k = compute_optimal_k(&item_str_refs, bias, 3, max_k);

        if items.len() <= adaptive_k {
            return CrushArrayResult {
                items: items.to_vec(),
                strategy_info: "none:adaptive_at_limit".to_string(),
                ccr_hash: None,
                dropped_summary: String::new(),
                compacted: None,
                compaction_kind: None,
            };
        }

        if self.config.preview_count > 0 && items.len() > self.config.preview_count {
            let preview: Vec<Value> = items
                .iter()
                .take(self.config.preview_count)
                .cloned()
                .collect();
            let _dropped_count = items.len() - self.config.preview_count;
            let canonical = canonical_array_json(items);
            let h = hash_canonical(&canonical);
            let marker = serde_json::to_string(&serde_json::json!({
                "_truhowl_pager": {
                    "hash": h,
                    "total_items": items.len(),
                    "valid_indices": format!("{}..{}", self.config.preview_count, items.len().saturating_sub(1)),
                    "instruction": format!("Call truhowl_expand('{}', index) to retrieve specific rows", h)
                }
            })).unwrap_or_default();
            if let Some(store) = &self.ccr_store {
                store.put_with_version(&h, &canonical, 1);
            }
            return CrushArrayResult {
                items: preview,
                strategy_info: format!("preview:{}->{}", items.len(), self.config.preview_count),
                ccr_hash: Some(h),
                dropped_summary: marker,
                compacted: None,
                compaction_kind: None,
            };
        }

        if let Some(stage) = &self.compaction {
            let (c, rendered) = stage.run_with_store(items, self.ccr_store.as_ref());
            if c.was_compacted() {
                let input_bytes = estimate_array_bytes(&item_strings);
                let savings_ratio = if input_bytes > 0 {
                    1.0 - (rendered.len() as f64 / input_bytes as f64)
                } else {
                    0.0
                };
                if savings_ratio >= self.config.lossless_min_savings_ratio {
                    let kind = compaction_kind_str(&c);
                    return CrushArrayResult {
                        items: items.to_vec(),
                        strategy_info: format!("lossless:{kind}"),
                        ccr_hash: None,
                        dropped_summary: String::new(),
                        compacted: Some(rendered),
                        compaction_kind: Some(kind),
                    };
                }
            }
        }

        if self.config.lossless_only {
            return CrushArrayResult {
                items: items.to_vec(),
                strategy_info: "lossless_only:uncompacted".to_string(),
                ccr_hash: None,
                dropped_summary: String::new(),
                compacted: None,
                compaction_kind: None,
            };
        }

        debug_assert!(
            !self.config.lossless_only,
            "lossy path reached under lossless_only - the early return \
             above must keep this codepath (and its CCR store write) \
             unreachable in strict lossless mode",
        );

        let effective_max_items = adaptive_k;
        let analysis = self.analyzer.analyze_array(items);

        if analysis.recommended_strategy == CompressionStrategy::Skip {
            let reason = match &analysis.crushability {
                Some(c) => format!("skip:{}", c.reason),
                None => String::new(),
            };
            return CrushArrayResult {
                items: items.to_vec(),
                strategy_info: reason,
                ccr_hash: None,
                dropped_summary: String::new(),
                compacted: None,
                compaction_kind: None,
            };
        }

        let plan = self.planner().create_plan(
            &analysis,
            items,
            query_context,
            None,
            Some(effective_max_items),
            Some(&item_strings),
        );
        let result = self.execute_plan(&plan, items);

        let dropped_count = items.len().saturating_sub(result.len());
        let (ccr_hash, dropped_summary) = if dropped_count > 0 && self.config.enable_ccr_marker {
            let canonical = canonical_array_json(items);
            let h = hash_canonical(&canonical);
            let marker = serde_json::to_string(&serde_json::json!({
                "_truhowl_pager": {
                    "hash": h,
                    "total_items": items.len(),
                    "valid_indices": format!("{}..{}", result.len(), items.len().saturating_sub(1)),
                    "instruction": format!("Call truhowl_expand('{}', index) to retrieve specific rows", h)
                }
            })).unwrap_or_default();
            if let Some(store) = &self.ccr_store {
                store.put(&h, &canonical);
            }
            (Some(h), marker)
        } else {
            (None, String::new())
        };

        CrushArrayResult {
            items: result,
            strategy_info: analysis.recommended_strategy.as_str().to_string(),
            ccr_hash,
            dropped_summary,
            compacted: None,
            compaction_kind: None,
        }
    }

    pub fn crush_mixed_array(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
    ) -> (Vec<Value>, String) {
        let n = items.len();
        if n <= 8 {
            return (items.to_vec(), "mixed:passthrough".to_string());
        }

        let mut groups: GroupBuckets = GroupBuckets::default();
        for (i, item) in items.iter().enumerate() {
            groups.push(group_key(item), i, item.clone());
        }

        let mut keep_indices: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        let mut strategy_parts: Vec<String> = Vec::new();

        for (type_key, indices, values) in groups.into_iter() {
            if values.len() < self.config.min_items_to_analyze {
                keep_indices.extend(&indices);
                continue;
            }

            match type_key {
                "dict" => {
                    let CrushArrayResult { items: crushed, .. } =
                        self.crush_array(&values, query_context, bias);
                    let crushed_keys: std::collections::HashSet<String> =
                        crushed.iter().map(canonical_json_for_match).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if crushed_keys.contains(&canonical_json_for_match(&values[i])) {
                            keep_indices.insert(*idx);
                        }
                    }
                    strategy_parts.push(format!("dict:{}->{}", values.len(), crushed.len()));
                }
                "str" => {
                    let strs: Vec<&str> = values.iter().filter_map(|v| v.as_str()).collect();
                    let (crushed, _) = crush_string_array(&strs, &self.config, bias);
                    let crushed_set: std::collections::HashSet<&str> =
                        crushed.iter().map(|s| s.as_str()).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if let Some(s) = values[i].as_str() {
                            if crushed_set.contains(s) {
                                keep_indices.insert(*idx);
                            }
                        }
                    }
                    strategy_parts.push(format!("str:{}->{}", values.len(), crushed.len()));
                }
                "number" => {
                    let item_strings: Vec<String> = values.iter().map(|v| v.to_string()).collect();
                    let item_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();
                    let (_kt, kf, kl, _) = compute_k_split(&item_refs, &self.config, bias);

                    let kf = kf.min(values.len());
                    let kl = kl.min(values.len().saturating_sub(kf));
                    let first_idx: Vec<usize> = indices.iter().take(kf).copied().collect();
                    let last_idx: Vec<usize> =
                        indices.iter().rev().take(kl).copied().collect::<Vec<_>>();
                    keep_indices.extend(&first_idx);
                    keep_indices.extend(&last_idx);

                    let finite: Vec<f64> = values
                        .iter()
                        .filter_map(|v| v.as_f64().filter(|f| f.is_finite()))
                        .collect();
                    if finite.len() > 1 {
                        if let Some(mean_v) = super::stats_math::mean(&finite) {
                            if let Some(std_v) = super::stats_math::sample_stdev(&finite) {
                                if std_v > 0.0 {
                                    let threshold = self.config.variance_threshold * std_v;
                                    for (i, val) in values.iter().enumerate() {
                                        if let Some(num) = val.as_f64().filter(|f| f.is_finite()) {
                                            if (num - mean_v).abs() > threshold {
                                                keep_indices.insert(indices[i]);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    strategy_parts.push(format!("num:{}", values.len()));
                }
                _ => {
                    keep_indices.extend(&indices);
                }
            }
        }

        let result: Vec<Value> = keep_indices.iter().map(|&i| items[i].clone()).collect();
        let strategy = format!(
            "mixed:adaptive({}->{},{})",
            n,
            result.len(),
            strategy_parts.join(",")
        );
        (result, strategy)
    }
}

fn group_key(item: &Value) -> &'static str {
    match item {
        Value::Object(_) => "dict",
        Value::String(_) => "str",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::Array(_) => "list",
        Value::Null => "none",
    }
}

#[derive(Default)]
struct GroupBuckets {
    entries: Vec<(&'static str, Vec<usize>, Vec<Value>)>,
    index_of: std::collections::HashMap<&'static str, usize>,
}

impl GroupBuckets {
    fn push(&mut self, key: &'static str, idx: usize, value: Value) {
        match self.index_of.get(key).copied() {
            Some(i) => {
                self.entries[i].1.push(idx);
                self.entries[i].2.push(value);
            }
            None => {
                self.index_of.insert(key, self.entries.len());
                self.entries.push((key, vec![idx], vec![value]));
            }
        }
    }
}

impl IntoIterator for GroupBuckets {
    type Item = (&'static str, Vec<usize>, Vec<Value>);
    type IntoIter = std::vec::IntoIter<Self::Item>;
    fn into_iter(self) -> Self::IntoIter {
        self.entries.into_iter()
    }
}

fn canonical_json_for_match(value: &Value) -> String {
    crate::engines::compression::anchor_selector::python_json_dumps_sort_keys(value)
}

fn compaction_kind_str(c: &Compaction) -> &'static str {
    match c {
        Compaction::Table { .. } => "table",
        Compaction::Buckets { .. } => "buckets",
        Compaction::OpaqueRef { .. } => "ccr",
        Compaction::Untouched(_) => "untouched",
    }
}

fn estimate_array_bytes(item_strings: &[String]) -> usize {
    let payload: usize = item_strings.iter().map(|s| s.len()).sum();
    let separators = item_strings.len().saturating_sub(1);
    payload + separators + 2
}

fn canonical_array_json(items: &[Value]) -> String {
    serde_json::to_string(items).unwrap_or_default()
}
fn hash_canonical(canonical: &str) -> String {
    let h = blake3::hash(canonical.as_bytes());
    h.to_hex().as_str()[..12].to_string()
}

fn opaque_kind_label(kind: &OpaqueKind) -> &str {
    match kind {
        OpaqueKind::Base64Blob => "base64",
        OpaqueKind::LongString => "string",
        OpaqueKind::HtmlChunk => "html",
        OpaqueKind::Other(s) => s.as_str(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engines::compression::bm25::BM25Scorer;
    use crate::runtime::ccr::InMemoryCcrStore;
    use serde_json::json;
    use std::sync::Arc;

    fn crusher() -> SmartCrusher {
        SmartCrusher::new(SmartCrusherConfig::default())
    }

    #[test]
    fn execute_plan_empty_indices_returns_empty() {
        let c = crusher();
        let plan = CompressionPlan::default();
        let items: Vec<Value> = (0..5).map(|i| json!({"id": i})).collect();
        let result = c.execute_plan(&plan, &items);
        assert!(result.is_empty());
    }

    #[test]
    fn execute_plan_returns_items_in_sorted_index_order() {
        let c = crusher();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let plan = CompressionPlan {
            keep_indices: vec![5, 2, 8, 0],
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 4);
        assert_eq!(result[0]["id"], 0);
        assert_eq!(result[1]["id"], 2);
        assert_eq!(result[2]["id"], 5);
        assert_eq!(result[3]["id"], 8);
    }

    #[test]
    fn execute_plan_skips_out_of_bounds() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let plan = CompressionPlan {
            keep_indices: vec![0, 5, 2],
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn execute_plan_factors_constants_when_enabled() {
        let cfg = SmartCrusherConfig {
            factor_out_constants: true,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "region": "us-west-2", "status": "ok"}))
            .collect();
        let mut constant_fields = std::collections::BTreeMap::new();
        constant_fields.insert("region".to_string(), json!("us-west-2"));
        constant_fields.insert("status".to_string(), json!("ok"));
        let plan = CompressionPlan {
            keep_indices: vec![0, 1, 2],
            constant_fields,
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 4);
        assert_eq!(result[0]["_constant_fields"]["region"], "us-west-2");
        assert_eq!(result[0]["_constant_fields"]["status"], "ok");
        for item in &result[1..] {
            assert!(item.get("region").is_none());
            assert!(item.get("status").is_none());
            assert!(item.get("id").is_some());
        }
    }

    #[test]
    fn execute_plan_keeps_drifted_values_when_factoring() {
        let cfg = SmartCrusherConfig {
            factor_out_constants: true,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items = vec![
            json!({"id": 0, "status": "ok"}),
            json!({"id": 1, "status": "FAILED"}),
        ];
        let mut constant_fields = std::collections::BTreeMap::new();
        constant_fields.insert("status".to_string(), json!("ok"));
        let plan = CompressionPlan {
            keep_indices: vec![0, 1],
            constant_fields,
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 3);
        assert!(result[1].get("status").is_none());
        assert_eq!(result[2]["status"], "FAILED");
    }

    #[test]
    fn execute_plan_default_off_leaves_items_unchanged() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i, "k": "v"})).collect();
        let mut constant_fields = std::collections::BTreeMap::new();
        constant_fields.insert("k".to_string(), json!("v"));
        let plan = CompressionPlan {
            keep_indices: vec![0, 1, 2],
            constant_fields,
            ..CompressionPlan::default()
        };
        let result = c.execute_plan(&plan, &items);
        assert_eq!(result.len(), 3);
        assert_eq!(result[0]["k"], "v");
    }

    #[test]
    fn crush_array_passthrough_when_below_adaptive_k() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 3);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
    }

    #[test]
    fn crush_array_skip_path_returns_original_items() {
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 30);
        assert!(
            result.strategy_info.starts_with("skip:"),
            "expected skip:..., got {}",
            result.strategy_info
        );
    }

    #[test]
    fn crush_array_low_uniqueness_compresses() {
        let c = crusher();
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.items.len() <= 30, "should not exceed original count");
    }

    #[test]
    fn crush_array_keeps_error_items() {
        let c = crusher();
        let mut items: Vec<Value> = (0..30).map(|i| json!({"id": i, "status": "ok"})).collect();
        items.push(json!({"id": 30, "status": "error", "msg": "FATAL"}));
        let result = c.crush_array(&items, "", 1.0);
        assert!(
            result
                .items
                .iter()
                .any(|item| { item.get("status").and_then(|v| v.as_str()) == Some("error") }),
            "error item must survive crush_array"
        );
    }

    #[test]
    fn crush_mixed_passthrough_at_threshold() {
        let c = crusher();
        let items: Vec<Value> = vec![
            json!(1),
            json!("two"),
            json!({"k": "v"}),
            json!([1, 2]),
            json!(null),
            json!(true),
            json!(3),
            json!("four"),
        ];
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert_eq!(result.len(), 8);
        assert_eq!(strat, "mixed:passthrough");
    }

    #[test]
    fn crush_mixed_groups_and_compresses_dicts() {
        let c = crusher();
        let mut items: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
        for i in 0..5 {
            items.push(json!(format!("string_{}", i)));
        }
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(strat.starts_with("mixed:adaptive("));
        let str_count = result.iter().filter(|v| v.is_string()).count();
        assert_eq!(str_count, 5);
    }

    #[test]
    fn crush_mixed_keeps_lists_and_nulls_unchanged() {
        let c = crusher();
        let mut items: Vec<Value> = vec![json!([1, 2]); 6];
        items.extend(vec![json!(null); 6]);
        items.extend(vec![json!({"k": 1}); 10]);
        let (result, _strat) = c.crush_mixed_array(&items, "", 1.0);
        let list_count = result.iter().filter(|v| v.is_array()).count();
        let null_count = result.iter().filter(|v| v.is_null()).count();
        assert_eq!(list_count, 6);
        assert_eq!(null_count, 6);
    }

    #[test]
    fn crusher_construction_default() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        assert_eq!(c.config.max_items_after_crush, 15);
    }

    #[test]
    fn crush_non_json_passes_through_unchanged() {
        let c = crusher();
        let result = c.crush("not json at all", "", 1.0);
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "not json at all");
        assert_eq!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_scalar_json_passes_through() {
        let c = crusher();
        let result = c.crush("42", "", 1.0);
        assert_eq!(result.compressed, "42");
        assert!(!result.was_modified);
    }

    #[test]
    fn crush_small_array_passes_through() {
        let c = crusher();
        let result = c.crush(r#"[1,2,3]"#, "", 1.0);
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "[1,2,3]");
    }

    #[test]
    fn crush_dict_array_crushes_when_low_uniqueness() {
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let mut input = String::from("[");
        for i in 0..30 {
            if i > 0 {
                input.push(',');
            }
            input.push_str(r#"{"status":"ok"}"#);
        }
        input.push(']');
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "30 identical dicts should compress (low_uniqueness_safe_to_sample)"
        );
        assert_ne!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_serializes_with_python_safe_format() {
        let c = crusher();
        let input = r#"{"a": 1, "b": 2, "c": 3}"#;
        let result = c.crush(input, "", 1.0);
        assert_eq!(
            result.compressed, r#"{"a":1,"b":2,"c":3}"#,
            "safe_json_dumps emits compact `,` / `:` separators"
        );
    }

    #[test]
    fn crush_recurses_into_nested_arrays() {
        let c = crusher();
        let mut inner = String::from("[");
        for i in 0..30 {
            if i > 0 {
                inner.push(',');
            }
            inner.push_str(r#"{"status":"ok"}"#);
        }
        inner.push(']');
        let input = format!(r#"{{"data": {}}}"#, inner);
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "nested compressible array must be crushed even inside a wrapper object"
        );
    }

    #[test]
    fn crusher_with_custom_scorer() {
        let c = SmartCrusher::with_scorer(SmartCrusherConfig::default(), BM25Scorer::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "anything", 1.0);
        assert!(result.items.len() <= 30);
    }

    #[test]
    fn without_compaction_yields_none_compacted_field() {
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    #[test]
    fn lossless_wins_when_savings_above_threshold() {
        let c = crusher();
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "name": format!("u_{i}"), "status": "ok"}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        let compacted = result.compacted.expect("compacted should be set");
        assert!(compacted.starts_with("[50]{"), "got: {compacted}");
        assert_eq!(result.compaction_kind, Some("table"));
        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "got: {}",
            result.strategy_info
        );
        assert!(result.ccr_hash.is_none());
        assert_eq!(result.items.len(), 50);
    }

    #[test]
    fn lossy_falls_through_when_savings_below_threshold() {
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(
            result.items.len() < 50,
            "expected lossy drop, got {} items",
            result.items.len()
        );
        let h = result.ccr_hash.expect("ccr_hash populated on drop");
        assert_eq!(h.len(), 12);
        assert!(
            result.dropped_summary.contains("_truhowl_pager"),
            "got: {}",
            result.dropped_summary
        );
        assert!(result.dropped_summary.contains(&h.to_string()));
    }

    #[test]
    fn ccr_hash_is_deterministic() {
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i, "tag": "ok"})).collect();
        let r1 = c.crush_array(&items, "", 1.0);
        let r2 = c.crush_array(&items, "", 1.0);
        assert_eq!(r1.ccr_hash, r2.ccr_hash);
        assert!(r1.ccr_hash.is_some());
    }

    #[test]
    fn ccr_hash_changes_with_input() {
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let a: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let b: Vec<Value> = (100..130).map(|i| json!({"id": i})).collect();
        let ra = c.crush_array(&a, "", 1.0);
        let rb = c.crush_array(&b, "", 1.0);
        assert_ne!(ra.ccr_hash, rb.ccr_hash);
    }

    #[test]
    fn lossy_without_compaction_still_emits_ccr_hash() {
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        if result.items.len() < items.len() {
            assert!(result.ccr_hash.is_some());
            assert!(!result.dropped_summary.is_empty());
        }
    }

    #[test]
    fn passthrough_paths_do_not_emit_ccr_hash() {
        let c = crusher();
        let small: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let r = c.crush_array(&small, "", 1.0);
        assert!(r.ccr_hash.is_none());
        assert_eq!(r.dropped_summary, "");
    }

    #[test]
    fn compaction_skips_non_object_array() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|i| json!(i)).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    #[test]
    fn process_string_short_string_passthrough() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let (out, info) = c.process_value(&json!("hello world"), 0, "", 1.0);
        assert_eq!(out, json!("hello world"));
        assert!(info.is_empty());
    }

    #[test]
    fn process_string_stringified_json_array_recurses() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_array_json = serde_json::to_string(
            &(0..50)
                .map(|i| json!({"id": i, "level": "info", "msg": "ok"}))
                .collect::<Vec<_>>(),
        )
        .unwrap();
        let doc = json!({"payload": big_array_json.clone()});
        let (out, info) = c.process_value(&doc, 0, "", 1.0);
        let payload = out.pointer("/payload").and_then(|v| v.as_str()).unwrap();
        assert!(
            info.contains("string_json") || payload != big_array_json,
            "expected processing trace; info={info}, len before={}, after={}",
            big_array_json.len(),
            payload.len(),
        );
    }

    #[test]
    fn process_string_opaque_blob_becomes_ccr_marker() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big_b64});
        let (out, _info) = c.process_value(&doc, 0, "", 1.0);
        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert!(blob.starts_with("<<ccr:"), "got: {blob}");
        assert!(blob.contains(",base64,"));
    }

    #[test]
    fn process_string_top_level_string_processed() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let plain = "just some plain text";
        let result = c.crush(plain, "", 1.0);
        assert_eq!(result.compressed, plain);
    }

    #[test]
    fn process_string_does_not_alter_short_quoted_strings() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let doc = json!({"msg": "{this looks like json but isnt}"});
        let (out, _) = c.process_value(&doc, 0, "", 1.0);
        assert_eq!(out, doc);
    }

    #[test]
    fn process_string_helper_parses_only_containers() {
        assert!(try_parse_json_container("{\"a\":1}").is_some());
        assert!(try_parse_json_container("[1,2,3]").is_some());
        assert!(try_parse_json_container("123").is_none());
        assert!(try_parse_json_container("\"hello\"").is_none());
        assert!(try_parse_json_container("not json").is_none());
        assert!(try_parse_json_container("{malformed").is_none());
    }

    #[test]
    fn enable_ccr_marker_false_suppresses_marker_and_store() {
        let store: Arc<InMemoryCcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            enable_ccr_marker: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher {
            ccr_store: Some(Arc::clone(&store)),
            ..SmartCrusher::new(cfg)
        };
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        assert!(result.ccr_hash.is_none(), "ccr_hash should be None");
        assert!(
            result.dropped_summary.is_empty(),
            "dropped_summary should be empty, got: {:?}",
            result.dropped_summary
        );
        assert_eq!(
            store_len_after, store_len_before,
            "ccr_store grew despite enable_ccr_marker=false"
        );
    }

    #[test]
    fn enable_ccr_marker_true_is_default_behavior() {
        let store: Arc<InMemoryCcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..SmartCrusherConfig::default()
        };
        assert!(cfg.enable_ccr_marker);
        let c = SmartCrusher {
            ccr_store: Some(Arc::clone(&store)),
            ..SmartCrusher::new(cfg)
        };
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        assert!(result.ccr_hash.is_some(), "default should produce a hash");
        assert!(
            result.dropped_summary.contains("_truhowl_pager"),
            "default should produce a marker: {:?}",
            result.dropped_summary
        );
        assert!(
            store_len_after > store_len_before,
            "default should write to ccr_store"
        );
    }

    #[test]
    fn enable_ccr_marker_false_suppresses_opaque_markers() {
        let rows: Vec<Value> = (0..10)
            .map(|i| json!({"path": "a.py", "line": i, "content": "x".repeat(300)}))
            .collect();

        let off = SmartCrusher::new(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.0,
            enable_ccr_marker: false,
            ..SmartCrusherConfig::default()
        });
        let rendered_off = off
            .crush_array(&rows, "", 1.0)
            .compacted
            .expect("lossless table should ship at ratio 0.0");
        assert!(
            !rendered_off.contains("<<ccr:"),
            "opaque marker leaked despite enable_ccr_marker=false: {rendered_off}"
        );
        assert!(
            rendered_off.contains(&"x".repeat(300)),
            "blob should be inline when markers are off: {rendered_off}"
        );

        let on = SmartCrusher::new(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.0,
            ..SmartCrusherConfig::default()
        });
        let rendered_on = on
            .crush_array(&rows, "", 1.0)
            .compacted
            .expect("lossless table should ship at ratio 0.0");
        assert!(
            rendered_on.contains("<<ccr:"),
            "default should still emit the opaque marker: {rendered_on}"
        );
    }

    #[test]
    fn lossless_only_leaves_array_uncompacted_instead_of_dropping() {
        let rows: Vec<Value> = (0..50)
            .map(|i| json!({"path": "a.py", "line": i, "content": "x".repeat(300)}))
            .collect();

        let crusher = SmartCrusher::new(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let result = crusher.crush_array(&rows, "", 1.0);

        assert_eq!(result.items, rows, "lossless_only must not drop rows");
        assert!(result.ccr_hash.is_none(), "no hash under lossless_only");
        assert!(
            result.dropped_summary.is_empty(),
            "no drop sentinel under lossless_only: {:?}",
            result.dropped_summary
        );
        assert!(
            result.compacted.is_none(),
            "nothing shipped, nothing dropped"
        );
    }

    #[test]
    fn lossless_only_inlines_opaque_blobs_when_table_ships() {
        let rows: Vec<Value> = (0..10)
            .map(|i| json!({"path": "a.py", "line": i, "content": "x".repeat(300)}))
            .collect();
        let crusher = SmartCrusher::new(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.0,
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let rendered = crusher
            .crush_array(&rows, "", 1.0)
            .compacted
            .expect("table should ship at ratio 0.0");
        assert!(
            !rendered.contains("<<ccr:"),
            "opaque marker leaked under lossless_only: {rendered}"
        );
        assert!(
            rendered.contains(&"x".repeat(300)),
            "blob should be inline under lossless_only: {rendered}"
        );
    }

    #[test]
    fn lossless_only_never_writes_to_ccr_store() {
        let store: Arc<InMemoryCcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            lossless_only: true,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher {
            ccr_store: Some(Arc::clone(&store)),
            ..SmartCrusher::new(cfg)
        };
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(
            result.items, items,
            "lossless_only must keep every row (no drop)"
        );
        assert!(result.ccr_hash.is_none(), "no hash under lossless_only");
        assert_eq!(
            store.len(),
            store_len_before,
            "ccr_store grew under lossless_only - invariant violated"
        );
    }
}
