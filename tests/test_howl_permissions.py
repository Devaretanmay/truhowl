# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0

import pytest
from unittest.mock import MagicMock

from truhowl.github.client import GitHubAppClient
from truhowl.github.howl_bot import HowlBot
from truhowl.github.hunt_bot import HuntBot


def test_howl_client_is_strictly_readonly():
    howl = HowlBot()
    assert howl.client.readonly is True

    hunt = HuntBot()
    assert hunt.client.readonly is False


def test_howl_raises_permission_error_on_write_attempts():
    howl = HowlBot()

    with pytest.raises(PermissionError, match="read-only authority"):
        howl.client.create_branch("octocat/repo", "main", "truhowl/fix")

    with pytest.raises(PermissionError, match="read-only authority"):
        howl.client.create_or_update_file(
            repo="octocat/repo",
            branch="truhowl/fix",
            path="src/index.ts",
            content="patch",
            commit_message="fix",
        )

    with pytest.raises(PermissionError, match="read-only authority"):
        howl.client.create_pull_request(
            repo="octocat/repo",
            title="Truhowl fix",
            body="Fix details",
            head_branch="truhowl/fix",
        )

    with pytest.raises(PermissionError, match="read-only authority"):
        howl.client.update_pull_request("octocat/repo", 1, state="closed")


def test_howl_can_create_issues_and_comments():
    client = GitHubAppClient(token="test-token", readonly=True)
    client._request = MagicMock(return_value={"number": 42, "id": 100})

    issue = client.create_issue("octocat/repo", "Title", "Body")
    assert issue["number"] == 42
    client._request.assert_called_with("POST", "repos/octocat/repo/issues", data={"title": "Title", "body": "Body"})

    comment = client.post_pr_comment("octocat/repo", 42, "Explanation")
    assert comment["id"] == 100


def test_hunt_client_can_invoke_write_endpoints():
    client = GitHubAppClient(token="test-token", readonly=False)
    client.get_branch_ref = MagicMock(return_value={"object": {"sha": "abcdef123"}})
    client._request = MagicMock(return_value={"ref": "refs/heads/truhowl/fix", "number": 1})

    branch_res = client.create_branch("octocat/repo", "main", "truhowl/fix")
    assert branch_res["ref"] == "refs/heads/truhowl/fix"

    file_res = client.create_or_update_file("octocat/repo", "truhowl/fix", "file.txt", "content", "msg")
    assert file_res["number"] == 1

    pr_res = client.create_pull_request("octocat/repo", "PR Title", "PR Body", "truhowl/fix")
    assert pr_res["number"] == 1
