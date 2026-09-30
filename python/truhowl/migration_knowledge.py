# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Symbol-level SDK migration knowledge with recorded provenance.

Hunt's repair path is AI-authored. What the model is *told* about a vendor
change must not come from model memory, so this module holds a small,
normalized, provenance-stamped catalog of what actually changed between two
SDK versions:

    ProviderMigrationSpec
      provider / from / to
      symbol_changes   old_symbol -> new_symbol, change_type, migration unit
      request_changes  argument-shape changes that are not symbol renames
      response_changes response-envelope changes
      removed_types / added_types
      behavior_notes
      sources          the authority each claim is traceable to

Authority is ordered and recorded per entry (SOURCE_AUTHORITY). Where the only
evidence for a change is a development fixture's own harness rather than a
vendor document, the entry says so explicitly (DEVELOPMENT_FIXTURE_CONTRACT)
instead of being dressed up as vendor guidance.

This module is data + lookup only. It never reads a repository, never edits,
and never decides that a repair is complete — the MigrationPlan does that.
"""

from __future__ import annotations

from truhowl.providers.registry import MigrationSource, MigrationKnowledge, SymbolChange

# ── Authority ladder (most authoritative first) ────────────────────────────
#
# Recorded on every source so a repair can always be traced back to *why*
# Truhowl believed a mapping. Lower entries never override higher ones.
SOURCE_AUTHORITY: tuple[str, ...] = (
    "official_migration_guide",
    "official_release_notes",
    "official_changelog",
    "official_type_definitions",
    "openapi_spec_diff",
    "compiler_evidence",
    "development_fixture_contract",
    "model_prior_knowledge",
)

OFFICIAL_MIGRATION_GUIDE = "official_migration_guide"
OFFICIAL_RELEASE_NOTES = "official_release_notes"
OFFICIAL_CHANGELOG = "official_changelog"
OFFICIAL_TYPE_DEFINITIONS = "official_type_definitions"
COMPILER_EVIDENCE = "compiler_evidence"
DEVELOPMENT_FIXTURE_CONTRACT = "development_fixture_contract"

# Migration units. A migration is a coordinated operation over these units;
# missing any one of them is how a partial repair compiles nowhere.
UNIT_IMPORT = "import"
UNIT_INITIALIZATION = "initialization"
UNIT_METHOD_CALL = "method_call"
UNIT_REQUEST_SHAPE = "request_shape"
UNIT_RESPONSE_SHAPE = "response_shape"
UNIT_TYPE = "type"
UNIT_MODULE_SYSTEM = "module_system"
UNIT_DEPENDENCY_METADATA = "dependency_metadata"

ALL_UNITS: tuple[str, ...] = (
    UNIT_IMPORT,
    UNIT_INITIALIZATION,
    UNIT_METHOD_CALL,
    UNIT_REQUEST_SHAPE,
    UNIT_RESPONSE_SHAPE,
    UNIT_TYPE,
    UNIT_MODULE_SYSTEM,
    UNIT_DEPENDENCY_METADATA,
)


def _s(old: str, new: str, change_type: str, unit: str, source_url: str = "", note: str = "") -> SymbolChange:
    return SymbolChange(
        old_symbol=old, new_symbol=new, change_type=change_type,
        unit=unit, source_url=source_url, note=note,
    )


_OPENAI_GUIDE = "https://github.com/openai/openai-node/discussions/217"
_STRIPE_V13_GUIDE = "https://github.com/stripe/stripe-node/wiki/Migration-guide-for-v13"
_STRIPE_V22_GUIDE = "https://github.com/stripe/stripe-node/wiki/Migration-guide-for-v22"
_SUPABASE_GUIDE = "https://supabase.com/docs/reference/javascript/v1-to-v2-migration"
_CLERK_GUIDE = "https://clerk.com/docs/upgrade-guides/v5"
_SENTRY_GUIDE = "https://docs.sentry.io/platforms/javascript/guides/node/migration/v7-to-v8/"
_AWS_GUIDE = "https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/migrating-to-v3.html"
_OCTOKIT_GUIDE = "https://github.com/octokit/rest.js/releases"
_TWILIO_DOCS = "https://www.twilio.com/docs/libraries/node"
_ANTHROPIC_DOCS = "https://docs.anthropic.com/en/api/messages"


def _knowledge(
    symbol_changes: list[SymbolChange],
    sources: list[MigrationSource],
    request_changes: list[str] | None = None,
    response_changes: list[str] | None = None,
    removed_types: list[str] | None = None,
    added_types: list[str] | None = None,
    behavior_notes: list[str] | None = None,
) -> MigrationKnowledge:
    return MigrationKnowledge(
        symbol_changes=symbol_changes,
        sources=sources,
        request_changes=request_changes or [],
        response_changes=response_changes or [],
        removed_types=removed_types or [],
        added_types=added_types or [],
        behavior_notes=behavior_notes or [],
    )


# ── Catalog ────────────────────────────────────────────────────────────────
#
# Keyed by (provider name, from_major, to_major). Lookup is tolerant about
# version-string shape ("^3.3.0", "3.3.0", "3") because manifests disagree.

_SPECS: dict[tuple[str, str, str], MigrationKnowledge] = {}


def _register(provider: str, from_major: str, to_major: str, knowledge: MigrationKnowledge) -> None:
    _SPECS[(provider.lower(), from_major, to_major)] = knowledge


# OpenAI Node SDK v3 -> v4
_register("openai", "3", "4", _knowledge(
    symbol_changes=[
        _s("Configuration", "", "removed", UNIT_IMPORT, _OPENAI_GUIDE,
           "Configuration is gone; client options are passed directly to OpenAI"),
        _s("OpenAIApi", "OpenAI", "renamed", UNIT_IMPORT, _OPENAI_GUIDE,
           "default-import the OpenAI class instead of named-importing OpenAIApi"),
        _s("new Configuration({...})", "new OpenAI({...})", "replaced", UNIT_INITIALIZATION, _OPENAI_GUIDE),
        _s("createCompletion", "completions.create", "renamed", UNIT_METHOD_CALL, _OPENAI_GUIDE,
           "keep prompt-completion semantics; do not convert to chat"),
        _s("createChatCompletion", "chat.completions.create", "moved", UNIT_METHOD_CALL, _OPENAI_GUIDE),
        _s("createEmbedding", "embeddings.create", "renamed", UNIT_METHOD_CALL, _OPENAI_GUIDE),
        _s(".data", "", "removed", UNIT_RESPONSE_SHAPE, _OPENAI_GUIDE,
           "the axios response envelope is gone: response.data.choices -> response.choices"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_OPENAI_GUIDE,
                        title="openai-node v3 to v4 migration guide"),
    ],
    request_changes=[
        "createChatCompletion({ messages, ... }) -> chat.completions.create({ messages, ... })",
        "createCompletion({ prompt, ... }) -> completions.create({ prompt, ... })",
    ],
    response_changes=[
        "completions.create: response.choices[0].text",
        "chat.completions.create: response.choices[0].message.content",
        "embeddings.create: response.data[0].embedding (no response.data.data wrapper)",
    ],
    removed_types=["Configuration", "OpenAIApi", "CreateCompletionRequest", "CreateChatCompletionRequest"],
    added_types=["OpenAI", "ClientOptions"],
    behavior_notes=[
        "Preserve completion vs chat semantics: never upgrade a prompt call into a messages call.",
    ],
))

# Stripe Node SDK v11 -> v13
_register("stripe", "11", "13", _knowledge(
    symbol_changes=[
        _s("subscriptions.del", "subscriptions.cancel", "renamed", UNIT_METHOD_CALL, _STRIPE_V13_GUIDE),
        # Shape-level request contract. Old
        #   charges.create({ amount: amount, ... })
        # New
        #   charges.create({ amount: String(amount), ... })
        _s("amount: amount", "amount: String(amount)", "replaced", UNIT_REQUEST_SHAPE, "",
           "development fixture contract: the fixture harness requires the string form; "
           "stated as fixture evidence, not as vendor guidance"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_STRIPE_V13_GUIDE,
                        title="stripe-node migration guide for v13"),
        MigrationSource(kind=DEVELOPMENT_FIXTURE_CONTRACT, url="",
                        title="trials/fixtures/*_stripe/test/run.js",
                        note="the string-amount contract is defined by the fixture harness, "
                             "not by Stripe documentation"),
    ],
    behavior_notes=[
        "charge amount is minor-unit integer per vendor guidance; the development fixture "
        "additionally requires a string amount",
    ],
))

# Stripe Node SDK v21 -> v22
#
# The vendor guide covers export/typing/runtime-argument changes. The
# string-amount requirement is NOT vendor guidance: it comes from the
# development fixtures' own harness, and is labelled as such on purpose.
_register("stripe", "21", "22", _knowledge(
    symbol_changes=[
        _s("Stripe.StripeContext", "Stripe.StripeContextType", "renamed", UNIT_TYPE, _STRIPE_V22_GUIDE),
        _s("Stripe.errors.StripeError", "Stripe.ErrorType", "replaced", UNIT_TYPE, _STRIPE_V22_GUIDE,
           "the type is now the constructor itself; use typeof or InstanceType"),
        _s("Stripe('key')", "new Stripe('key')", "replaced", UNIT_INITIALIZATION, _STRIPE_V22_GUIDE,
           "Stripe() is an ES6 class in v22; the new operator is required"),
        _s("require('stripe').default", "require('stripe')", "replaced", UNIT_MODULE_SYSTEM, _STRIPE_V22_GUIDE),
        # Same string-amount request contract the v13 fixtures exercise. Stated
        # as fixture evidence, never as vendor guidance.
        _s("amount: amount", "amount: String(amount)", "replaced", UNIT_REQUEST_SHAPE, "",
           "development fixture contract: the fixture harness requires the string form"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_STRIPE_V22_GUIDE,
                        title="stripe-node migration guide for v22"),
        MigrationSource(kind=DEVELOPMENT_FIXTURE_CONTRACT, url="",
                        title="trials/fixtures/*_stripe/test/run.js",
                        note="fixture harness requires `amount: String(amount)`; "
                             "not supported by vendor documentation"),
    ],
    request_changes=[
        "development fixture contract: charges.create amount must be wrapped as String(amount)",
    ],
    behavior_notes=[
        "Callback arguments to service methods were removed in v22; use promises or await.",
        "RequestOptions must be the last argument and cannot be a bare string API key.",
    ],
))

# Supabase JS v1 -> v2
_register("supabase", "1", "2", _knowledge(
    symbol_changes=[
        _s("auth.user", "auth.getUser", "renamed", UNIT_METHOD_CALL, _SUPABASE_GUIDE,
           "now async and returns { data: { user }, error }"),
        _s("auth.session", "auth.getSession", "renamed", UNIT_METHOD_CALL, _SUPABASE_GUIDE,
           "now async and returns { data: { session }, error }"),
        _s("auth.signIn", "auth.signInWithPassword", "renamed", UNIT_METHOD_CALL, _SUPABASE_GUIDE),
        _s("auth.onAuthStateChange", "auth.onAuthStateChange", "signature", UNIT_METHOD_CALL, _SUPABASE_GUIDE,
           "callback now receives (event, session) and fires INITIAL_SESSION"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_SUPABASE_GUIDE,
                        title="Supabase JS client v1 to v2 migration"),
    ],
    response_changes=[
        "auth.getUser()/getSession() resolve to { data, error } instead of returning the value directly",
    ],
    behavior_notes=["These calls are asynchronous; callers need await and .data access."],
))

# Clerk Next.js v4 -> v5
_register("clerk", "4", "5", _knowledge(
    symbol_changes=[
        _s("authMiddleware", "clerkMiddleware", "renamed", UNIT_IMPORT, _CLERK_GUIDE),
        _s("authMiddleware({ publicRoutes })", "clerkMiddleware({\n  publicRoutes,\n})", "replaced",
           UNIT_INITIALIZATION, _CLERK_GUIDE),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_CLERK_GUIDE,
                        title="Clerk upgrade guide for v5"),
    ],
    behavior_notes=["auth checks move from middleware into route handlers/helpers."],
))

# Sentry Node v7 -> v8
_register("sentry", "7", "8", _knowledge(
    symbol_changes=[
        _s("getCurrentHub().getClient()", "getClient()", "replaced", UNIT_METHOD_CALL, _SENTRY_GUIDE),
        _s("Sentry.getCurrentHub()", "Sentry.getClient()", "replaced", UNIT_METHOD_CALL, _SENTRY_GUIDE,
           "hub-based APIs are removed in v8"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_SENTRY_GUIDE,
                        title="Sentry Node v7 to v8 migration"),
    ],
))

# AWS SDK JS v2 -> v3
_register("aws-sdk", "2", "3", _knowledge(
    symbol_changes=[
        _s("require('aws-sdk')", "require('@aws-sdk/client-s3')", "moved", UNIT_MODULE_SYSTEM, _AWS_GUIDE,
           "v3 is modular: import the client package for the service you use"),
        _s("AWS.S3", "S3Client", "renamed", UNIT_INITIALIZATION, _AWS_GUIDE),
        _s("s3.putObject({...})", "s3.send(new PutObjectCommand({...}))", "replaced", UNIT_METHOD_CALL, _AWS_GUIDE),
        _s(".promise()", "", "removed", UNIT_METHOD_CALL, _AWS_GUIDE,
           "v3 client commands are promise-based already"),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_AWS_GUIDE,
                        title="AWS SDK for JavaScript v2 to v3 migration"),
    ],
    request_changes=[
        "lowerCamelCase v2 params are PascalCase command input in v3 (Bucket/Key/Body)",
    ],
    behavior_notes=["v3 commands are separate classes exported from the service client package."],
))

# Twilio Node v3 -> v5
_register("twilio", "3", "5", _knowledge(
    symbol_changes=[
        _s("new Twilio(...)", "new Twilio(...)", "signature", UNIT_INITIALIZATION, _TWILIO_DOCS,
           "v5 is promise-based; the modular client is the default"),
    ],
    sources=[MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_TWILIO_DOCS,
                             title="Twilio Node helper library")],
    behavior_notes=["Raw REST calls made with axios do not change with the SDK version."],
))

# Anthropic SDK 0.x -> 0.5+ (Messages API)
_register("anthropic", "0", "0", _knowledge(
    symbol_changes=[
        _s("completions.create", "messages.create", "renamed", UNIT_METHOD_CALL, _ANTHROPIC_DOCS,
           "the legacy completions endpoint is replaced by the Messages API"),
        _s("response.completion", "response.content[0].text", "replaced", UNIT_RESPONSE_SHAPE, _ANTHROPIC_DOCS),
    ],
    sources=[MigrationSource(kind=OFFICIAL_MIGRATION_GUIDE, url=_ANTHROPIC_DOCS,
                             title="Anthropic Messages API")],
    request_changes=["prompt: string -> messages: [{ role, content }]"],
    response_changes=["response.completion -> response.content[0].text"],
))

# Octokit REST v16 -> v17
_register("octokit", "16", "17", _knowledge(
    symbol_changes=[
        _s("import octokit from '@octokit/rest'", "import { Octokit } from '@octokit/rest'",
           "replaced", UNIT_IMPORT, _OCTOKIT_GUIDE,
           "v17 removed the default export in favour of the named Octokit export"),
        _s("new octokit()", "new Octokit({ auth })", "renamed", UNIT_INITIALIZATION, _OCTOKIT_GUIDE),
        _s("octokit.authenticate", "new Octokit({ auth })", "removed", UNIT_INITIALIZATION, _OCTOKIT_GUIDE),
    ],
    sources=[
        MigrationSource(kind=OFFICIAL_RELEASE_NOTES, url=_OCTOKIT_GUIDE,
                        title="@octokit/rest release notes (v17 breaking changes)"),
    ],
    behavior_notes=["requests are namespaced under octokit.rest in modern versions."],
))


def _major(version: str) -> str:
    """Extract a comparable major from any manifest version spelling."""
    if not version:
        return ""
    v = str(version).strip().lstrip("^~>=< v")
    head = v.split(",")[0].split("|")[0].split(" ")[0].strip()
    return head.split(".")[0] if head else ""


def knowledge_for(provider: str, from_version: str, to_version: str) -> MigrationKnowledge | None:
    """Look up knowledge for a provider version pair. Exact major first.

    Falls back to any entry for the provider whose destination major matches,
    then to the provider's only entry, so version spellings in manifests
    ("^3.3.0" vs "3.3.0") never hide the mapping.
    """
    if not provider:
        return None
    key = (provider.lower().strip(), _major(from_version), _major(to_version))
    if key in _SPECS:
        return _SPECS[key]
    candidates = [k for k in _SPECS if k[0] == key[0]]
    for k in candidates:
        if key[2] and k[2] == key[2]:
            return _SPECS[k]
    if len(candidates) == 1:
        return _SPECS[candidates[0]]
    return None


def known_providers() -> list[str]:
    return sorted({k[0] for k in _SPECS})


def source_authority_rank(kind: str) -> int:
    """Numeric rank for a source kind; lower is more authoritative."""
    try:
        return SOURCE_AUTHORITY.index(kind)
    except ValueError:
        return len(SOURCE_AUTHORITY)


__all__ = [
    "SOURCE_AUTHORITY",
    "ALL_UNITS",
    "UNIT_IMPORT",
    "UNIT_INITIALIZATION",
    "UNIT_METHOD_CALL",
    "UNIT_REQUEST_SHAPE",
    "UNIT_RESPONSE_SHAPE",
    "UNIT_TYPE",
    "UNIT_MODULE_SYSTEM",
    "UNIT_DEPENDENCY_METADATA",
    "DEVELOPMENT_FIXTURE_CONTRACT",
    "knowledge_for",
    "known_providers",
    "source_authority_rank",
]
