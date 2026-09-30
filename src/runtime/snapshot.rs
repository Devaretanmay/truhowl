// Hash-based filesystem snapshot for rollback: snapshot() copies every file
// under `workdir` (minus build/vendor dirs) to `snapshot_dir` and records its
// blake3 hash; restore() copies back only files whose hash changed.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

pub const MANIFEST: &str = "_manifest.json";

// Top-level directory names never snapshotted.
const DEFAULT_EXCLUDE: &[&str] = &[
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".truhowl",
    ".hg",
    ".svn",
    "target",
    "build",
    "dist",
    ".next",
];

// blake3 hash, hex-truncated to 16 chars (matches Python manifest format).
fn file_hash(path: &Path) -> std::io::Result<String> {
    let mut hasher = blake3::Hasher::new();
    let mut file = fs::File::open(path)?;
    hasher.update_reader(&mut file)?;
    let hex = hasher.finalize().to_hex();
    Ok(hex[..16].to_string())
}

fn walk_files(
    dir: &Path,
    base: &Path,
    exclude: &HashSet<String>,
    visit: &mut impl FnMut(&Path, &Path),
) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if exclude.contains(&name) {
            continue;
        }
        if path.is_dir() {
            walk_files(&path, base, exclude, visit);
        } else if path.is_file() {
            let rel = path.strip_prefix(base).unwrap_or(&path);
            visit(&path, rel);
        }
    }
}

pub struct SnapshotManager {
    workdir: PathBuf,
    snapshot_dir: PathBuf,
    exclude: HashSet<String>,
}

impl SnapshotManager {
    pub fn new(workdir: &str, snapshot_dir: &str, exclude: Option<HashSet<String>>) -> Self {
        let exclude = match exclude {
            Some(set) => set,
            None => DEFAULT_EXCLUDE.iter().map(|s| s.to_string()).collect(),
        };
        Self {
            workdir: PathBuf::from(workdir),
            snapshot_dir: PathBuf::from(snapshot_dir),
            exclude,
        }
    }

    pub fn snapshot(&self) -> Result<usize, String> {
        if self.snapshot_dir.is_dir() {
            fs::remove_dir_all(&self.snapshot_dir).map_err(|e| e.to_string())?;
        }
        fs::create_dir_all(&self.snapshot_dir).map_err(|e| e.to_string())?;

        let mut manifest: serde_json::Map<String, serde_json::Value> = serde_json::Map::new();
        let mut count = 0usize;
        walk_files(
            &self.workdir,
            &self.workdir,
            &self.exclude,
            &mut |src, rel| {
                let dst = self.snapshot_dir.join(rel);
                if let Some(parent) = dst.parent() {
                    let _ = fs::create_dir_all(parent);
                }
                if let (Ok(h), Ok(_)) = (file_hash(src), fs::copy(src, &dst)) {
                    manifest.insert(
                        rel.to_string_lossy().to_string(),
                        serde_json::Value::String(h),
                    );
                    count += 1;
                }
            },
        );

        let manifest_path = self.snapshot_dir.join(MANIFEST);
        let json = serde_json::Value::Object(manifest);
        fs::write(&manifest_path, json.to_string()).map_err(|e| e.to_string())?;
        Ok(count)
    }

    pub fn restore(&self) -> Result<usize, String> {
        let manifest_path = self.snapshot_dir.join(MANIFEST);
        if !manifest_path.is_file() {
            return Ok(0);
        }
        let raw = fs::read_to_string(&manifest_path).map_err(|e| e.to_string())?;
        let manifest: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(&raw).map_err(|e| e.to_string())?;

        let mut count = 0usize;
        for (rel, expected) in manifest.iter() {
            let current_path = self.workdir.join(rel);
            let expected_hash = expected.as_str().unwrap_or_default().to_string();

            let needs_restore = match file_hash(&current_path) {
                Ok(h) => h != expected_hash,
                Err(_) => true, // missing or unreadable: restore
            };
            if !needs_restore {
                continue;
            }

            let src = self.snapshot_dir.join(rel);
            if !src.is_file() {
                continue;
            }
            if let Some(parent) = current_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            if fs::copy(&src, &current_path).is_ok() {
                count += 1;
            }
        }

        let tracked: HashSet<String> = manifest.keys().cloned().collect();
        let mut generated = Vec::new();
        walk_files(
            &self.workdir,
            &self.workdir,
            &self.exclude,
            &mut |_src, rel| {
                let rel = rel.to_string_lossy().to_string();
                if !tracked.contains(&rel) {
                    generated.push(self.workdir.join(&rel));
                }
            },
        );
        for path in generated {
            if fs::remove_file(path).is_ok() {
                count += 1;
            }
        }
        Ok(count)
    }

    pub fn cleanup(&self) -> Result<(), String> {
        if self.snapshot_dir.is_dir() {
            fs::remove_dir_all(&self.snapshot_dir).map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn tmpdir(name: &str) -> String {
        let dir = std::env::temp_dir().join(format!("bw_snap_{name}_{}", uuid()));
        dir.to_string_lossy().to_string()
    }

    fn uuid() -> String {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("{n:x}")
    }

    #[test]
    fn snapshot_and_restore_changed_file() {
        let workdir = tmpdir("work");
        let snap = format!("{workdir}.snap");
        fs::create_dir_all(&workdir).unwrap();
        fs::write(format!("{workdir}/a.txt"), "hello").unwrap();

        let mgr = SnapshotManager::new(&workdir, &snap, None);
        assert_eq!(mgr.snapshot().unwrap(), 1);

        fs::write(format!("{workdir}/a.txt"), "changed").unwrap();
        assert_eq!(mgr.restore().unwrap(), 1);
        assert_eq!(
            fs::read_to_string(format!("{workdir}/a.txt")).unwrap(),
            "hello"
        );

        let _ = mgr.cleanup();
        let _ = fs::remove_dir_all(&workdir);
    }

    #[test]
    fn restore_restores_deleted_file() {
        let workdir = tmpdir("del");
        let snap = format!("{workdir}.snap");
        fs::create_dir_all(&workdir).unwrap();
        fs::write(format!("{workdir}/a.txt"), "data").unwrap();

        let mgr = SnapshotManager::new(&workdir, &snap, None);
        mgr.snapshot().unwrap();
        fs::remove_file(format!("{workdir}/a.txt")).unwrap();

        assert_eq!(mgr.restore().unwrap(), 1);
        assert!(Path::new(&format!("{workdir}/a.txt")).is_file());

        let _ = mgr.cleanup();
        let _ = fs::remove_dir_all(&workdir);
    }

    #[test]
    fn excludes_build_dirs() {
        let workdir = tmpdir("excl");
        let snap = format!("{workdir}.snap");
        fs::create_dir_all(format!("{workdir}/target")).unwrap();
        fs::create_dir_all(&workdir).unwrap();
        fs::write(format!("{workdir}/keep.txt"), "k").unwrap();
        fs::write(format!("{workdir}/target/skip.txt"), "s").unwrap();

        let mgr = SnapshotManager::new(&workdir, &snap, None);
        assert_eq!(mgr.snapshot().unwrap(), 1);

        let _ = mgr.cleanup();
        let _ = fs::remove_dir_all(&workdir);
    }
}
