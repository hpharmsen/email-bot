"""Tests voor de orchestrator in email_bot.main."""
from __future__ import annotations

import subprocess as sp
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from email_bot import main as M
from email_bot.claude_runner import Outcome
from email_bot.gmail_client import ParsedMessage
from email_bot.projects import Project

AUTH_PASS = 'mx.google.com; spf=pass; dkim=pass; dmarc=pass'
AUTH_FAIL = 'mx.google.com; spf=fail; dkim=fail; dmarc=fail'


def _project(name='bra1n', path=Path('/tmp/bra1n'), email='bra1n@harmsen.nl',
             extra_whitelist=(), publish_script=None) -> Project:
    return Project(
        name=name,
        path=path,
        email=email,
        whitelist=frozenset({*extra_whitelist, 'hp@harmsen.nl'}),
        live_url='https://example.com',
        alias_name=name.capitalize(),
        publish_script=publish_script,
    )


def _parsed(sender='hp@harmsen.nl', sender_raw='HP <hp@harmsen.nl>',
            recipient='bra1n@harmsen.nl',
            auth=AUTH_PASS, in_reply_to=None, body='Tagline wijzigen.'):
    return ParsedMessage(
        gmail_id='g1', thread_id='t1',
        sender=sender, sender_raw=sender_raw,
        recipient=recipient,
        subject='Test', body=body,
        message_id='<a@b>', in_reply_to=in_reply_to, references='',
        headers={}, auth_results=auth,
    )


def _gmail_mock() -> MagicMock:
    g = MagicMock()
    g.fetch_raw_email.return_value = MagicMock()
    return g


def _projects_by_email(*projects: Project) -> dict[str, Project]:
    return {p.email: p for p in projects}


# --- Whitelist ---

def test_owner_always_in_whitelist() -> None:
    p = _project()
    assert M.in_whitelist('hp@harmsen.nl', p) is True


def test_in_whitelist_case_insensitive() -> None:
    p = _project(extra_whitelist=['michael@anderswinst.com'])
    assert M.in_whitelist('MICHAEL@ANDERSWINST.COM', p) is True
    assert M.in_whitelist('other@x', p) is False


# --- Rate limit ---

def test_rate_limit_under_threshold(tmp_path: Path) -> None:
    rf = tmp_path / 'r.json'
    now = time.time()
    for _ in range(M.RATE_LIMIT_PER_HOUR - 1):
        M.record_commit(now=now, rate_file=rf)
    assert M.is_rate_limited(now=now, rate_file=rf) is False


def test_rate_limit_at_threshold(tmp_path: Path) -> None:
    rf = tmp_path / 'r.json'
    now = time.time()
    for _ in range(M.RATE_LIMIT_PER_HOUR):
        M.record_commit(now=now, rate_file=rf)
    assert M.is_rate_limited(now=now, rate_file=rf) is True


def test_rate_limit_old_entries_expire(tmp_path: Path) -> None:
    rf = tmp_path / 'r.json'
    for _ in range(M.RATE_LIMIT_PER_HOUR + 1):
        M.record_commit(now=time.time() - 7200, rate_file=rf)
    assert M.is_rate_limited(rate_file=rf) is False


# --- Lock ---

def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    lock = tmp_path / 'lock'
    with M._acquire_lock(lock) as got:
        assert got is True
    assert not lock.exists()


def test_lock_blocks_when_held(tmp_path: Path) -> None:
    lock = tmp_path / 'lock'
    with M._acquire_lock(lock) as outer:
        assert outer is True
        with M._acquire_lock(lock) as inner:
            assert inner is False


# --- Snapshot + publish (echte git tmp-repos) ---

def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    sp.run(['git', 'init', '-q', '-b', 'main'], cwd=str(path), check=True)
    sp.run(['git', 'config', 'user.email', 't@t.com'], cwd=str(path), check=True)
    sp.run(['git', 'config', 'user.name', 'Test'], cwd=str(path), check=True)
    (path / 'index.html').write_text('hello')
    sp.run(['git', 'add', '.'], cwd=str(path), check=True)
    sp.run(['git', 'commit', '-q', '-m', 'init'], cwd=str(path), check=True)
    return sp.run(['git', 'rev-parse', 'HEAD'], cwd=str(path),
                  capture_output=True, text=True, check=True).stdout.strip()


