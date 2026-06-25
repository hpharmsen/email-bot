"""Tests voor claude_runner prompt-bouwen en output-parsing."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from email_bot.claude_runner import Outcome, _load_env_file, build_prompt, run
from email_bot.gmail_client import ParsedMessage


def _parsed(body='Tagline wijzigen.'):
    return ParsedMessage(
        gmail_id='g', thread_id='t',
        sender='hp@harmsen.nl', sender_raw='hp@harmsen.nl',
        recipient='bra1n@harmsen.nl',
        subject='Test', body=body,
        message_id='<a@b>', in_reply_to=None, references='',
        headers={}, auth_results='spf=pass; dkim=pass; dmarc=pass',
    )


def _mock_proc(stdout='', stderr='', returncode=0, timeout=False) -> MagicMock:
    proc = MagicMock()
    proc.pid = 999999
    if timeout:
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd='claude', timeout=10)
    else:
        proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


@contextmanager
def _patched_popen(proc: MagicMock):
    """Patch Popen + suppress process-group cleanup (no real PIDs in tests)."""
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc), \
            patch('email_bot.claude_runner._kill_process_group'):
        yield


# --- build_prompt ---

def test_prompt_includes_untrusted_delimiters(tmp_path):
    out = build_prompt(_parsed(body='Update tagline.'), 'bra1n', tmp_path, [])
    assert 'UNTRUSTED USER CONTENT START' in out
    assert 'UNTRUSTED USER CONTENT END' in out
    assert 'Update tagline' in out


def test_prompt_mentions_project_path_and_scope(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert str(tmp_path) in out
    assert '.git/' in out


def test_prompt_includes_project_name(tmp_path):
    out = build_prompt(_parsed(), 'juf-marijke', tmp_path, [])
    assert 'juf-marijke' in out


def test_prompt_lists_attachments(tmp_path):
    img = tmp_path / 'x.jpg'
    img.touch()
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [img])
    assert str(img) in out


def test_prompt_no_attachments_says_geen(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert 'BIJLAGEN (al gevalideerd en opgeslagen): geen' in out


def test_prompt_includes_verification_steps(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert 'VERIFICATIE' in out
    assert 'Self-review' in out
    assert 'agent-browser' in out
    assert '1280x800' in out
    assert '390x844' in out


def test_prompt_includes_live_url_when_given(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [],
                       live_url='https://bra1n.example.com')
    assert 'https://bra1n.example.com' in out
    assert 'verify_url' in out


def test_prompt_handles_missing_live_url(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert 'LIVE-URL BASIS' in out
    assert 'geen geconfigureerd' in out


def test_prompt_includes_claude_md_when_present(tmp_path):
    (tmp_path / 'CLAUDE.md').write_text('# stijl')
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert 'CLAUDE.md' in out


def test_prompt_omits_claude_md_hint_when_absent(tmp_path):
    out = build_prompt(_parsed(), 'bra1n', tmp_path, [])
    assert 'CLAUDE.md' not in out


# --- run ---

def test_run_committed_success(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'committed', 'summary': 'Tagline gewijzigd.',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome == Outcome('committed', 'Tagline gewijzigd.')


def test_run_clarify(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'clarify', 'summary': 'Welke pagina?',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'clarify'
    assert 'pagina' in outcome.summary


def test_run_reject(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'reject', 'summary': 'Destructief verzoek.',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'reject'


def test_run_nonzero_exit_returns_error(tmp_path):
    with _patched_popen(_mock_proc(stderr='boom', returncode=1)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'error'
    assert 'exit 1' in outcome.summary
    assert 'boom' in outcome.summary


def test_run_nonzero_exit_extracts_result_field_from_json(tmp_path):
    stdout = json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': True,
        'result': 'Not logged in · Please run /login',
    })
    with _patched_popen(_mock_proc(stdout=stdout, returncode=1)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'error'
    assert outcome.summary == 'Claude exit 1: Not logged in · Please run /login'


def test_run_timeout_returns_error(tmp_path):
    with _patched_popen(_mock_proc(timeout=True)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt', timeout=600)
    assert outcome.kind == 'error'
    assert 'timed out' in outcome.summary


def test_run_invalid_json_returns_error(tmp_path):
    with _patched_popen(_mock_proc(stdout='not json')):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'error'
    assert 'parsing' in outcome.summary.lower()


def test_run_unknown_action_returns_error(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'destroy_world', 'summary': 'mwhaha',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'error'
    assert 'Unknown action' in outcome.summary


def test_run_accepts_flat_payload_without_structured_output_wrapper(tmp_path):
    stdout = json.dumps({'action': 'committed', 'summary': 'OK.'})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'committed'


def test_run_extracts_verify_url(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'committed', 'summary': 'About-page bijgewerkt.',
        'verify_url': 'https://example.com/about',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.verify_url == 'https://example.com/about'


def test_run_drops_non_http_verify_url(tmp_path):
    stdout = json.dumps({'structured_output': {
        'action': 'committed', 'summary': 'X.',
        'verify_url': 'javascript:alert(1)',
    }})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.verify_url is None


def test_run_verify_url_optional(tmp_path):
    stdout = json.dumps({'structured_output': {'action': 'committed', 'summary': 'X.'}})
    with _patched_popen(_mock_proc(stdout=stdout)):
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.verify_url is None


def test_run_starts_new_session_for_process_group_cleanup(tmp_path):
    stdout = json.dumps({'action': 'committed', 'summary': 'OK.'})
    proc = _mock_proc(stdout=stdout)
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc) as popen, \
            patch('email_bot.claude_runner._kill_process_group') as killpg:
        run('p', tmp_path, tmp_path / 'log.txt')
    assert popen.call_args.kwargs.get('start_new_session') is True
    killpg.assert_called_once_with(proc)


def test_run_kills_process_group_even_on_timeout(tmp_path):
    with patch('email_bot.claude_runner.subprocess.Popen',
               return_value=_mock_proc(timeout=True)), \
            patch('email_bot.claude_runner._kill_process_group') as killpg:
        run('p', tmp_path, tmp_path / 'log.txt', timeout=10)
    assert killpg.called


# --- _load_env_file ---

def test_load_env_file_missing_returns_empty(tmp_path):
    assert _load_env_file(tmp_path / 'nope.env') == {}


def test_load_env_file_parses_basic_pairs(tmp_path):
    env = tmp_path / '.env'
    env.write_text('FOO=bar\nBAZ=qux\n')
    assert _load_env_file(env) == {'FOO': 'bar', 'BAZ': 'qux'}


def test_load_env_file_skips_blanks_and_comments(tmp_path):
    env = tmp_path / '.env'
    env.write_text('# header\n\nFOO=bar\n# another\n')
    assert _load_env_file(env) == {'FOO': 'bar'}


def test_load_env_file_strips_quotes(tmp_path):
    env = tmp_path / '.env'
    env.write_text('FOO="bar baz"\nQUX=\'one\'\n')
    assert _load_env_file(env) == {'FOO': 'bar baz', 'QUX': 'one'}
