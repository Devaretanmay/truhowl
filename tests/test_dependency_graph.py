# Copyright 2026 Truhowl Authors

from truhowl.graph import build_dependency_graph, audit_dependency_graph
from truhowl.audit import render_audit_cli, render_audit_github_issue, run_audit


def test_build_dependency_graph_taxonomy():
    graph = build_dependency_graph("trials/fixtures/taxonomy_stripe")
    assert len(graph.get("providers", [])) > 0
    assert len(graph.get("contracts", [])) > 0
    assert len(graph.get("manifest_deps", [])) > 0
    assert len(graph.get("callsites", [])) > 0
    assert len(graph.get("edges", [])) > 0


def test_audit_dependency_graph_taxonomy():
    summary = audit_dependency_graph("trials/fixtures/taxonomy_stripe")
    assert summary["total_providers_detected"] >= 1
    assert summary["total_callsites_mapped"] >= 1
    assert len(summary["at_risk"]) >= 1
    
    stripe_risk = summary["at_risk"][0]
    assert stripe_risk["provider_name"] == "Stripe"
    assert stripe_risk["is_auto_repairable"] is True
    assert stripe_risk["callsites_count"] > 0


def test_render_audit_formats():
    summary = audit_dependency_graph("trials/fixtures/taxonomy_stripe")
    cli_out = render_audit_cli(summary)
    assert "TRUHOWL: EXTERNAL-CHANGE DEPENDENCY AUDIT" in cli_out
    assert "Stripe" in cli_out
    
    issue_out = render_audit_github_issue(summary)
    assert "Truhowl: External Dependency Map & Risk Register" in issue_out
    assert "| **Stripe** |" in issue_out


def test_run_audit_json():
    json_out = run_audit("trials/fixtures/taxonomy_stripe", output_format="json")
    assert '"total_providers_detected":' in json_out
    assert '"at_risk":' in json_out
