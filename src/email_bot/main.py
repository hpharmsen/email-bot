"""Multi-project e-mail bot orchestrator.

Flow per cron-tick:
  fetch_pending -> route op recipient -> auth-check -> self-reply -> whitelist
  -> rate-limit -> snapshot -> claude -> scope-validate -> push -> deploy
  -> reply -> set_status.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import claude_runner
from .claude_runner import Outcome
from .gmail_client import (
    GmailClient,
    ParsedMessage,
    is_authenticated,
    is_self_reply,
    save_image_attachments,
)
from .projects import Project, load_projects

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = PROJECT_ROOT / 'config.toml'
CONFIG_DIR = PROJECT_ROOT / 'config'
TOKEN_FILE = CONFIG_DIR / 'token.json'
STATE_DIR = CONFIG_DIR / 'state'
RATE_FILE = STATE_DIR / 'rate.json'
LOCK_FILE = CONFIG_DIR / 'run.lock'
LOG_DIR = PROJECT_ROOT / 'logs'

# Globaal (over alle projecten heen).
RATE_LIMIT_PER_HOUR = 6
LOCK_STALE_AFTER = 15 * 60
GIT_REMOTE = 'origin'
GIT_BRANCH = 'main'

# Max wall-clock voor een publish_script (rsync, builds, etc.).
PUBLISH_TIMEOUT = 600

# Cron heeft minimale PATH; deploy-scripts (rsync, ssh, uv) verwachten /opt/homebrew.
_EXTRA_PATH_DIRS = ('/opt/homebrew/bin', '/usr/local/bin')

# Defense-in-depth boven --add-dir. Per-project zou kunnen, voor nu globaal.
FORBIDDEN_PREFIXES = ('.github/', '.git/', '.claude/', 'CNAME')

# Attachments worden onder <project>/assets/img/ opgeslagen — past bij de meeste
# static-site conventies. Configureerbaar maken kan later.
ATTACH_SUBPATH = Path('assets/img')

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.settings.basic',
]

log = logging.getLogger('email_bot')


# ---------------- Exceptions ----------------

class ScopeViolation(Exception):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f'paths outside scope: {paths}')


class PublishError(Exception):
    pass


class DeployError(Exception):
    """Raised when the project's publish_script fails."""

    def __init__(self, message: str, log_path: Path | None = None) -> None:
        self.log_path = log_path
        super().__init__(message)


# ---------------- Lock ----------------

