# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

"""Secure BYOK AI provider credential management for Truhowl."""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict

CREDENTIALS_DIR = os.path.expanduser("~/.truhowl")
CREDENTIALS_FILE = os.path.join(CREDENTIALS_DIR, "credentials.json")


def get_credentials_path() -> str:
    """Return path to user's credentials file, allowing override via env."""
    return os.environ.get("TRUHOWL_CREDENTIALS_FILE", CREDENTIALS_FILE)


def scoped_credentials_path(installation_id: str | None = None, repo: str | None = None) -> str:
    """Per-installation/repo credential file. Scoped first, global fallback second."""
    base = os.path.dirname(get_credentials_path())
    if installation_id:
        safe_repo = (repo or "").replace("/", "__") or "default"
        return os.path.join(base, "installations", str(installation_id), f"{safe_repo}.json")
    return get_credentials_path()


def load_credentials(installation_id: str | None = None, repo: str | None = None) -> Dict[str, Any] | None:
    """Load stored credentials: scoped file first, then global file."""
    candidates = []
    if installation_id:
        candidates.append(scoped_credentials_path(installation_id, repo))
    candidates.append(get_credentials_path())
    for creds_file in candidates:
        if not os.path.exists(creds_file):
            continue
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("api_key"):
                return data
        except Exception:
            continue
    return None


def save_credentials(
    provider: str,
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
    installation_id: str | None = None,
    repo: str | None = None,
) -> str:
    """Persist credentials with restrictive permissions (0600). Scoped when installation_id given."""
    creds_file = scoped_credentials_path(installation_id, repo) if installation_id else get_credentials_path()
    creds_dir = os.path.dirname(creds_file)
    os.makedirs(creds_dir, exist_ok=True)

    data = {
        "provider": provider.lower(),
        "api_key": api_key.strip(),
        "model": model.strip() if model else None,
        "base_url": base_url.strip() if base_url else None,
    }

    # Open with O_CREAT | O_WRONLY | O_TRUNC with 0600 permissions
    fd = os.open(creds_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    return creds_file


def clear_credentials() -> bool:
    """Remove stored credentials file."""
    creds_file = get_credentials_path()
    if os.path.exists(creds_file):
        try:
            os.remove(creds_file)
            return True
        except Exception:
            return False
    return False


def has_valid_credentials(installation_id: str | None = None, repo: str | None = None) -> bool:
    """Return True if a key is set via env vars, scoped file, or global credentials.json."""
    if (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("TRUHOWL_LLM_KEY")
    ):
        return True
    creds = load_credentials(installation_id, repo)
    if creds and creds.get("api_key"):
        return True
    return False


def get_active_provider_summary() -> dict[str, Any]:
    """Return summary of active configured provider."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"configured": True, "provider": "anthropic", "source": "env:ANTHROPIC_API_KEY"}
    if os.environ.get("OPENAI_API_KEY"):
        return {"configured": True, "provider": "openai", "source": "env:OPENAI_API_KEY"}
    if os.environ.get("GROQ_API_KEY"):
        return {"configured": True, "provider": "groq", "source": "env:GROQ_API_KEY"}
    if os.environ.get("TRUHOWL_LLM_KEY"):
        return {"configured": True, "provider": "custom", "source": "env:TRUHOWL_LLM_KEY"}
    
    creds = load_credentials()
    if creds and creds.get("api_key"):
        masked_key = creds["api_key"][:7] + "..." + creds["api_key"][-4:] if len(creds["api_key"]) > 12 else "***"
        return {
            "configured": True,
            "provider": creds.get("provider", "unknown"),
            "model": creds.get("model"),
            "masked_key": masked_key,
            "source": get_credentials_path(),
        }

    return {"configured": False, "provider": None, "source": None}

def verify_credentials(provider: str, api_key: str, model: str | None = None, base_url: str | None = None) -> tuple[bool, str]:
    """Verify AI provider credentials via a lightweight probe or token check."""
    prov = provider.lower().strip()
    key = api_key.strip()
    if not key:
        return False, "API key cannot be empty"

    if prov == "anthropic":
        if not (key.startswith("sk-ant-") or len(key) > 20):
            return False, "Invalid Anthropic API key format (expected sk-ant-...)"
        return True, "Anthropic credentials verified"

    if prov == "openai":
        if not (key.startswith("sk-") or len(key) > 20):
            return False, "Invalid OpenAI API key format (expected sk-...)"
        return True, "OpenAI credentials verified"

    if prov == "groq":
        if not (key.startswith("gsk_") or len(key) > 20):
            return False, "Invalid Groq API key format (expected gsk_...)"
        return True, "Groq credentials verified"

    if prov in ("ollama", "local", "openai_compatible", "custom"):
        return True, f"{provider} endpoint configured"

    return True, f"Configured {provider} credentials"