def test_publish_no_changes_raises(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    head = _init_git_repo(repo)
    p = _project(path=repo)
    with pytest.raises(M.PublishError):
        M.publish(p, M.Snapshot(head=head))


def test_publish_pushes_clean_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    pushed = []
    monkeypatch.setattr(M, '_git_push', lambda r, fallback_head=None: pushed.append(r))
    (repo / 'index.html').write_text('updated')
    sp.run(['git', 'commit', '-aq', '-m', 'tagline'], cwd=str(repo), check=True)
    p = _project(path=repo)
    commit, files = M.publish(p, M.Snapshot(head=pre))
    assert commit and commit != pre
    assert files == ['index.html']
    assert pushed == [repo]


def test_publish_scope_violation_reverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    pushed = []
    monkeypatch.setattr(M, '_git_push', lambda r, fallback_head=None: pushed.append(r))
    (repo / '.github').mkdir()
    (repo / '.github' / 'evil.yml').write_text('evil: true')
    sp.run(['git', 'add', '.github'], cwd=str(repo), check=True)
    sp.run(['git', 'commit', '-q', '-m', 'malicious'], cwd=str(repo), check=True)
    p = _project(path=repo)
    with pytest.raises(M.ScopeViolation) as exc:
        M.publish(p, M.Snapshot(head=pre))
    assert any(path.startswith('.github/') for path in exc.value.paths)
    assert pushed == []
    current = sp.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                     capture_output=True, text=True, check=True).stdout.strip()
    assert current == pre


def test_publish_non_git_repo_raises(tmp_path: Path) -> None:
    p = _project(path=tmp_path / 'nogit')
    with pytest.raises(M.PublishError, match='geen git-repo'):
        M.publish(p, M.Snapshot(head=None))


# --- github_commit_url ---

def test_github_commit_url_from_ssh_remote(tmp_path: Path) -> None:
    repo = tmp_path / 'r'
    _init_git_repo(repo)
    sp.run(['git', 'remote', 'add', 'origin', 'git@github.com:hpharmsen/bra1n-website.git'],
           cwd=str(repo), check=True)
    url = M.github_commit_url(repo, 'abc1234')
    assert url == 'https://github.com/hpharmsen/bra1n-website/commit/abc1234'


def test_github_commit_url_from_https_remote(tmp_path: Path) -> None:
    repo = tmp_path / 'r'
    _init_git_repo(repo)
    sp.run(['git', 'remote', 'add', 'origin', 'https://github.com/foo/bar'],
           cwd=str(repo), check=True)
    assert M.github_commit_url(repo, 'def5678') == 'https://github.com/foo/bar/commit/def5678'


def test_github_commit_url_non_github_returns_none(tmp_path: Path) -> None:
    repo = tmp_path / 'r'
    _init_git_repo(repo)
    sp.run(['git', 'remote', 'add', 'origin', 'git@gitlab.com:foo/bar.git'],
           cwd=str(repo), check=True)
    assert M.github_commit_url(repo, 'x') is None


# --- Reply templates ---

def test_reply_success_with_github_link() -> None:
    p = _project()
    out = M.reply_success(p, 'Klaar.', 'abc1234', ['index.html'],
                          'https://github.com/x/y/commit/abc1234')
    assert 'Klaar.' in out
    assert 'abc1234' in out
    assert 'https://example.com' in out  # live_url
    assert 'github.com/x/y' in out
    assert 'Gepusht' in out


def test_reply_success_without_github_link_falls_back_to_hash() -> None:
    p = _project()
    out = M.reply_success(p, 'Klaar.', 'abc1234', [], None)
    assert 'abc1234' in out
    assert 'Gepusht' in out


def test_reply_success_uses_alias_name() -> None:
    p = _project(name='juf-marijke')
    out = M.reply_success(p, 'Klaar.', 'h', [], None)
    assert 'Juf-marijke' in out


