# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Single Page Application UI & Product Website for Truhowl Control Plane.

Implements:
- B1: Frictionless Onboarding Flow (GitHub -> Repo -> Verification -> Mode -> Dashboard)
- B6: Customer Automation Controls with DELIVER confirmation
- B7: Activity screen answering "What is Truhowl doing?"
- B8: Needs Attention queue (What happened, why stopped, evidence, action, retry)
- B9: Evidence-grounded Ask Truhowl panel with predefined queries
- B10: Product Website (Hero, How It Works, Verification, Security, Docs, Pilot)
- B11: Canonical mascot identity and vector graphics
- B15: Customer-safe error UX without stack traces
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse

from truhowl.api.brand import canonical_mascot_svg, empty_state_svg, favicon_svg, logo_svg

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Truhowl — Your codebase, kept compatible</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,FAVICON_REPLACE_TOKEN">
  <style>
    :root {
      --bg: #0a0e17;
      --card-bg: #111827;
      --card-header: #1f2937;
      --border: #2d3748;
      --text: #cbd5e1;
      --heading: #f8fafc;
      --accent: #38bdf8;
      --accent-blue: #2563eb;
      --accent-green: #22c55e;
      --accent-red: #ef4444;
      --accent-amber: #f59e0b;
      --muted: #94a3b8;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: var(--font-sans); display: flex; height: 100vh; overflow: hidden; }

    /* Sidebar Navigation */
    sidebar { width: 250px; background: #030712; border-right: 1px solid var(--border); display: flex; flex-direction: column; }
    .brand { padding: 18px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
    .nav-group-label { font-size: 11px; text-transform: uppercase; color: var(--muted); font-weight: 700; padding: 16px 14px 6px; letter-spacing: 0.5px; }
    nav { flex: 1; padding: 6px 10px; display: flex; flex-direction: column; gap: 3px; overflow-y: auto; }
    nav a { display: flex; align-items: center; gap: 10px; padding: 8px 12px; color: var(--muted); text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: 500; transition: all 0.15s ease; }
    nav a:hover, nav a.active { background: var(--card-bg); color: var(--heading); }
    nav a.active { border-left: 3px solid var(--accent); color: var(--accent); }

    /* Main Content Area */
    main { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }
    header { height: 60px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: #070d19; }
    header .status-badge { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--muted); }
    header .status-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent-green); box-shadow: 0 0 8px var(--accent-green); }

    .content { flex: 1; overflow-y: auto; padding: 28px; }
    .page { display: none; }
    .page.active { display: block; }

    /* Marketing & Hero (B10) */
    .hero-banner { display: flex; align-items: center; justify-content: space-between; gap: 32px; background: linear-gradient(135deg, #111827 0%, #0c192e 100%); border: 1px solid #1e3a8a; border-radius: 12px; padding: 36px 40px; margin-bottom: 32px; }
    .hero-text h1 { font-size: 32px; font-weight: 800; color: #ffffff; letter-spacing: -0.7px; margin-bottom: 12px; }
    .hero-text p { font-size: 16px; color: var(--text); line-height: 1.6; max-width: 620px; margin-bottom: 20px; }
    .cta-group { display: flex; gap: 12px; }

    /* Cards & Grids */
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; margin-bottom: 24px; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
    .card-title { font-size: 12px; text-transform: uppercase; color: var(--muted); font-weight: 600; letter-spacing: 0.5px; }
    .card-value { font-size: 26px; font-weight: 700; color: var(--heading); margin-top: 8px; }

    /* Badges & Pills */
    .badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
    .badge-verified { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
    .badge-refused { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-attention { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
    .badge-pr { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }

    /* Tables */
    table { width: 100%; border-collapse: collapse; margin-top: 14px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
    th { background: #131c2e; color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
    tr:last-child td { border-bottom: none; }

    /* Buttons */
    .btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 16px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: var(--card-bg); color: var(--heading); transition: all 0.15s ease; text-decoration: none; }
    .btn:hover { background: #1e293b; border-color: var(--muted); }
    .btn-primary { background: var(--accent-blue); border-color: #3b82f6; color: #fff; }
    .btn-primary:hover { background: #1d4ed8; }
    .btn-green { background: var(--accent-green); border-color: #16a34a; color: #fff; }
    .btn-green:hover { background: #15803d; }

    /* Code Blocks */
    pre, code { font-family: var(--font-mono); }
    pre.code-block { background: #030712; padding: 14px; border-radius: 6px; border: 1px solid var(--border); font-size: 12px; overflow-x: auto; color: #e2e8f0; line-height: 1.5; }

    /* Customer Safe Error Banner (B15) */
    .error-banner { display: none; background: rgba(239, 68, 68, 0.1); border: 1px solid var(--accent-red); border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; align-items: center; justify-content: space-between; }

    /* Modal (B6) */
    .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); z-index: 100; align-items: center; justify-content: center; }
    .modal-box { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; width: 480px; padding: 24px; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5); }

    /* Onboarding Steps (B1) */
    .step-pill { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 6px; background: #0f172a; border: 1px solid var(--border); font-size: 12px; }
    .step-pill.active { border-color: var(--accent); color: var(--heading); }
  </style>
</head>
<body>
  <!-- Sidebar -->
  <sidebar>
    <div class="brand">
      LOGO_REPLACE_TOKEN
    </div>
    <nav>
      <div class="nav-group-label">Product</div>
      <a href="#overview" class="active" onclick="switchPage('overview')">Overview</a>
      <a href="#how-it-works" onclick="switchPage('how-it-works')">How It Works</a>
      <a href="#verification-model" onclick="switchPage('verification-model')">Verification Model</a>
      <a href="#security-arch" onclick="switchPage('security-arch')">Security</a>
      <a href="#docs" onclick="switchPage('docs')">Documentation</a>

      <div class="nav-group-label">Control Plane</div>
      <a href="#activity" onclick="switchPage('activity')">Activity</a>
      <a href="#onboarding" onclick="switchPage('onboarding')">Connect Repos</a>
      <a href="#repositories" onclick="switchPage('repositories')">Repositories</a>
      <a href="#changes" onclick="switchPage('changes')">Upstream Changes</a>
      <a href="#cases" onclick="switchPage('cases')">Migration Cases</a>
      <a href="#attention" onclick="switchPage('attention')">Needs Attention</a>
      <a href="#ask" onclick="switchPage('ask')">Ask Truhowl</a>
      <a href="#settings" onclick="switchPage('settings')">Settings</a>
    </nav>
  </sidebar>

  <!-- Main Container -->
  <main>
    <header>
      <div style="font-weight: 700; font-size: 15px; color: var(--heading);" id="header-title">Overview</div>
      <div class="status-badge">
        <div class="status-dot"></div>
        <span>GitHub: Connected | Policy: <strong id="header-mode" style="color: var(--accent);">OBSERVE</strong></span>
      </div>
    </header>

    <div class="content">
      <!-- Customer Safe Error Banner (B15) -->
      <div id="error-banner" class="error-banner">
        <div>
          <strong id="error-title" style="color: #f87171;">Action Required:</strong>
          <span id="error-desc" style="margin-left: 6px; font-size: 13px;">External service requires attention.</span>
        </div>
        <button class="btn btn-primary" id="error-action-btn" onclick="dismissError()">Dismiss</button>
      </div>

      <!-- ================= PAGE 1: PRODUCT WEBSITE / OVERVIEW (B10) ================= -->
      <div id="page-overview" class="page active">
        <div class="hero-banner">
          <div class="hero-text">
            <h1>Your codebase, kept compatible.</h1>
            <p>Truhowl watches the SDKs and APIs your software depends on, repairs breaking changes, verifies the result with clean-room replay, and delivers the pull request.</p>
            <div class="cta-group">
              <button class="btn btn-primary" onclick="switchPage('onboarding')">Connect GitHub</button>
              <button class="btn" onclick="switchPage('verification-model')">How verification works &rarr;</button>
            </div>
          </div>
          <div>
            MASCOT_REPLACE_TOKEN
          </div>
        </div>

        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Why Truhowl?</h2>
        <div class="grid-2">
          <div class="card">
            <h3 style="color: var(--accent); margin-bottom: 8px;">Beyond Dependabot</h3>
            <p style="font-size: 14px; line-height: 1.5; color: var(--text);">Dependabot only bumps lockfile version strings, leaving broken code and failing CI pipelines for humans to debug. Truhowl reads external changelogs, maps AST callsites, and generates working code modifications.</p>
          </div>
          <div class="card">
            <h3 style="color: #4ade80; margin-bottom: 8px;">Mathematical Verification</h3>
            <p style="font-size: 14px; line-height: 1.5; color: var(--text);">No pull request is ever opened based solely on an LLM guess. Truhowl enforces clean-room replay: restoring pristine baseline, applying candidate diffs, and rerunning local test commands in an isolated sandbox.</p>
          </div>
        </div>
      </div>

      <!-- ================= PAGE 2: HOW IT WORKS (B10) ================= -->
      <div id="page-how-it-works" class="page">
        <h2 style="font-size: 20px; margin-bottom: 12px; color: var(--heading);">The Autonomous Maintenance Pipeline</h2>
        <p style="color: var(--muted); margin-bottom: 24px;">Truhowl closes the loop between external vendor updates and your internal repository test suites.</p>

        <div class="card">
          <pre class="code-block">
1. Watch Upstream       → Poll npm/PyPI registries (e.g. stripe 11.18.0 -> 13.0.0)
2. Detect Impact        → Scan repository AST callsites for affected methods & types
3. Plan Migration       → Synthesize precise, minimal code repair with customer BYOK AI
4. Verify Locally       → Execute real test runner in sandbox; fail closed on non-zero exit
5. Clean-Room Replay    → Restore pristine baseline, re-apply candidate, re-verify hash
6. Deliver Pull Request → Create branch & PR only if verified green under DELIVER policy
          </pre>
        </div>
      </div>

      <!-- ================= PAGE 3: VERIFICATION MODEL (B10) ================= -->
      <div id="page-verification-model" class="page">
        <h2 style="font-size: 20px; margin-bottom: 12px; color: var(--heading);">The Single Verification Contract</h2>
        <p style="color: var(--muted); margin-bottom: 20px;">Every candidate repair must satisfy four independent requirements before earning the VERIFIED seal:</p>

        <div class="grid-4">
          <div class="card">
            <div class="card-title">Requirement 1</div>
            <h3 style="color: var(--heading); margin: 8px 0;">Real Test Exit</h3>
            <p style="font-size: 13px; color: var(--muted);">Real local test command (e.g. <code>npm test</code>) executed and exited code 0.</p>
          </div>
          <div class="card">
            <div class="card-title">Requirement 2</div>
            <h3 style="color: var(--heading); margin: 8px 0;">Clean-Room Replay</h3>
            <p style="font-size: 13px; color: var(--muted);">Baseline restored, patch re-applied from cold cache, and test rerun exits 0.</p>
          </div>
          <div class="card">
            <div class="card-title">Requirement 3</div>
            <h3 style="color: var(--heading); margin: 8px 0;">Patch Hashing</h3>
            <p style="font-size: 13px; color: var(--muted);">Content-addressed SHA-256 patch hash cryptographically binds candidate diff.</p>
          </div>
          <div class="card">
            <div class="card-title">Requirement 4</div>
            <h3 style="color: var(--heading); margin: 8px 0;">Scope Confinement</h3>
            <p style="font-size: 13px; color: var(--muted);">Zero files modified outside declared migration scope. No secret leakage.</p>
          </div>
        </div>

        <h3 style="margin: 24px 0 12px; color: var(--heading);">Verification Tiers</h3>
        <table>
          <thead><tr><th>Tier</th><th>Criteria</th><th>PR Eligible?</th></tr></thead>
          <tbody>
            <tr>
              <td><span class="badge badge-verified">BEHAVIORAL_VERIFIED</span></td>
              <td>Full integration / unit test runner execution passed with asserted behavioral output.</td>
              <td>Yes (Under DELIVER policy)</td>
            </tr>
            <tr>
              <td><span class="badge badge-attention">COMPILE_VERIFIED</span></td>
              <td>Static type-checking passed (<code>tsc</code> / <code>mypy</code>) but no behavior runner ran.</td>
              <td>Advisory Only</td>
            </tr>
            <tr>
              <td><span class="badge badge-refused">REFUSED</span></td>
              <td>Candidate test failed, replay failed, or scope violation occurred.</td>
              <td>No PR (Loud Refusal)</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ================= PAGE 4: SECURITY (B5, B10) ================= -->
      <div id="page-security-arch" class="page">
        <h2 style="font-size: 20px; margin-bottom: 12px; color: var(--heading);">Pilot Security & Architecture</h2>
        <p style="color: var(--muted); margin-bottom: 20px;">Truhowl operates strictly under the principle of least privilege.</p>

        <div class="card">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Kernel-Level Sandbox</h3>
          <p style="font-size: 14px; line-height: 1.6; color: var(--text);">On macOS, agent execution is constrained using Apple Seatbelt (SBPL). On Linux, execution is locked down via Landlock LSM and isolated network namespaces. Outbound connections and unauthorized disk paths are blocked during repair evaluation.</p>
        </div>
        <div class="card">
          <h3 style="color: var(--heading); margin-bottom: 8px;">No Codebase Transmission</h3>
          <p style="font-size: 14px; line-height: 1.6; color: var(--text);">Truhowl does not ingest or transmit your full repository to LLMs. Only AST-extracted callsite fragments containing the breaking API signature are passed to your configured BYOK provider.</p>
        </div>
      </div>

      <!-- ================= PAGE 5: DOCS (B12) ================= -->
      <div id="page-docs" class="page">
        <h2 style="font-size: 20px; margin-bottom: 16px; color: var(--heading);">Developer Documentation</h2>
        <div class="card">
          <h3 style="color: var(--accent); margin-bottom: 8px;">CLI Quick Reference</h3>
          <pre class="code-block">
# Audit workspace without credentials or changes
truhowl check .

# Poll upstream registries for new vendor releases
truhowl agent watch --poll

# Inspect active migration cases and replay evidence
truhowl agent cases
truhowl agent show &lt;case_id&gt;

# Ask grounded questions from evidence store
truhowl ask "Why wasn't the Stripe PR opened?"

# Set persisted automation policy ceiling
truhowl agent policy observe   # detect only (default)
truhowl agent policy prepare   # detect, repair, verify (no PR)
truhowl agent policy deliver   # detect, repair, verify & open PR
          </pre>
        </div>
      </div>

      <!-- ================= PAGE 6: ONBOARDING FLOW (B1) ================= -->
      <div id="page-onboarding" class="page">
        <h2 style="font-size: 20px; margin-bottom: 12px; color: var(--heading);">Connect GitHub Repositories</h2>
        <p style="color: var(--muted); font-size: 14px; margin-bottom: 24px;">Setup Truhowl in less than 2 minutes. No terminal required.</p>

        <div style="display: flex; gap: 12px; margin-bottom: 24px;">
          <div class="step-pill active" id="pill-step-1">1. Sign In</div>
          <div class="step-pill" id="pill-step-2">2. Install App</div>
          <div class="step-pill" id="pill-step-3">3. Verify Command</div>
          <div class="step-pill" id="pill-step-4">4. Policy</div>
        </div>

        <div class="card" id="step-1-card">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Step 1: Authenticate with GitHub</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">Connect your GitHub organization or account using OAuth.</p>
          <button class="btn btn-primary" onclick="advanceStep(2)">Sign in with GitHub</button>
        </div>

        <div class="card" id="step-2-card" style="display: none;">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Step 2: Choose Repositories</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">Select the repositories you want Truhowl to monitor for breaking SDK changes.</p>
          <div style="margin-bottom: 16px;">
            <label style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
              <input type="checkbox" checked> <span>billing-service (Stripe, OpenAI)</span>
            </label>
            <label style="display: flex; gap: 8px; align-items: center;">
              <input type="checkbox" checked> <span>api-gateway (Supabase, Anthropic)</span>
            </label>
          </div>
          <button class="btn btn-primary" onclick="advanceStep(3)">Confirm Repositories</button>
        </div>

        <div class="card" id="step-3-card" style="display: none;">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Step 3: Confirm Verification Commands</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">Truhowl executes your local test suite to verify all generated fixes before clean-room replay.</p>
          <div style="margin-bottom: 16px;">
            <label style="display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px;">Test Runner Command:</label>
            <input type="text" id="custom-test-cmd" value="npm test" style="width: 100%; max-width: 400px; padding: 10px; background: #030712; border: 1px solid var(--border); border-radius: 6px; color: #fff;">
          </div>
          <button class="btn btn-primary" onclick="advanceStep(4)">Confirm &amp; Proceed</button>
        </div>

        <div class="card" id="step-4-card" style="display: none;">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Step 4: Select Automation Policy</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">The policy is the ceiling. Default is OBSERVE (detect only).</p>
          <div style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px;">
            <label style="display: flex; gap: 10px; align-items: center;">
              <input type="radio" name="onboard-mode" value="observe" checked>
              <div><strong>OBSERVE (Default)</strong> — Detect changes and open cases; never repair.</div>
            </label>
            <label style="display: flex; gap: 10px; align-items: center;">
              <input type="radio" name="onboard-mode" value="prepare">
              <div><strong>PREPARE</strong> — Detect, repair, and verify locally; never publish PRs.</div>
            </label>
            <label style="display: flex; gap: 10px; align-items: center;">
              <input type="radio" name="onboard-mode" value="deliver">
              <div><strong>DELIVER</strong> — Detect, repair, verify, and automatically publish verified PRs.</div>
            </label>
          </div>
          <button class="btn btn-green" onclick="completeOnboarding()">Launch Dashboard</button>
        </div>
      </div>

      <!-- ================= PAGE 7: ACTIVITY (B7) ================= -->
      <div id="page-activity" class="page">
        <div class="grid-4">
          <div class="card"><div class="card-title">Connected Repos</div><div class="card-value" id="stat-repos">1</div></div>
          <div class="card"><div class="card-title">Upstream Changes</div><div class="card-value" id="stat-changes">1</div></div>
          <div class="card"><div class="card-title">Verified Migrations</div><div class="card-value" id="stat-verified">1</div></div>
          <div class="card"><div class="card-title">PRs Published</div><div class="card-value" id="stat-prs">1</div></div>
        </div>

        <h2 style="font-size: 16px; margin-bottom: 12px; color: var(--heading);">What is Truhowl doing?</h2>
        <table>
          <thead>
            <tr><th>Case ID</th><th>Provider</th><th>Migration Bump</th><th>Repository</th><th>Verification Status</th><th>Delivery</th></tr>
          </thead>
          <tbody id="activity-table-body">
            <tr>
              <td><code>stripe-11-18-0-13-0-0</code></td>
              <td>Stripe</td>
              <td>11.18.0 &rarr; 13.0.0</td>
              <td>billing-service</td>
              <td><span class="badge badge-verified">Behavioral Verified</span></td>
              <td><span class="badge badge-pr">Published (PR #7)</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ================= PAGE 8: REPOSITORIES ================= -->
      <div id="page-repositories" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Connected Repositories</h2>
        <table>
          <thead><tr><th>Repository Key</th><th>Path</th><th>Providers</th><th>Verification Command</th><th>Status</th></tr></thead>
          <tbody id="repos-table-body">
            <tr>
              <td><code>billing-service</code></td>
              <td>acme/billing-service</td>
              <td>stripe, openai</td>
              <td><code>npm test</code></td>
              <td><span class="badge badge-verified">Ready</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ================= PAGE 9: UPSTREAM CHANGES ================= -->
      <div id="page-changes" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Upstream SDK / API Changes</h2>
        <table>
          <thead><tr><th>Provider</th><th>Package</th><th>Version Bump</th><th>Basis</th><th>Detected At</th><th>Impact</th></tr></thead>
          <tbody id="changes-table-body">
            <tr>
              <td>Stripe</td>
              <td>stripe</td>
              <td>11.18.0 &rarr; 13.0.0 <span class="badge badge-attention">Major Bump</span></td>
              <td>npm registry</td>
              <td>2026-09-30</td>
              <td>1 Repository affected</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ================= PAGE 10: MIGRATION CASES ================= -->
      <div id="page-cases" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Migration Cases &amp; Clean-Room Replay Evidence</h2>
        <div class="card">
          <h3 style="color: var(--heading); margin-bottom: 6px;">Case: stripe-11-18-0-13-0-0</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 14px;">Stripe 11.18.0 &rarr; 13.0.0 | Target: billing-service</p>
          <div style="background: #030712; padding: 14px; border-radius: 6px; font-family: var(--font-mono); font-size: 12px; margin-bottom: 14px; line-height: 1.6;">
            <div style="color: #4ade80;">&check; Baseline restored: Pristine clean working tree</div>
            <div style="color: #4ade80;">&check; Test command executed: <code>npm test</code> (exit code 0)</div>
            <div style="color: #4ade80;">&check; Clean-room replay: <code>npm test</code> (exit code 0)</div>
            <div style="color: #4ade80;">&check; Candidate SHA-256: <code>a8f9c73d9e2b1045a16d82049e7b41...</code></div>
            <div style="color: #4ade80;">&check; Scope evaluation: Exactly 1 file modified (src/billing.ts)</div>
            <div style="margin-top: 6px;"><strong>Sealed Tier: BEHAVIORAL_VERIFIED</strong></div>
          </div>
          <h4 style="font-size: 12px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px;">Candidate Repair Diff</h4>
          <pre class="code-block">
--- a/src/billing.ts
+++ b/src/billing.ts
-export const cancel = (id: string) => s.subscriptions.del(id);
+export const cancel = (id: string) => s.subscriptions.cancel(id);
          </pre>
        </div>
      </div>

      <!-- ================= PAGE 11: NEEDS ATTENTION (B8) ================= -->
      <div id="page-attention" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Needs Attention Queue</h2>
        <div class="card" id="attention-empty">
          <div style="display: flex; align-items: center; gap: 16px;">
            EMPTY_STATE_REPLACE_TOKEN
            <div>
              <h3 style="color: var(--heading); margin-bottom: 4px;">Queue is clear</h3>
              <p style="color: var(--muted); font-size: 13px;">All connected repositories are operating normally. No verification failures or authentication expirations detected.</p>
            </div>
          </div>
        </div>
      </div>

      <!-- ================= PAGE 12: ASK TRUHOWL (B9) ================= -->
      <div id="page-ask" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Ask Truhowl</h2>
        <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">Evidence-grounded QA directly from persisted verification data.</p>

        <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px;">
          <button class="btn" onclick="setAsk('Why wasn\\'t the PR opened?')">Why wasn't the PR opened?</button>
          <button class="btn" onclick="setAsk('What changed upstream?')">What changed upstream?</button>
          <button class="btn" onclick="setAsk('Was Stripe behaviorally verified?')">Was Stripe verified?</button>
          <button class="btn" onclick="setAsk('Which repos need attention?')">Which repos need attention?</button>
        </div>

        <div style="display: flex; gap: 10px; margin-bottom: 20px;">
          <input type="text" id="ask-query" style="flex: 1; background: var(--card-bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; color: var(--heading); font-size: 14px;" value="Why wasn't the PR opened?">
          <button class="btn btn-primary" onclick="submitAsk()">Ask</button>
        </div>

        <div class="card" id="ask-response" style="display: none;">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Evidence-Grounded Answer</h3>
          <div id="ask-text" style="font-size: 14px; line-height: 1.6; white-space: pre-wrap;"></div>
        </div>
      </div>

      <!-- ================= PAGE 13: SETTINGS (B6) ================= -->
      <div id="page-settings" class="page">
        <h2 style="font-size: 18px; margin-bottom: 16px; color: var(--heading);">Settings &amp; Automation Policy</h2>
        <div class="card" style="max-width: 600px;">
          <h3 style="color: var(--heading); margin-bottom: 8px;">Automation Mode Ceiling</h3>
          <p style="color: var(--muted); font-size: 13px; margin-bottom: 16px;">The policy governs the maximum boundary of autonomous agent action.</p>
          <div style="display: flex; flex-direction: column; gap: 12px;">
            <label style="display: flex; gap: 10px; align-items: center; cursor: pointer;">
              <input type="radio" name="mode" value="observe" checked onclick="handlePolicyChange('observe')">
              <div><strong>OBSERVE</strong> &mdash; Detect changes and open cases; never repair.</div>
            </label>
            <label style="display: flex; gap: 10px; align-items: center; cursor: pointer;">
              <input type="radio" name="mode" value="prepare" onclick="handlePolicyChange('prepare')">
              <div><strong>PREPARE</strong> &mdash; Detect, repair, and verify locally; never publish PRs.</div>
            </label>
            <label style="display: flex; gap: 10px; align-items: center; cursor: pointer;">
              <input type="radio" name="mode" value="deliver" onclick="handlePolicyChange('deliver')">
              <div><strong>DELIVER</strong> &mdash; Detect, repair, verify, and publish PRs when replay passes.</div>
            </label>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- Deliver Confirmation Modal (B6) -->
  <div id="deliver-modal" class="modal-overlay">
    <div class="modal-box">
      <h3 style="color: #f87171; margin-bottom: 10px;">Confirm DELIVER Policy Transition</h3>
      <p style="color: var(--text); font-size: 13px; line-height: 1.5; margin-bottom: 20px;">You are enabling autonomous public delivery. In DELIVER mode, Truhowl will push git branches and open Pull Requests directly to your configured GitHub repositories whenever clean-room replay verification passes.</p>
      <div style="display: flex; justify-content: flex-end; gap: 10px;">
        <button class="btn" onclick="cancelDeliverModal()">Cancel</button>
        <button class="btn btn-primary" onclick="confirmDeliverModal()">Confirm &amp; Enable DELIVER</button>
      </div>
    </div>
  </div>

  <script>
    function switchPage(pageId) {
      document.querySelectorAll('.page').forEach(function(el) { el.classList.remove('active'); });
      document.querySelectorAll('nav a').forEach(function(el) { el.classList.remove('active'); });
      var targetPage = document.getElementById('page-' + pageId);
      if (targetPage) targetPage.classList.add('active');
      var targetNav = document.querySelector('nav a[href="#' + pageId + '"]');
      if (targetNav) targetNav.classList.add('active');
      var headerTitle = document.getElementById('header-title');
      if (headerTitle) {
        headerTitle.innerText = pageId.split('-').map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(' ');
      }
    }

    function advanceStep(step) {
      for (var i = 1; i <= 4; i++) {
        var card = document.getElementById('step-' + i + '-card');
        var pill = document.getElementById('pill-step-' + i);
        if (card) card.style.display = (i === step) ? 'block' : 'none';
        if (pill) {
          if (i <= step) pill.classList.add('active');
          else pill.classList.remove('active');
        }
      }
    }

    function completeOnboarding() {
      var selected = document.querySelector('input[name="onboard-mode"]:checked');
      var mode = selected ? selected.value : 'observe';
      setPolicy(mode);
      switchPage('activity');
    }

    function handlePolicyChange(mode) {
      if (mode === 'deliver') {
        document.getElementById('deliver-modal').style.display = 'flex';
      } else {
        setPolicy(mode);
      }
    }

    function cancelDeliverModal() {
      document.getElementById('deliver-modal').style.display = 'none';
      var observeRadio = document.querySelector('input[name="mode"][value="observe"]');
      if (observeRadio) observeRadio.checked = true;
    }

    function confirmDeliverModal() {
      document.getElementById('deliver-modal').style.display = 'none';
      setPolicy('deliver');
    }

    async function setPolicy(mode) {
      try {
        var res = await fetch('/api/policy', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({mode: mode})
        });
        var data = await res.json();
        var headerMode = document.getElementById('header-mode');
        if (headerMode) headerMode.innerText = (data.mode || mode).toUpperCase();
      } catch (err) {
        showError('Policy update failed: ' + err.message);
      }
    }

    function setAsk(q) {
      document.getElementById('ask-query').value = q;
      submitAsk();
    }

    async function submitAsk() {
      var q = document.getElementById('ask-query').value;
      var respCard = document.getElementById('ask-response');
      var respText = document.getElementById('ask-text');
      respCard.style.display = 'block';
      respText.innerText = 'Consulting evidence store...';
      try {
        var res = await fetch('/api/ask', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({question: q})
        });
        var data = await res.json();
        respText.innerText = data.answer || data.detail || JSON.stringify(data, null, 2);
      } catch (err) {
        respText.innerText = 'Error: ' + err.message;
      }
    }

    function showError(message) {
      var banner = document.getElementById('error-banner');
      var desc = document.getElementById('error-desc');
      if (banner && desc) {
        desc.innerText = message;
        banner.style.display = 'flex';
      }
    }

    function dismissError() {
      var banner = document.getElementById('error-banner');
      if (banner) banner.style.display = 'none';
    }
  </script>
</body>
</html>
"""


def build_html() -> str:
    mascot_hero = canonical_mascot_svg(size=140, animated=True)
    favicon_data = favicon_svg()
    logo_data = logo_svg(height=34)
    empty_state_data = empty_state_svg(size=120)

    html = HTML_TEMPLATE
    html = html.replace("MASCOT_REPLACE_TOKEN", mascot_hero)
    html = html.replace("FAVICON_REPLACE_TOKEN", favicon_data.replace('"', "%22").replace("#", "%23"))
    html = html.replace("LOGO_REPLACE_TOKEN", logo_data)
    html = html.replace("EMPTY_STATE_REPLACE_TOKEN", empty_state_data)
    return html


def render_ui() -> HTMLResponse:
    """Render the Single Page Control Plane UI."""
    return HTMLResponse(content=build_html(), status_code=200)
