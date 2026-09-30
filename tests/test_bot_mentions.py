from unittest.mock import MagicMock
from truhowl.github.pr_bot import handle_issue_comment_event
from truhowl.repo_identity import get_repository, STATE_ACTIVE, register_repository

def _comment_payload(text, repo='acme/backend', sender_type='User', is_pr=True):
    issue = {'number': 7}
    if is_pr:
        issue['pull_request'] = {'url': f'https://api.github.com/{repo}/pulls/7'}
    return {
        'action': 'created',
        'repository': {'full_name': repo},
        'issue': issue,
        'comment': {'body': text, 'user': {'login': 'dev'}},
        'sender': {'type': sender_type},
    }

def test_single_tag_howl_activates_bot(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test123456789012345')
    client = MagicMock()
    client.get_pull_request.return_value = {
        'number': 7, 'title': 'Test PR', 'body': 'body',
        'head': {'ref': 'feat', 'sha': 'abc1234'},
        'base': {'ref': 'main'},
    }
    client.get_pull_request_files.return_value = []
    
    register_repository('acme/backend')
    res = handle_issue_comment_event(
        _comment_payload('@howl', repo='acme/backend'), 'issue_comment.created', client,
        workdir=str(tmp_path)
    )
    assert res['success'] is True
    assert res['event_type'] == 'issue_comment.howl'
    
    rec = get_repository('acme/backend')
    assert rec['howl_state'] == STATE_ACTIVE
