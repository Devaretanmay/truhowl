from truhowl.providers.registry import get_default_registry, ProviderSpec


def test_default_provider_registry_contains_core_apis():
    reg = get_default_registry()
    providers = reg.list_providers()
    names = [p.name for p in providers]

    assert "stripe" in names
    assert "openai" in names
    assert "anthropic" in names
    assert "twilio" in names
    assert "resend" in names
    assert "supabase" in names


def test_provider_lookup_and_migrations():
    reg = get_default_registry()
    stripe = reg.get("stripe")
    assert stripe is not None
    assert stripe.display_name == "Stripe Node SDK"
    assert "21.0.0->22.0.0" in stripe.migrations
    m = stripe.migrations["21.0.0->22.0.0"]
    assert m.to_version == "22.0.0"
    assert "stripe-node/wiki" in m.changelog_url


def test_custom_provider_registration():
    reg = get_default_registry()
    custom = ProviderSpec(
        name="custom_api",
        display_name="Custom API SDK",
        package_name="custom-sdk",
        docs_url="https://example.com/docs",
    )
    reg.register(custom)

    found = reg.get("custom_api")
    assert found is not None
    assert found.package_name == "custom-sdk"