def test_reply_success_verify_url_overrides_project_live_url() -> None:
    p = _project()
    out = M.reply_success(p, 'Klaar.', 'h', [], None,
                          verify_url='https://example.com/about')
    assert 'Live: https://example.com/about' in out
    # generic live_url niet als aparte regel ernaast
    assert out.count('Live:') == 1


def test_reply_success_falls_back_to_project_live_url_without_verify_url() -> None:
    p = _project()
    out = M.reply_success(p, 'Klaar.', 'h', [], None)
    assert 'Live: https://example.com' in out


def test_reply_success_rejects_non_http_verify_url() -> None:
    p = _project()
    out = M.reply_success(p, 'Klaar.', 'h', [], None, verify_url='javascript:alert(1)')
    assert 'javascript:' not in out
    assert 'Live: https://example.com' in out


def test_reply_clarify_includes_question() -> None:
    p = _project()
    out = M.reply_clarify(p, 'Welke pagina?')
    assert 'Welke pagina?' in out
    assert 'Bra1n' in out


def test_reply_error_log_attached_flag() -> None:
    p = _project()
    assert 'Log bijgevoegd' in M.reply_error(p, 'Boom.')
    assert 'Log bijgevoegd' not in M.reply_error(p, 'Boom.', log_attached=False)


# --- process_message routing ---

def test_unknown_recipient_marks_error() -> None:
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed(recipient='unknown@harmsen.nl')
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'error')
    gmail.send_reply.assert_not_called()


def test_routes_to_correct_project(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('clarify', 'q?'))
    gmail = _gmail_mock()
    bra1n = _project(name='bra1n', email='bra1n@harmsen.nl')
    marijke = _project(name='juf-marijke', email='juf-marijke@harmsen.nl')
    projects = _projects_by_email(bra1n, marijke)
    msg = _parsed(recipient='juf-marijke@harmsen.nl')
    M.process_message(gmail, msg, projects)
    alias = gmail.send_reply.call_args.kwargs['alias_from']
    assert 'juf-marijke@harmsen.nl' in alias


def test_self_reply_blocked_no_reply() -> None:
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed(sender='bra1n@harmsen.nl', sender_raw='bra1n@harmsen.nl')
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'rejected')
    gmail.send_reply.assert_not_called()


def test_authentication_fails() -> None:
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed(auth=AUTH_FAIL)
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'rejected')
    body = gmail.send_reply.call_args.args[1]
    assert 'SPF' in body


def test_not_whitelisted() -> None:
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed(sender='attacker@evil.com', sender_raw='attacker@evil.com')
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'rejected')
    assert 'niet geautoriseerd' in gmail.send_reply.call_args.args[1]


def test_whitelist_isolation_between_projects() -> None:
    """Sender in whitelist van project A mag NIET mailen naar project B."""
    gmail = _gmail_mock()
    bra1n = _project(name='bra1n', email='bra1n@harmsen.nl',
                     extra_whitelist=['marketing@bra1n.example'])
    marijke = _project(name='juf-marijke', email='juf-marijke@harmsen.nl')
    projects = _projects_by_email(bra1n, marijke)
    msg = _parsed(sender='marketing@bra1n.example',
                  sender_raw='marketing@bra1n.example',
                  recipient='juf-marijke@harmsen.nl')
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'rejected')


def test_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(M, 'is_rate_limited', lambda: True)
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'rejected')
    assert 'rate limit' in gmail.send_reply.call_args.args[1]


def _setup_happy_mocks(monkeypatch: pytest.MonkeyPatch, outcome: Outcome) -> None:
    monkeypatch.setattr(M, 'is_rate_limited', lambda: False)
    monkeypatch.setattr(M, 'take_snapshot', lambda project: M.Snapshot(head=None))
    monkeypatch.setattr(M, 'save_image_attachments', lambda raw, dest: [])
    monkeypatch.setattr(M.claude_runner, 'run', lambda *a, **kw: outcome)


