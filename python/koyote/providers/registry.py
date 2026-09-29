"""Provider-Neutral Contract Registry & Migration Specs Catalog."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RewriteRule:
    pattern: str
    replacement: str
    file_extensions: list[str]
    description: str
    is_regex: bool = True


@dataclass
class ProviderMigration:
    from_version: str
    to_version: str
    changelog_url: str
    description: str
    old_spec_path: str | None = None
    new_spec_path: str | None = None
    breaking_changes_count: int = 1
    rewrites: list[RewriteRule] = field(default_factory=list)


@dataclass
class ProviderSpec:
    name: str
    display_name: str
    package_name: str
    docs_url: str
    migrations: dict[str, ProviderMigration] = field(default_factory=dict)
    openapi_spec_url: str | None = None


class ProviderRegistry:
    """Central registry for API providers, contract specifications, and migrations."""

    def __init__(self):
        self._providers: dict[str, ProviderSpec] = {}
        self._load_builtins()

    def register(self, provider: ProviderSpec):
        """Register a new provider specification."""
        self._providers[provider.name.lower()] = provider
        self._providers[provider.package_name.lower()] = provider

    def get(self, name_or_package: str) -> ProviderSpec | None:
        """Lookup provider by name or package identifier."""
        return self._providers.get(name_or_package.lower())

    def list_providers(self) -> list[ProviderSpec]:
        """List all registered unique providers."""
        seen = set()
        unique = []
        for p in self._providers.values():
            if p.name not in seen:
                seen.add(p.name)
                unique.append(p)
        return unique

    def _load_builtins(self):
        stripe_spec = ProviderSpec(
            name="stripe",
            display_name="Stripe Node SDK",
            package_name="stripe",
            docs_url="https://docs.stripe.com/api",
        )
        stripe_spec.migrations["11.0.0->13.0.0"] = ProviderMigration(
            from_version="11.18.0",
            to_version="13.0.0",
            changelog_url="https://github.com/stripe/stripe-node/wiki/Migration-guide-for-v13",
            description="Stripe Node SDK v11 to v13 migration.",
            old_spec_path="trials/fixtures/calcom_stripe/specs/stripe_v11.json",
            new_spec_path="trials/fixtures/calcom_stripe/specs/stripe_v13.json",
            rewrites=[
                RewriteRule(
                    pattern=r'stripe\.subscriptions\.del\(',
                    replacement='stripe.subscriptions.cancel(',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Replace deprecated subscriptions.del with subscriptions.cancel (removed in v13)",
                ),
                RewriteRule(
                    pattern=r'amount:\s*amount',
                    replacement='amount: String(amount)',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Convert amount parameter to string for Stripe contract",
                ),
                RewriteRule(
                    pattern=r'"stripe":\s*"\^11\.\d+\.\d+"',
                    replacement='"stripe": "^13.0.0"',
                    file_extensions=[".json"],
                    description="Bump stripe package version to ^13.0.0",
                ),
            ],
        )
        stripe_spec.migrations["21.0.0->22.0.0"] = ProviderMigration(
            from_version="11.18.0",
            to_version="22.0.0",
            changelog_url="https://github.com/stripe/stripe-node/wiki/Migration-guide-for-v22",
            description="Stripe Node SDK v22 drift: amount field string requirement.",
            old_spec_path="trials/fixtures/taxonomy_stripe/specs/stripe_v21.json",
            new_spec_path="trials/fixtures/taxonomy_stripe/specs/stripe_v22.json",
            rewrites=[
                RewriteRule(
                    pattern=r'amount:\s*amount',
                    replacement='amount: String(amount)',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Convert amount parameter to string for Stripe v22 contract",
                ),
                RewriteRule(
                    pattern=r'"stripe":\s*"\^11\.\d+\.\d+"',
                    replacement='"stripe": "^22.0.0"',
                    file_extensions=[".json"],
                    description="Bump stripe package version to ^22.0.0",
                ),
            ],
        )
        self.register(stripe_spec)

        openai_spec = ProviderSpec(
            name="openai",
            display_name="OpenAI Node SDK",
            package_name="openai",
            docs_url="https://platform.openai.com/docs/api-reference",
        )
        openai_spec.migrations["3.0.0->4.0.0"] = ProviderMigration(
            from_version="3.3.0",
            to_version="4.0.0",
            changelog_url="https://github.com/openai/openai-node/discussions/217",
            description="OpenAI Node SDK v3 to v4 rewrite: createCompletion -> completions.create, createChatCompletion -> chat.completions.create.",
            old_spec_path="trials/fixtures/langchainjs_openai/specs/openai_v3.json",
            new_spec_path="trials/fixtures/langchainjs_openai/specs/openai_v4.json",
            rewrites=[
                RewriteRule(
                    pattern=r'import\s*\{\s*Configuration\s*,\s*OpenAIApi\s*\}\s*from\s*["\']openai["\'];?',
                    replacement='import OpenAI from "openai";',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Replace Configuration and OpenAIApi named imports with default OpenAI import (v4)",
                ),
                RewriteRule(
                    pattern=r':\s*OpenAIApi\b',
                    replacement=': OpenAI',
                    file_extensions=[".ts", ".tsx"],
                    description="Update OpenAIApi type annotation to OpenAI client (v4)",
                ),
                RewriteRule(
                    pattern=r'new\s+OpenAIApi\(\s*new\s+Configuration\((\{[\s\S]*?\})\)\s*\)',
                    replacement=r'new OpenAI(\1)',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Simplify nested new OpenAIApi(new Configuration({...})) to new OpenAI({...}) (v4)",
                ),
                RewriteRule(
                    pattern=r'createCompletion\(',
                    replacement='completions.create(',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx", ".py"],
                    description="Rename createCompletion to completions.create (v4 API)",
                ),
                RewriteRule(
                    pattern=r'createChatCompletion\(',
                    replacement='chat.completions.create(',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx", ".py"],
                    description="Rename createChatCompletion to chat.completions.create (v4 API)",
                ),
                RewriteRule(
                    pattern=r'new Configuration\(\{',
                    replacement='new OpenAI({',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Replace Configuration constructor with OpenAI client (v4)",
                ),
                RewriteRule(
                    pattern=r'completion\.data\.choices',
                    replacement='completion.choices',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Remove .data wrapper from OpenAI completion response (v4)",
                ),
            ],
        )
        self.register(openai_spec)

        anthropic_spec = ProviderSpec(
            name="anthropic",
            display_name="Anthropic SDK",
            package_name="@anthropic-ai/sdk",
            docs_url="https://docs.anthropic.com/en/api/getting-started",
        )
        anthropic_spec.migrations["0.4.0->0.5.0"] = ProviderMigration(
            from_version="0.4.0",
            to_version="0.5.0",
            changelog_url="https://docs.anthropic.com/en/api/messages",
            description="Anthropic Messages API shift: completions.create -> messages.create.",
            rewrites=[
                RewriteRule(
                    pattern=r'completions\.create\(',
                    replacement='messages.create(',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx", ".py"],
                    description="Rename completions.create to messages.create (Anthropic Messages API)",
                ),
            ],
        )
        self.register(anthropic_spec)

        twilio_spec = ProviderSpec(
            name="twilio",
            display_name="Twilio Node Helper",
            package_name="twilio",
            docs_url="https://www.twilio.com/docs/libraries/node",
        )
        self.register(twilio_spec)

        resend_spec = ProviderSpec(
            name="resend",
            display_name="Resend Email SDK",
            package_name="resend",
            docs_url="https://resend.com/docs/api-reference/introduction",
        )
        self.register(resend_spec)

        supabase_spec = ProviderSpec(
            name="supabase",
            display_name="Supabase JS Client",
            package_name="@supabase/supabase-js",
            docs_url="https://supabase.com/docs/reference/javascript/introduction",
        )
        self.register(supabase_spec)

        clerk_spec = ProviderSpec(
            name="clerk",
            display_name="Clerk Next.js SDK",
            package_name="@clerk/nextjs",
            docs_url="https://clerk.com/docs/upgrade-guides/v5",
        )
        clerk_spec.migrations["4.0.0->5.0.0"] = ProviderMigration(
            from_version="4.29.0",
            to_version="5.0.0",
            changelog_url="https://clerk.com/docs/upgrade-guides/v5",
            description="Clerk SDK v4 to v5: authMiddleware -> clerkMiddleware.",
            rewrites=[
                RewriteRule(
                    pattern=r'authMiddleware\(',
                    replacement='clerkMiddleware(',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Replace authMiddleware with clerkMiddleware (v5 API)",
                ),
                RewriteRule(
                    pattern=r'"@clerk/nextjs":\s*"\^4\.\d+\.\d+"',
                    replacement='"@clerk/nextjs": "^5.0.0"',
                    file_extensions=[".json"],
                    description="Bump @clerk/nextjs to ^5.0.0",
                ),
            ],
        )
        self.register(clerk_spec)

        aws_spec = ProviderSpec(
            name="aws-sdk",
            display_name="AWS SDK for JavaScript",
            package_name="aws-sdk",
            docs_url="https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/migrating-to-v3.html",
        )
        aws_spec.migrations["2.0.0->3.0.0"] = ProviderMigration(
            from_version="2.1400.0",
            to_version="3.0.0",
            changelog_url="https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/migrating-to-v3.html",
            description="AWS SDK v2 to v3: modular client and remove .promise().",
            rewrites=[
                RewriteRule(
                    pattern=r'require\([\'"]aws-sdk[\'"]\)',
                    replacement="require('@aws-sdk/client-s3')",
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Migrate monolithic aws-sdk import to modular client (v3 API)",
                ),
                RewriteRule(
                    pattern=r'\.promise\(\)',
                    replacement='',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Strip deprecated .promise() call on AWS SDK v3 client commands",
                ),
                RewriteRule(
                    pattern=r'"aws-sdk":\s*"\^2\.\d+\.\d+"',
                    replacement='"@aws-sdk/client-s3": "^3.0.0"',
                    file_extensions=[".json"],
                    description="Bump aws-sdk to modular @aws-sdk/client-s3 v3",
                ),
            ],
        )
        self.register(aws_spec)

        sentry_spec = ProviderSpec(
            name="sentry",
            display_name="Sentry Node SDK",
            package_name="@sentry/node",
            docs_url="https://docs.sentry.io/platforms/javascript/guides/node/migration/v7-to-v8/",
        )
        sentry_spec.migrations["7.0.0->8.0.0"] = ProviderMigration(
            from_version="7.114.0",
            to_version="8.0.0",
            changelog_url="https://docs.sentry.io/platforms/javascript/guides/node/migration/v7-to-v8/",
            description="Sentry Node v7 to v8: getCurrentHub().getClient() -> getClient().",
            rewrites=[
                RewriteRule(
                    pattern=r'Sentry\.getCurrentHub\(\)\.getClient\(\)',
                    replacement='Sentry.getClient()',
                    file_extensions=[".ts", ".tsx", ".js", ".jsx"],
                    description="Replace deprecated getCurrentHub().getClient() with getClient() (v8 API)",
                ),
                RewriteRule(
                    pattern=r'"@sentry/node":\s*"\^7\.\d+\.\d+"',
                    replacement='"@sentry/node": "^8.0.0"',
                    file_extensions=[".json"],
                    description="Bump @sentry/node to ^8.0.0",
                ),
            ],
        )
        self.register(sentry_spec)


_GLOBAL_REGISTRY: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    """Get global default provider registry singleton."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ProviderRegistry()
    return _GLOBAL_REGISTRY


def find_migration_for(source: Any) -> ProviderMigration | None:
    """Adapter: resolve a ChangeSource to a registry migration.

    Only sdk/external_api kinds backed by the provider registry resolve;
    every other kind returns None (→ quarantine/AI path). Thin by design.
    """
    kind = getattr(source, "kind", "")
    if kind not in ("sdk", "external_api"):
        return None
    identity = getattr(source, "identity", "") or ""
    spec = get_default_registry().get(identity)
    if not spec or not spec.migrations:
        return None
    want_from = getattr(source, "version_from", "") or ""
    want_to = getattr(source, "version_to", "") or ""

    def _norm(v: str) -> str:
        return v.strip().lstrip("^~>=< ")

    migs = list(spec.migrations.values())
    nf, nt = _norm(want_from), _norm(want_to)
    for m in migs:
        if nf and nt and _norm(m.from_version) == nf and _norm(m.to_version) == nt:
            return m
    for m in migs:
        if nf and _norm(m.from_version) == nf:
            return m
        if nt and _norm(m.to_version) == nt:
            return m
    return migs[0]
