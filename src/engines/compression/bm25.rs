use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct BM25Scorer {
    avg_doc_len: f64,
    k1: f64,
    b: f64,
}

impl Default for BM25Scorer {
    fn default() -> Self {
        Self {
            avg_doc_len: 100.0,
            k1: 1.5,
            b: 0.75,
        }
    }
}

impl BM25Scorer {
    pub fn compute_bm25(&self, term_freq: f64, doc_len: f64, idf: f64) -> f64 {
        let numerator = term_freq * (self.k1 + 1.0);
        let denominator =
            term_freq + self.k1 * (1.0 - self.b + self.b * doc_len / self.avg_doc_len);
        idf * numerator / denominator
    }

    pub fn compute_idf(&self, n: f64, total: f64) -> f64 {
        if total <= 0.0 || n <= 0.0 {
            return 0.0;
        }
        ((total - n + 0.5) / (n + 0.5) + 1.0).ln()
    }

    pub fn score_batch(&self, items: &[&str], context: Option<&str>) -> Vec<f64> {
        let query = match context {
            Some(q) if !q.is_empty() => q.to_lowercase(),
            _ => return vec![0.5; items.len()],
        };

        let query_terms: Vec<&str> = query.split_whitespace().collect();
        if query_terms.is_empty() {
            return vec![0.5; items.len()];
        }

        let items_lower: Vec<String> = items.iter().map(|s| s.to_lowercase()).collect();
        let total = items.len() as f64;

        let mut doc_freqs: HashMap<&str, f64> = HashMap::new();
        for term in &query_terms {
            let count = items_lower.iter().filter(|doc| doc.contains(term)).count() as f64;
            doc_freqs.insert(*term, count);
        }

        items_lower
            .iter()
            .map(|doc| {
                let doc_len = doc.len() as f64;
                let mut score = 0.0;
                for term in &query_terms {
                    let term_freq = doc.matches(*term).count() as f64;
                    if term_freq > 0.0 {
                        let df = doc_freqs.get(*term).copied().unwrap_or(0.0);
                        let idf = self.compute_idf(df, total);
                        score += self.compute_bm25(term_freq, doc_len, idf);
                    }
                }
                (score / (score + 1.0)).min(1.0)
            })
            .collect()
    }
}