def test_clarify_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('clarify', 'Welke pagina?'))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    assert 'Welke pagina?' in gmail.send_reply.call_args.args[1]
    gmail.set_status.assert_called_once_with(msg, 'clarify')


def test_reject_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('reject', 'Destructief.'))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'error')
    assert 'Destructief' in gmail.send_reply.call_args.args[1]


def test_committed_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Tagline aangepast.'))
    monkeypatch.setattr(M, 'publish', lambda project, snap: ('abc1234', ['index.html']))
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: f'https://gh/c/{h}')
    monkeypatch.setattr(M, 'record_commit', lambda: None)
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'done')
    body = gmail.send_reply.call_args.args[1]
    assert 'Tagline aangepast' in body
    assert 'abc1234' in body


def test_committed_scope_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Probeerde iets.'))
    monkeypatch.setattr(M, 'publish',
                        MagicMock(side_effect=M.ScopeViolation(['.github/evil.yml'])))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'error')
    body = gmail.send_reply.call_args.args[1]
    assert '.github/evil.yml' in body
    assert 'Teruggedraaid' in body


def test_committed_no_actual_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Beweerde done.'))
    monkeypatch.setattr(M, 'publish',
                        MagicMock(side_effect=M.PublishError('Geen wijzigingen.')))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'error')
    assert 'Geen wijzigingen' in gmail.send_reply.call_args.args[1]


# --- publish_script (deploy step) ---

def _write_script(path: Path, body: str) -> None:
    path.write_text(f'#!/bin/bash\n{body}\n')
    path.chmod(0o755)


def test_run_publish_script_none_is_noop(tmp_path: Path) -> None:
    p = _project(path=tmp_path)
    M.run_publish_script(p, tmp_path / 'log')   # geen exception, geen log


def test_run_publish_script_success(tmp_path: Path) -> None:
    _write_script(tmp_path / 'deploy.sh', 'echo "deployed"')
    p = _project(path=tmp_path, publish_script='deploy.sh')
    log = tmp_path / 'd.log'
    M.run_publish_script(p, log)
    assert 'EXIT 0' in log.read_text()
    assert 'deployed' in log.read_text()


def test_run_publish_script_failure_raises(tmp_path: Path) -> None:
    _write_script(tmp_path / 'deploy.sh', 'echo boom >&2; exit 2')
    p = _project(path=tmp_path, publish_script='deploy.sh')
    log = tmp_path / 'd.log'
    with pytest.raises(M.DeployError) as exc:
        M.run_publish_script(p, log)
    assert 'exit 2' in str(exc.value)
    assert exc.value.log_path == log
    assert 'boom' in log.read_text()


def test_run_publish_script_missing_raises(tmp_path: Path) -> None:
    p = _project(path=tmp_path, publish_script='nope.sh')
    with pytest.raises(M.DeployError, match='ontbreekt'):
        M.run_publish_script(p, tmp_path / 'log')


def test_committed_deploy_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Klaar.'))
    monkeypatch.setattr(M, 'publish', lambda project, snap: ('abc1234', ['x.html']))
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: None)
    monkeypatch.setattr(M, 'record_commit', lambda: None)
    monkeypatch.setattr(M, 'LOG_DIR', tmp_path)
    deploy_calls = []
    monkeypatch.setattr(M, 'run_publish_script',
                        lambda project, log: deploy_calls.append(project.name))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project(publish_script='publish.sh'))
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    assert deploy_calls == ['bra1n']
    gmail.set_status.assert_called_once_with(msg, 'done')


def test_committed_deploy_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Klaar.'))
    monkeypatch.setattr(M, 'publish', lambda project, snap: ('abc1234', ['x.html']))
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: None)
    monkeypatch.setattr(M, 'record_commit', lambda: None)
    monkeypatch.setattr(M, 'LOG_DIR', tmp_path)

    deploy_log = tmp_path / 'dlog'
    deploy_log.write_text('rsync failed')

    def _fail(project, log):
        raise M.DeployError('publish_script faalde (exit 1): rsync: connection refused',
                            deploy_log)
    monkeypatch.setattr(M, 'run_publish_script', _fail)

    gmail = _gmail_mock()
    projects = _projects_by_email(_project(publish_script='publish.sh'))
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'error')
    body = gmail.send_reply.call_args.args[1]
    assert 'deploy faalde' in body
    assert 'abc1234'[:7] in body
    assert gmail.send_reply.call_args.kwargs['attachment_paths'] == [deploy_log]