@contextmanager
def _acquire_lock(path: Path = LOCK_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            if _lock_is_stale(path):
                log.warning('Lock stale, vrijgeven en opnieuw proberen.')
                path.unlink(missing_ok=True)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            else:
                yield False
                return
        os.ftruncate(fd, 0)
        os.write(fd, f'{os.getpid()} {int(time.time())}\n'.encode())
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            path.unlink(missing_ok=True)
        os.close(fd)


def _lock_is_stale(path: Path) -> bool:
    try:
        parts = path.read_text().strip().split()
        if len(parts) >= 2:
            return time.time() - int(parts[1]) > LOCK_STALE_AFTER
    except (OSError, ValueError):
        pass
    return False


# ---------------- Whitelist ----------------

def in_whitelist(sender: str, project: Project) -> bool:
    return sender.lower() in project.whitelist


# ---------------- Rate limit ----------------

def is_rate_limited(now: float | None = None, rate_file: Path = RATE_FILE) -> bool:
    now = now if now is not None else time.time()
    history = _load_rate(rate_file)
    recent = [t for t in history if t > now - 3600]
    return len(recent) >= RATE_LIMIT_PER_HOUR


def record_commit(now: float | None = None, rate_file: Path = RATE_FILE) -> None:
    now = now if now is not None else time.time()
    history = [t for t in _load_rate(rate_file) if t > now - 3600]
    history.append(now)
    rate_file.parent.mkdir(parents=True, exist_ok=True)
    rate_file.write_text(json.dumps(history))


def _load_rate(rate_file: Path) -> list[float]:
    if not rate_file.exists():
        return []
    try:
        return json.loads(rate_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []


# ---------------- Snapshot + scope ----------------

@dataclass
class Snapshot:
    head: str | None


def take_snapshot(project: Project) -> Snapshot:
    head = _git_head(project.path) if (project.path / '.git').exists() else None
    return Snapshot(head=head)


def _reconcile_outcome(snap: Snapshot, project: Project, outcome: Outcome,
                       msg_id: str = '') -> Outcome:
    """Handhaaf de invariant: HEAD verplaatst <=> outcome is 'committed'.

    Claude kan committen en alsnog niet-committed rapporteren (max-turns exit,
    twijfel-na-commit, output-parsing-fail). Andersom kan Claude 'committed'
    claimen zonder HEAD te bewegen. Git is hier de bron van waarheid."""
    if snap.head is None or not (project.path / '.git').exists():
        return outcome
    post_head = _git_head(project.path)
    head_moved = post_head != snap.head

    if head_moved and outcome.kind != 'committed':
        log.warning(
            f'[{msg_id}] HEAD verplaatst ({snap.head[:7]}..{post_head[:7]}) '
            f'maar Claude rapporteerde {outcome.kind!r}; behandel als committed.'
        )
        summary = outcome.summary.strip() or 'Wijziging doorgevoerd.'
        return Outcome('committed', summary, outcome.verify_url,
                       original_kind=outcome.kind)

    if outcome.kind == 'committed' and not head_moved:
        log.warning(
            f'[{msg_id}] Claude rapporteerde committed maar HEAD bewoog niet; '
            f'degradeer naar error.'
        )
        return Outcome('error',
                       'Claude rapporteerde committed maar HEAD bewoog niet.',
                       original_kind='committed')

    return outcome


def publish(project: Project, snap: Snapshot) -> tuple[str, list[str]]:
    """Returns (commit_hash, changed_files). Raises bij scope-violatie of
    wanneer er niets daadwerkelijk gewijzigd is."""
    if snap.head is None:
        raise PublishError(f'Project {project.name!r} is geen git-repo: {project.path}')

    current = _git_head(project.path)
    if current == snap.head:
        raise PublishError('Claude rapporteerde "committed" maar geen wijzigingen gevonden.')

    changed = _git_changed(project.path, snap.head, current)
    forbidden = [p for p in changed if p.startswith(FORBIDDEN_PREFIXES)]
    if forbidden:
        subprocess.run(['git', 'reset', '--hard', snap.head],
                       cwd=str(project.path), check=True, capture_output=True)
        raise ScopeViolation(forbidden)

    _git_push(project.path, fallback_head=snap.head)
    return _git_head(project.path), changed


def _git_head(repo: Path) -> str:
    proc = subprocess.run(['git', 'rev-parse', 'HEAD'],
                          cwd=str(repo), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _git_changed(repo: Path, from_hash: str, to_hash: str) -> list[str]:
    proc = subprocess.run(['git', 'diff', '--name-only', from_hash, to_hash],
                          cwd=str(repo), capture_output=True, text=True, check=True)
    return [line for line in proc.stdout.splitlines() if line]


def _git_push(repo: Path, fallback_head: str | None = None) -> None:
    # Rebase op remote om divergentie (bv. edits via GitHub UI) automatisch op te lossen.
    rebase = subprocess.run(['git', 'pull', '--rebase', GIT_REMOTE, GIT_BRANCH],
                            cwd=str(repo), capture_output=True, text=True, check=False)
    if rebase.returncode != 0:
        subprocess.run(['git', 'rebase', '--abort'],
                       cwd=str(repo), capture_output=True, check=False)
        if fallback_head:
            subprocess.run(['git', 'reset', '--hard', fallback_head],
                           cwd=str(repo), capture_output=True, check=False)
        raise PublishError(f'git pull --rebase faalde: {rebase.stderr.strip()}')
    proc = subprocess.run(['git', 'push', GIT_REMOTE, GIT_BRANCH],
                          cwd=str(repo), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PublishError(f'git push faalde: {proc.stderr.strip()}')


def _enriched_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = env.get('PATH', '').split(':') if env.get('PATH') else []
    for d in _EXTRA_PATH_DIRS:
        if d not in parts and Path(d).is_dir():
            parts.append(d)
    env['PATH'] = ':'.join(parts)
    return env


def run_publish_script(project: Project, log_path: Path) -> None:
    """Run het project-specifieke deploy-script. Raises DeployError op falen.
    Output (stdout+stderr) gaat naar log_path."""
    if not project.publish_script:
        return
    script = project.path / project.publish_script
    if not script.exists():
        raise DeployError(f'publish_script ontbreekt: {script}')
    try:
        result = subprocess.run(
            ['bash', str(script)],
            cwd=str(project.path), env=_enriched_env(),
            capture_output=True, text=True,
            timeout=PUBLISH_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        log_path.write_text(f'publish_script timed out after {PUBLISH_TIMEOUT}s\n')
        raise DeployError(f'publish_script timed out na {PUBLISH_TIMEOUT}s', log_path)

    log_path.write_text(
        f'EXIT {result.returncode}\n--- STDOUT ---\n{result.stdout}\n'
        f'--- STDERR ---\n{result.stderr}\n'
    )
    if result.returncode != 0:
        tail = ' | '.join((result.stderr or result.stdout or '').strip().splitlines()[-3:])
        raise DeployError(
            f'publish_script faalde (exit {result.returncode}): {tail or "geen output"}',
            log_path,
        )


def github_commit_url(repo: Path, commit_hash: str) -> str | None:
    """Leid een https://github.com/.../commit/<hash> URL af uit de git remote.
    Returns None als de remote geen GitHub-URL is."""
    try:
        proc = subprocess.run(['git', 'remote', 'get-url', GIT_REMOTE],
                              cwd=str(repo), capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = proc.stdout.strip()
    match = re.match(r'(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)', url)
    if not match:
        return None
    return f'https://github.com/{match.group(1)}/{match.group(2)}/commit/{commit_hash}'


# ---------------- Reply templates ----------------

def reply_success(project: Project, summary: str, commit_hash: str,
                  files: list[str], commit_url: str | None,
                  verify_url: str | None = None) -> str:
    lines = [summary, '']
    link = f' Commit: {commit_url}' if commit_url else f' Commit: {commit_hash}'
    lines.append(f'Gepusht naar remote.{link}')
    live = verify_url if verify_url and verify_url.startswith(('http://', 'https://')) else project.live_url
    if live:
        lines.append(f'Live: {live}')
    if files:
        lines.append('Bestanden: ' + ', '.join(files))
    lines.extend(['', 'Groet,', project.alias_name])
    return '\n'.join(lines)


def reply_clarify(project: Project, question: str) -> str:
    return f'Voordat ik dit uitvoer, eerst een vraag:\n\n{question}\n\nGroet,\n{project.alias_name}'


def reply_error(project: Project, reason: str, log_attached: bool = True) -> str:
    extra = '\n\nLog bijgevoegd voor diagnose.' if log_attached else ''
    return f'Verzoek niet uitgevoerd: {reason}{extra}\n\nGroet,\n{project.alias_name}'


def reply_rejected(project: Project, reason: str) -> str:
    return f'Verzoek niet uitgevoerd: {reason}\n\nGroet,\n{project.alias_name}'


# ---------------- OAuth ----------------

def gmail_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            TOKEN_FILE.chmod(0o600)
        else:
            raise RuntimeError('Token ongeldig; voer bootstrap.py opnieuw uit.')
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


# ---------------- Process per message ----------------

def _deliver(gmail: GmailClient, msg: ParsedMessage, project: Project,
             body: str, status: str,
             attachment_paths: list[Path] | None = None) -> None:
    """Stuur reply en zet status. Status wordt áltijd gezet, ook als de reply
    faalt. Anders houdt de mail zijn 'email-bot'-label en wordt hij elke run
    opnieuw door Claude verwerkt — typisch wanneer Gmail's SSL-connectie tijdens
    een lange Claude-sessie idle dichtvalt en send_reply met BrokenPipeError
    faalt. Beter een ontbrekende reply dan een oneindige loop."""
    try:
        gmail.send_reply(msg, body, alias_from=project.alias_from,
                         attachment_paths=attachment_paths)
    except Exception as e:
        log.exception(f'send_reply faalde voor {msg.gmail_id}; status wordt alsnog gezet: {e}')
    try:
        gmail.set_status(msg, status)
    except Exception as e:
        log.exception(f'set_status faalde voor {msg.gmail_id} (status={status}): {e}')


def process_message(gmail: GmailClient, msg: ParsedMessage,
                    projects_by_email: dict[str, Project]) -> None:
    log.info(f'Verwerk {msg.gmail_id} van {msg.sender} -> {msg.recipient}')

    project = projects_by_email.get(msg.recipient.lower())
    if project is None:
        log.warning(f'Geen project voor recipient {msg.recipient!r}; markeer als error.')
        try:
            gmail.set_status(msg, 'error')
        except Exception as e:
            log.exception(f'set_status faalde voor {msg.gmail_id}: {e}')
        return

    if is_self_reply(msg, project.email):
        log.warning(f'Self-reply geblokkeerd: {msg.sender}')
        try:
            gmail.set_status(msg, 'rejected')   # geen reply om loop te voorkomen
        except Exception as e:
            log.exception(f'set_status faalde voor {msg.gmail_id}: {e}')
        return

    if not is_authenticated(msg.auth_results):
        _deliver(gmail, msg, project,
                 reply_rejected(project, 'mail niet geverifieerd door SPF/DKIM/DMARC'),
                 'rejected')
        return

    if not in_whitelist(msg.sender, project):
        _deliver(gmail, msg, project,
                 reply_rejected(project, 'afzender niet geautoriseerd'),
                 'rejected')
        return

    if is_rate_limited():
        _deliver(gmail, msg, project,
                 reply_rejected(project,
                     f'rate limit bereikt (max {RATE_LIMIT_PER_HOUR}/uur), probeer over een uur'),
                 'rejected')
        return

    raw_email = gmail.fetch_raw_email(msg)
    attachments = save_image_attachments(raw_email, project.path / ATTACH_SUBPATH)
    saved = [a.saved_path for a in attachments if a.saved_path]

    log_path = LOG_DIR / f'{int(time.time())}-{msg.gmail_id}.log'
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    snap = take_snapshot(project)
    prompt = claude_runner.build_prompt(msg, project.name, project.path, saved,
                                        live_url=project.live_url)
    outcome = claude_runner.run(prompt, project.path, log_path)
    outcome = _reconcile_outcome(snap, project, outcome, msg_id=msg.gmail_id)

    if outcome.kind == 'clarify':
        _deliver(gmail, msg, project, reply_clarify(project, outcome.summary), 'clarify')
        return

    if outcome.kind in ('reject', 'error'):
        attach = [log_path] if log_path.exists() else None
        _deliver(gmail, msg, project,
                 reply_error(project, outcome.summary, log_attached=bool(attach)),
                 'error', attachment_paths=attach)
        return

    # committed: scope-validate + push
    try:
        commit_hash, files = publish(project, snap)
    except ScopeViolation as e:
        attach = [log_path] if log_path.exists() else None
        _deliver(gmail, msg, project,
                 reply_error(project,
                     f'Wijziging raakte verboden paden: {", ".join(e.paths)}. Teruggedraaid.'),
                 'error', attachment_paths=attach)
        return
    except PublishError as e:
        attach = [log_path] if log_path.exists() else None
        _deliver(gmail, msg, project, reply_error(project, str(e)),
                 'error', attachment_paths=attach)
        return

    record_commit()
    commit_url = github_commit_url(project.path, commit_hash)

    if project.publish_script:
        deploy_log = LOG_DIR / f'{int(time.time())}-{msg.gmail_id}-deploy.log'
        try:
            run_publish_script(project, deploy_log)
        except DeployError as e:
            attach = [e.log_path] if e.log_path and e.log_path.exists() else None
            _deliver(gmail, msg, project,
                     reply_error(project,
                         f'Commit gepusht ({commit_hash[:7]}), maar deploy faalde: {e}',
                         log_attached=bool(attach)),
                     'error', attachment_paths=attach)
            return

    _deliver(gmail, msg, project,
             reply_success(project, outcome.summary, commit_hash, files, commit_url,
                           verify_url=outcome.verify_url),
             'done')


# ---------------- Entry-point ----------------

def main() -> int:
    _setup_logging()
    with _acquire_lock() as got:
        if not got:
            log.info('Lock bezet — andere run actief, skip.')
            return 0
        try:
            projects_by_email = load_projects(CONFIG_FILE)
            log.info(f'{len(projects_by_email)} project(en) geladen.')
            service = gmail_service()
            gmail = GmailClient(service)
            for msg in gmail.fetch_pending():
                try:
                    process_message(gmail, msg, projects_by_email)
                except Exception as e:
                    log.exception(f'Onverwachte fout bij {msg.gmail_id}: {e}')
        except Exception as e:
            log.exception(f'Fatale fout in main: {e}')
            return 1
    return 0


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(LOG_DIR / 'email_bot.log'),
            logging.StreamHandler(sys.stdout),
        ],
    )


if __name__ == '__main__':
    raise SystemExit(main())
