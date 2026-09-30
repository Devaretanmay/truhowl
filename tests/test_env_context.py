# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for repository environment, compiler options, and import conventions extraction."""

import json
import os
import shutil
import tempfile
import unittest

from truhowl.env_context import (
    analyze_import_styles,
    extract_package_json,
    extract_repo_environment,
    extract_tsconfig,
    format_repo_environment,
    format_repo_environment_dict,
    parse_jsonc,
)


class TestEnvContext(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_strip_json_comments_and_parse_jsonc(self):
        raw = """{
            // compiler options comment
            "compilerOptions": {
                "module": "commonjs", /* block comment */
                "target": "es2020",
                "esModuleInterop": false,
                "url": "http://example.com/test", // comment after string
                "strict": true,
            },
            "include": ["src/**/*",],
        }"""
        parsed = parse_jsonc(raw)
        self.assertIn("compilerOptions", parsed)
        opts = parsed["compilerOptions"]
        self.assertEqual(opts["module"], "commonjs")
        self.assertEqual(opts["target"], "es2020")
        self.assertFalse(opts["esModuleInterop"])
        self.assertEqual(opts["url"], "http://example.com/test")
        self.assertTrue(opts["strict"])
        self.assertEqual(parsed["include"], ["src/**/*"])

    def test_extract_package_json_esm_and_pnpm(self):
        pkg_data = {
            "name": "my-esm-pkg",
            "type": "module",
            "packageManager": "pnpm@8.15.1",
            "devDependencies": {
                "typescript": "^5.3.3",
            },
        }
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump(pkg_data, f)

        pkg_type, pm, ts_ver, has_ts = extract_package_json(self.test_dir)
        self.assertEqual(pkg_type, "module")
        self.assertEqual(pm, "pnpm")
        self.assertEqual(ts_ver, "^5.3.3")
        self.assertTrue(has_ts)

    def test_extract_package_json_lockfile_detection(self):
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump({"name": "cjs-pkg"}, f)
        with open(os.path.join(self.test_dir, "yarn.lock"), "w") as f:
            f.write("# yarn lockfile v1\n")

        pkg_type, pm, ts_ver, has_ts = extract_package_json(self.test_dir)
        self.assertEqual(pkg_type, "commonjs")
        self.assertEqual(pm, "yarn")
        self.assertEqual(ts_ver, "")
        self.assertFalse(has_ts)

    def test_extract_package_json_bun_detection(self):
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump({"name": "bun-pkg", "dependencies": {"typescript": "5.1.0"}}, f)
        with open(os.path.join(self.test_dir, "bun.lockb"), "w") as f:
            f.write("binary")

        pkg_type, pm, ts_ver, has_ts = extract_package_json(self.test_dir)
        self.assertEqual(pkg_type, "commonjs")
        self.assertEqual(pm, "bun")
        self.assertEqual(ts_ver, "5.1.0")
        self.assertTrue(has_ts)

    def test_extract_tsconfig_with_extends(self):
        base_tsconfig = {
            "compilerOptions": {
                "target": "es2022",
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "strict": True,
            }
        }
        with open(os.path.join(self.test_dir, "tsconfig.base.json"), "w") as f:
            json.dump(base_tsconfig, f)

        app_tsconfig = """{
            "extends": "./tsconfig.base.json",
            "compilerOptions": {
                "esModuleInterop": false,
                "allowSyntheticDefaultImports": false,
            }
        }"""
        with open(os.path.join(self.test_dir, "tsconfig.json"), "w") as f:
            f.write(app_tsconfig)

        opts = extract_tsconfig(self.test_dir)
        self.assertEqual(opts.target, "es2022")
        self.assertEqual(opts.module, "nodenext")
        self.assertEqual(opts.module_resolution, "nodenext")
        self.assertFalse(opts.es_module_interop)
        self.assertFalse(opts.allow_synthetic_default_imports)
        self.assertTrue(opts.strict)

    def test_analyze_import_styles_named_imports_ts2351_prevention(self):
        src_file = os.path.join(self.test_dir, "api.ts")
        with open(src_file, "w") as f:
            f.write(
                "import { Stripe } from 'stripe';\n"
                "import { Router } from 'express';\n"
                "export const router = Router();\n"
            )

        dominant, target_import, stats = analyze_import_styles(
            self.test_dir, relevant_files=["api.ts"], target_pkg="stripe"
        )
        self.assertIn("named imports", dominant)
        self.assertEqual(target_import, "import { Stripe } from 'stripe'")
        self.assertEqual(stats["named_import"], 2)
        self.assertEqual(stats["require"], 0)

    def test_analyze_import_styles_multiline_import(self):
        src_file = os.path.join(self.test_dir, "service.ts")
        with open(src_file, "w") as f:
            f.write(
                "import {\n"
                "    Configuration,\n"
                "    OpenAIApi,\n"
                "} from 'openai';\n"
            )

        dominant, target_import, stats = analyze_import_styles(
            self.test_dir, relevant_files=["service.ts"], target_pkg="openai"
        )
        self.assertIn("import { Configuration, OpenAIApi } from 'openai'", target_import)

    def test_analyze_import_styles_commonjs_require(self):
        src_file = os.path.join(self.test_dir, "server.js")
        with open(src_file, "w") as f:
            f.write(
                "const stripe = require('stripe');\n"
                "const express = require('express');\n"
                "module.exports = { stripe };\n"
            )

        dominant, target_import, stats = analyze_import_styles(
            self.test_dir, relevant_files=["server.js"], target_pkg="stripe"
        )
        self.assertIn("CommonJS", dominant)
        self.assertEqual(target_import, "const stripe = require('stripe')")
        self.assertEqual(stats["require"], 2)
        self.assertEqual(stats["cjs_export"], 1)

    def test_extract_repo_environment_guidance_for_ts2351(self):
        # Setup repo with tsconfig having esModuleInterop: false
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump({
                "name": "billing-service",
                "dependencies": {"stripe": "^11.0.0"},
                "devDependencies": {"typescript": "^5.0.0"}
            }, f)

        with open(os.path.join(self.test_dir, "tsconfig.json"), "w") as f:
            json.dump({
                "compilerOptions": {
                    "module": "commonjs",
                    "target": "es2020",
                    "moduleResolution": "node",
                    "esModuleInterop": False,
                    "strict": True
                }
            }, f)

        os.makedirs(os.path.join(self.test_dir, "src"), exist_ok=True)
        with open(os.path.join(self.test_dir, "src", "billing.ts"), "w") as f:
            f.write("import { Stripe } from 'stripe';\nexport const s = new Stripe('key');\n")

        env = extract_repo_environment(
            self.test_dir,
            relevant_files=["src/billing.ts"],
            target_pkg="stripe",
        )
        self.assertEqual(env.ecosystem, "typescript")
        self.assertEqual(env.package_type, "commonjs")
        self.assertEqual(env.package_manager, "npm")
        self.assertEqual(env.typescript_version, "^5.0.0")
        self.assertFalse(env.compiler_options.es_module_interop)
        self.assertTrue(env.compiler_options.strict)
        self.assertEqual(env.target_pkg_import, "import { Stripe } from 'stripe'")

        # Verify critical compiler rule in guidance
        guidance_text = " ".join(env.guidance)
        self.assertIn("CRITICAL COMPILER RULE", guidance_text)
        self.assertIn("TS2351", guidance_text)
        self.assertIn("named imports", guidance_text)
        self.assertIn("NAMED import syntax", guidance_text)

        # Verify formatting
        formatted = format_repo_environment(env)
        self.assertIn("TypeScript / Node.js", formatted)
        self.assertIn("esModuleInterop: false", formatted)
        self.assertIn("TS2351", formatted)
        self.assertIn("Target Package Import Pattern: `import { Stripe } from 'stripe'`", formatted)

        # Test format_repo_environment_dict
        dict_formatted = format_repo_environment_dict(env.to_dict())
        self.assertEqual(formatted, dict_formatted)

    def test_extract_repo_environment_python_ecosystem(self):
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w") as f:
            f.write("[tool.poetry]\nname = 'py-pkg'\n")
        with open(os.path.join(self.test_dir, "poetry.lock"), "w") as f:
            f.write("# lockfile\n")

        env = extract_repo_environment(self.test_dir)
        self.assertEqual(env.ecosystem, "python")
        self.assertEqual(env.package_manager, "poetry")

    def test_extract_repo_environment_rust_ecosystem(self):
        with open(os.path.join(self.test_dir, "Cargo.toml"), "w") as f:
            f.write("[package]\nname = 'rust-pkg'\nversion = '0.1.0'\n")

        env = extract_repo_environment(self.test_dir)
        self.assertEqual(env.ecosystem, "rust")
        self.assertEqual(env.package_manager, "cargo")


if __name__ == "__main__":
    unittest.main()
