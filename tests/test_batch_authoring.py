# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Batched authoring: one coherent migration candidate per provider round trip.

Motivation: authoring each file independently produced stitched-together
partial migrations and multiplied provider calls (a throttled provider was
silently reported as "the model produced no patch"). A batched response is
assigned to files only when it carries explicit FILE blocks; anything else
falls back to per-file authoring rather than guessing.
"""

import json
import os

from truhowl.ai_planner import (
    AIPatchPlanner,
    MAX_BATCH_FILES,
    parse_batch_edits,
)
from truhowl.llm import LLMConfig


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _ScriptedClient:
    """Returns a queued response per call and records the prompts it saw."""

    def __init__(self, responses):
        self.config = LLMConfig(provider="fake", api_key="k", model="fake-model")
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, messages=None, system_prompt=None):
        self.prompts.append("\n".join(m.get("content", "") for m in (messages or [])))
        if not self._responses:
            return _FakeResp("")
        return _FakeResp(self._responses.pop(0))


_OLD_A = "import { Configuration, OpenAIApi } from 'openai';\nconst c = new OpenAIApi(new Configuration({}));\n"
_OLD_B = "import { Configuration, OpenAIApi } from 'openai';\nexport const client: OpenAIApi = new OpenAIApi(new Configuration({}));\n"

_BATCH_RESPONSE = """<<<<<<< FILE: src/a.ts
<<<<<<< SEARCH
import { Configuration, OpenAIApi } from 'openai';
=======
import OpenAI from 'openai';
>>>>>>> REPLACE
<<<<<<< SEARCH
const c = new OpenAIApi(new Configuration({}));
=======
const c = new OpenAI({});
>>>>>>> REPLACE
>>>>>>> END FILE
<<<<<<< FILE: src/b.ts
<<<<<<< SEARCH
import { Configuration, OpenAIApi } from 'openai';
=======
import OpenAI from 'openai';
>>>>>>> REPLACE
<<<<<<< SEARCH
export const client: OpenAIApi = new OpenAIApi(new Configuration({}));
=======
export const client: OpenAI = new OpenAI({});
>>>>>>> REPLACE
>>>>>>> END FILE
"""


def _repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    with open(os.path.join(repo, "package.json"), "w") as f:
        json.dump({"name": "svc", "dependencies": {"openai": "^3.3.0"}}, f)
    with open(os.path.join(repo, "src", "a.ts"), "w") as f:
        f.write(_OLD_A)
    with open(os.path.join(repo, "src", "b.ts"), "w") as f:
        f.write(_OLD_B)
    return repo


def test_batch_authoring_migrates_every_file_in_one_call(tmp_path):
    repo = _repo(tmp_path)
    client = _ScriptedClient([_BATCH_RESPONSE])
    planner = AIPatchPlanner(client=client)

    results = planner.plan_and_apply(
        repo_dir=repo,
        affected_files=[os.path.join(repo, "src", "a.ts"), os.path.join(repo, "src", "b.ts")],
        provider_name="openai",
        from_version="^3.3.0",
        to_version="4.0.0",
        migration_details="openai v3 -> v4",
    )

    assert len(client.prompts) == 1, "one authoring call must cover the whole file set"
    assert {os.path.basename(r.file_path) for r in results} == {"a.ts", "b.ts"}
    for rel in ("src/a.ts", "src/b.ts"):
        with open(os.path.join(repo, rel)) as f:
            content = f.read()
        assert "OpenAIApi" not in content
        assert "Configuration" not in content
        assert "import OpenAI from 'openai';" in content


def test_unknown_file_blocks_are_not_written(tmp_path):
    """A FILE block for a path we never offered must not create or edit anything."""
    repo = _repo(tmp_path)
    response = _BATCH_RESPONSE.replace("src/b.ts", "../../escape.ts")
    planner = AIPatchPlanner(client=_ScriptedClient([response]))

    results = planner.plan_and_apply(
        repo_dir=repo,
        affected_files=[os.path.join(repo, "src", "a.ts"), os.path.join(repo, "src", "b.ts")],
        provider_name="openai",
        from_version="^3.3.0",
        to_version="4.0.0",
    )
    assert {os.path.basename(r.file_path) for r in results} == {"a.ts"}
    with open(os.path.join(repo, "src", "b.ts")) as f:
        assert "OpenAIApi" in f.read(), "unassigned file must be untouched"


def test_falls_back_to_per_file_when_response_has_no_file_blocks(tmp_path):
    """Legacy single-file responses still work: no FILE header, no guesswork."""
    repo = _repo(tmp_path)
    legacy = (
        "<<<<<<< SEARCH\nimport { Configuration, OpenAIApi } from 'openai';\n"
        "=======\nimport OpenAI from 'openai';\n>>>>>>> REPLACE\n"
    )
    # One batched attempt consumes the first response, then one call per file.
    client = _ScriptedClient([legacy, legacy, legacy])
    planner = AIPatchPlanner(client=client)

    results = planner.plan_and_apply(
        repo_dir=repo,
        affected_files=[os.path.join(repo, "src", "a.ts"), os.path.join(repo, "src", "b.ts")],
        provider_name="openai",
        from_version="^3.3.0",
        to_version="4.0.0",
    )
    assert len(client.prompts) == 3, "one batched attempt then one call per file"
    assert len(results) == 2
    assert all(r.success for r in results)


def test_provider_failure_is_recorded_not_silent(tmp_path):
    repo = _repo(tmp_path)

    class _Failing:
        config = LLMConfig(provider="fake", api_key="k", model="fake-model")

        def complete(self, messages=None, system_prompt=None):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    planner = AIPatchPlanner(client=_Failing())
    results = planner.plan_and_apply(
        repo_dir=repo,
        affected_files=[os.path.join(repo, "src", "a.ts"), os.path.join(repo, "src", "b.ts")],
        provider_name="openai",
        from_version="^3.3.0",
        to_version="4.0.0",
    )
    assert not any(r.success for r in results), "a failing provider must never yield a patch"
    assert "429" in planner.last_error, "a throttled provider must be identifiable, not silent"


def test_batch_is_skipped_for_oversized_file_sets(tmp_path):
    repo = _repo(tmp_path)
    files = []
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    for i in range(MAX_BATCH_FILES + 1):
        path = os.path.join(repo, "src", f"f{i}.ts")
        with open(path, "w") as f:
            f.write(_OLD_A)
        files.append(path)

    client = _ScriptedClient([])
    AIPatchPlanner(client=client).plan_and_apply(
        repo_dir=repo, affected_files=files, provider_name="openai",
        from_version="^3.3.0", to_version="4.0.0")
    assert len(client.prompts) == len(files), "per-file path must be used above the batch cap"


def test_parse_batch_edits_requires_file_headers():
    assert parse_batch_edits("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n") == {}
    parsed = parse_batch_edits(_BATCH_RESPONSE)
    assert set(parsed) == {"src/a.ts", "src/b.ts"}
    assert all(len(v) == 2 for v in parsed.values())
