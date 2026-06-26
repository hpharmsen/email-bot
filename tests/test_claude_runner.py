"""Tests voor claude_runner prompt-bouwen en output-parsing."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from email_bot.claude_runner import (
    Outcome, _is_transient_failure, _load_env_file, _read_head, build_prompt, run,
)
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
def _patched_popen(proc: MagicMock, head: str | None = None):
    """Patch Popen + suppress process-group cleanup + stub _read_head (geen echte
    git-call op tmp_path)."""
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc), \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=head):
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
            patch('email_bot.claude_runner._kill_process_group') as killpg, \
            patch('email_bot.claude_runner._read_head', return_value=None):
        run('p', tmp_path, tmp_path / 'log.txt')
    assert popen.call_args.kwargs.get('start_new_session') is True
    killpg.assert_called_once_with(proc)


def test_run_kills_process_group_even_on_timeout(tmp_path):
    with patch('email_bot.claude_runner.subprocess.Popen',
               return_value=_mock_proc(timeout=True)), \
            patch('email_bot.claude_runner._kill_process_group') as killpg, \
            patch('email_bot.claude_runner._read_head', return_value=None):
        run('p', tmp_path, tmp_path / 'log.txt', timeout=10)
    assert killpg.called


# --- retry on transient API failures ---

def _transient_payload(msg='API Error: Stream idle timeout - partial response received'):
    """Mirror van de echte CLI-output bij een idle-stream failure (0 tokens, 1 turn)."""
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': True,
        'result': msg, 'num_turns': 1,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    })


def test_is_transient_failure_recognizes_stream_idle():
    payload = json.loads(_transient_payload())
    assert _is_transient_failure(payload) is True


def test_is_transient_failure_recognizes_broken_pipe():
    payload = json.loads(_transient_payload('Broken pipe while streaming'))
    assert _is_transient_failure(payload) is True


def test_is_transient_failure_false_when_tokens_used():
    """Als het model al werk heeft gedaan mogen we NIET retryen — risico op dubbele commits."""
    payload = json.loads(_transient_payload())
    payload['usage']['output_tokens'] = 500
    assert _is_transient_failure(payload) is False


def test_is_transient_failure_false_for_max_turns():
    payload = {'subtype': 'error_max_turns', 'result': 'too complex',
               'usage': {'input_tokens': 0, 'output_tokens': 0}, 'num_turns': 200}
    assert _is_transient_failure(payload) is False


def test_is_transient_failure_false_for_unknown_error():
    payload = json.loads(_transient_payload('Permission denied'))
    assert _is_transient_failure(payload) is False


def test_run_retries_transient_failure_then_succeeds(tmp_path):
    success_stdout = json.dumps({'action': 'committed', 'summary': 'Klaar.'})
    proc_fail = _mock_proc(stdout=_transient_payload(), returncode=1)
    proc_ok = _mock_proc(stdout=success_stdout, returncode=0)
    with patch('email_bot.claude_runner.subprocess.Popen', side_effect=[proc_fail, proc_ok]), \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=None), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert outcome.kind == 'committed'
    assert outcome.summary == 'Klaar.'
    sleep.assert_called_once_with(30)


def test_run_exhausts_retries_on_persistent_transient_failure(tmp_path):
    proc_fail = _mock_proc(stdout=_transient_payload(), returncode=1)
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc_fail) as popen, \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=None), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert popen.call_count == 3  # 1 + 2 retries
    assert sleep.call_args_list == [((30,),), ((90,),)]
    assert outcome.kind == 'error'
    assert 'na 3 pogingen' in outcome.summary
    assert 'Stream idle timeout' in outcome.summary


def test_run_does_not_retry_non_transient_error(tmp_path):
    """Permission denied / login required = niet retryen, anders spammen we de API."""
    bad = json.dumps({
        'type': 'result', 'is_error': True,
        'result': 'Not logged in', 'num_turns': 1,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    })
    proc = _mock_proc(stdout=bad, returncode=1)
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc) as popen, \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=None), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert popen.call_count == 1
    sleep.assert_not_called()
    assert outcome.kind == 'error'
    assert 'Not logged in' in outcome.summary


def test_run_does_not_retry_max_turns_error(tmp_path):
    stdout = json.dumps({
        'subtype': 'error_max_turns', 'result': 'hit max turns',
        'num_turns': 200, 'usage': {'input_tokens': 1000, 'output_tokens': 500},
    })
    proc = _mock_proc(stdout=stdout, returncode=1)
    with patch('email_bot.claude_runner.subprocess.Popen', return_value=proc) as popen, \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=None), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt')
    assert popen.call_count == 1
    sleep.assert_not_called()
    assert outcome.kind == 'error'
    assert 'te complex' in outcome.summary


def test_run_success_first_attempt_no_sleep(tmp_path):
    stdout = json.dumps({'action': 'committed', 'summary': 'OK.'})
    with _patched_popen(_mock_proc(stdout=stdout)), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        run('p', tmp_path, tmp_path / 'log.txt')
    sleep.assert_not_called()


# --- retry op wall-clock timeout (subprocess hangt, HEAD onveranderd) ---

def test_run_retries_timeout_when_head_unchanged(tmp_path):
    """Subprocess hangt 25 min op een API-stream zonder dat Claude er zelf op
    afkapt; main.py heeft snapshot, dus retry is veilig zolang HEAD niet bewoog."""
    success_stdout = json.dumps({'action': 'committed', 'summary': 'Klaar.'})
    proc_timeout = _mock_proc(timeout=True)
    proc_ok = _mock_proc(stdout=success_stdout)
    with patch('email_bot.claude_runner.subprocess.Popen',
               side_effect=[proc_timeout, proc_ok]), \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value='abc123'), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt', timeout=10)
    assert outcome.kind == 'committed'
    sleep.assert_called_once_with(30)


def test_run_does_not_retry_timeout_when_head_moved(tmp_path):
    """Timeout na een commit = werk is gedaan; retry zou dubbele commits riskeren."""
    proc_timeout = _mock_proc(timeout=True)
    heads = iter(['abc123', 'def456'])  # pre, post
    with patch('email_bot.claude_runner.subprocess.Popen',
               return_value=proc_timeout) as popen, \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head',
                  side_effect=lambda _: next(heads)), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt', timeout=10)
    assert popen.call_count == 1
    sleep.assert_not_called()
    assert outcome.kind == 'error'
    assert 'timed out' in outcome.summary


def test_run_does_not_retry_timeout_when_not_git_repo(tmp_path):
    """Geen git-repo = kunnen veiligheid niet vaststellen; default niet retryen."""
    proc_timeout = _mock_proc(timeout=True)
    with patch('email_bot.claude_runner.subprocess.Popen',
               return_value=proc_timeout) as popen, \
            patch('email_bot.claude_runner._kill_process_group'), \
            patch('email_bot.claude_runner._read_head', return_value=None), \
            patch('email_bot.claude_runner.time.sleep') as sleep:
        outcome = run('p', tmp_path, tmp_path / 'log.txt', timeout=10)
    assert popen.call_count == 1
    sleep.assert_not_called()


# --- _read_head ---

def test_read_head_returns_hash_for_real_repo(tmp_path):
    sp = subprocess
    sp.run(['git', 'init', '-q', '-b', 'main'], cwd=str(tmp_path), check=True)
    sp.run(['git', 'config', 'user.email', 't@t.com'], cwd=str(tmp_path), check=True)
    sp.run(['git', 'config', 'user.name', 'Test'], cwd=str(tmp_path), check=True)
    (tmp_path / 'a.txt').write_text('hi')
    sp.run(['git', 'add', '.'], cwd=str(tmp_path), check=True)
    sp.run(['git', 'commit', '-q', '-m', 'init'], cwd=str(tmp_path), check=True)
    head = _read_head(tmp_path)
    assert head is not None and len(head) == 40


def test_read_head_returns_none_outside_git_repo(tmp_path):
    assert _read_head(tmp_path) is None


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