def test_committed_deploy_skipped_when_no_publish_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bra1n-stijl project (geen publish_script) overslaat de deploy-step."""
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Klaar.'))
    monkeypatch.setattr(M, 'publish', lambda project, snap: ('abc1234', ['x.html']))
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: None)
    monkeypatch.setattr(M, 'record_commit', lambda: None)
    called = []
    monkeypatch.setattr(M, 'run_publish_script',
                        lambda project, log: called.append(True))
    gmail = _gmail_mock()
    projects = _projects_by_email(_project(publish_script=None))
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    assert called == []
    gmail.set_status.assert_called_once_with(msg, 'done')


# --- _reconcile_outcome (HEAD-moved <-> committed invariant) ---

def _second_commit(repo: Path) -> str:
    (repo / 'index.html').write_text('changed')
    sp.run(['git', 'commit', '-aq', '-m', 'change'], cwd=str(repo), check=True)
    return sp.run(['git', 'rev-parse', 'HEAD'], cwd=str(repo),
                  capture_output=True, text=True, check=True).stdout.strip()


def test_reconcile_head_moved_committed_passes_through(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    _second_commit(repo)
    p = _project(path=repo)
    out = Outcome('committed', 'Tagline.')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m1')
    assert result.kind == 'committed'
    assert result.summary == 'Tagline.'
    assert result.original_kind is None


def test_reconcile_head_moved_error_promoted(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    _second_commit(repo)
    p = _project(path=repo)
    out = Outcome('error', 'Claude exit 1: max turns')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m2')
    assert result.kind == 'committed'
    # Summary uit Claude's oorspronkelijke error-message
    assert 'max turns' in result.summary
    assert result.original_kind == 'error'


def test_reconcile_head_moved_clarify_promoted(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    _second_commit(repo)
    p = _project(path=repo)
    out = Outcome('clarify', 'Welke pagina?')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m3')
    assert result.kind == 'committed'
    assert result.original_kind == 'clarify'


def test_reconcile_head_moved_reject_promoted(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    _second_commit(repo)
    p = _project(path=repo)
    out = Outcome('reject', 'Destructief.')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m4')
    assert result.kind == 'committed'
    assert result.original_kind == 'reject'


def test_reconcile_head_not_moved_committed_demoted(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    p = _project(path=repo)
    out = Outcome('committed', 'Beweerde done.')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m5')
    assert result.kind == 'error'
    assert 'HEAD' in result.summary
    assert result.original_kind == 'committed'


def test_reconcile_head_not_moved_error_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    p = _project(path=repo)
    out = Outcome('error', 'Boom.')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m6')
    assert result.kind == 'error'
    assert result.summary == 'Boom.'
    assert result.original_kind is None


def test_reconcile_head_not_moved_clarify_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    p = _project(path=repo)
    out = Outcome('clarify', 'Welke pagina?')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m7')
    assert result.kind == 'clarify'


def test_reconcile_head_not_moved_reject_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    p = _project(path=repo)
    out = Outcome('reject', 'Geen wijziging gevraagd.')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m8')
    assert result.kind == 'reject'


def test_reconcile_skipped_when_no_git_repo(tmp_path: Path) -> None:
    """Geen git-repo: reconciliatie skippen, outcome ongewijzigd doorlaten."""
    p = _project(path=tmp_path / 'nogit')
    out = Outcome('committed', 'Klaar.')
    result = M._reconcile_outcome(M.Snapshot(head=None), p, out, msg_id='m9')
    assert result.kind == 'committed'
    assert result.summary == 'Klaar.'


def test_reconcile_promoted_summary_has_fallback(tmp_path: Path) -> None:
    """Als de error-summary leeg is, gebruik een nette generieke tekst."""
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    _second_commit(repo)
    p = _project(path=repo)
    out = Outcome('error', '')
    result = M._reconcile_outcome(M.Snapshot(head=pre), p, out, msg_id='m10')
    assert result.kind == 'committed'
    assert result.summary  # niet leeg


# --- process_message: orphan-commit redding via reconciliatie ---

def test_orphan_commit_promoted_and_published(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """End-to-end: Claude commit, runner geeft 'error' terug, reconcile maakt
    er committed van, publish() pusht 'm alsnog."""
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    monkeypatch.setattr(M, 'is_rate_limited', lambda: False)
    monkeypatch.setattr(M, 'save_image_attachments', lambda raw, dest: [])
    monkeypatch.setattr(M, 'take_snapshot', lambda project: M.Snapshot(head=pre))

    def _fake_run(*args, **kwargs):
        _second_commit(repo)   # Claude committeert tijdens run
        return Outcome('error', 'Claude exit 1: max turns')
    monkeypatch.setattr(M.claude_runner, 'run', _fake_run)
    monkeypatch.setattr(M, '_git_push', lambda r, fallback_head=None: None)
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: None)
    monkeypatch.setattr(M, 'record_commit', lambda: None)

    gmail = _gmail_mock()
    projects = _projects_by_email(_project(path=repo))
    msg = _parsed()
    M.process_message(gmail, msg, projects)

    gmail.set_status.assert_called_once_with(msg, 'done')


def test_committed_without_head_movement_demoted_to_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Claude claimt committed maar HEAD bewoog niet — wordt error, geen push."""
    repo = tmp_path / 'site'
    pre = _init_git_repo(repo)
    monkeypatch.setattr(M, 'is_rate_limited', lambda: False)
    monkeypatch.setattr(M, 'save_image_attachments', lambda raw, dest: [])
    monkeypatch.setattr(M, 'take_snapshot', lambda project: M.Snapshot(head=pre))
    monkeypatch.setattr(M.claude_runner, 'run',
                        lambda *a, **kw: Outcome('committed', 'Beweerde done.'))
    publish_called = []
    monkeypatch.setattr(M, 'publish',
                        lambda project, snap: publish_called.append(True))

    gmail = _gmail_mock()
    projects = _projects_by_email(_project(path=repo))
    msg = _parsed()
    M.process_message(gmail, msg, projects)

    assert publish_called == []
    gmail.set_status.assert_called_once_with(msg, 'error')


# --- send_reply failure mag mail niet eeuwig in queue houden ---

def test_set_status_runs_even_if_send_reply_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Als send_reply faalt (typisch: Gmail SSL idle-timeout na lange Claude-run),
    moet set_status alsnog draaien zodat de mail uit 'email-bot' verdwijnt en
    niet elke run opnieuw verwerkt wordt."""
    _setup_happy_mocks(monkeypatch, Outcome('clarify', 'Welke pagina?'))
    gmail = _gmail_mock()
    gmail.send_reply.side_effect = BrokenPipeError('idle SSL socket')
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'clarify')


def test_set_status_runs_even_if_send_reply_fails_after_commit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Belangrijkste geval uit de praktijk: committed-pad, send_reply gooit
    BrokenPipeError, status moet 'done' worden — anders triggert de volgende
    cron-tick een dubbele commit."""
    _setup_happy_mocks(monkeypatch, Outcome('committed', 'Tagline aangepast.'))
    monkeypatch.setattr(M, 'publish', lambda project, snap: ('abc1234', ['index.html']))
    monkeypatch.setattr(M, 'github_commit_url', lambda repo, h: None)
    monkeypatch.setattr(M, 'record_commit', lambda: None)
    gmail = _gmail_mock()
    gmail.send_reply.side_effect = BrokenPipeError('idle SSL socket')
    projects = _projects_by_email(_project())
    msg = _parsed()
    M.process_message(gmail, msg, projects)
    gmail.set_status.assert_called_once_with(msg, 'done')
