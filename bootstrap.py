"""Eenmalige setup (per machine + per project): OAuth + Gmail-labels + filters.

Idempotent: meerdere keren runnen kan, het overslaat wat al bestaat. Voeg een
nieuw project toe aan config.toml en run dit script opnieuw om er een filter
voor aan te maken.

Aanroep:
    uv run python bootstrap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Reuse de constanten van main, zodat alle paden op één plek staan.
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from email_bot.gmail_client import GmailClient
from email_bot.projects import load_projects

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_ROOT / 'config.toml'
CONFIG_DIR = PROJECT_ROOT / 'config'
CREDENTIALS_FILE = CONFIG_DIR / 'credentials.json'
TOKEN_FILE = CONFIG_DIR / 'token.json'

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.settings.basic',
]

LABELS = [GmailClient.LABEL_INBOX, *GmailClient.STATUS_LABELS.values()]


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    _check_credentials()
    creds = _do_oauth()
    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

    projects = load_projects(CONFIG_FILE)
    print(f'Geladen: {len(projects)} project(en) uit {CONFIG_FILE}')

    label_ids = {name: _ensure_label(service, name) for name in LABELS}
    inbox_id = label_ids[GmailClient.LABEL_INBOX]
    for project in projects.values():
        _ensure_filter(service, project.email, inbox_id)

    _print_manual_steps(projects.values())
    return 0


def _check_credentials() -> None:
    if CREDENTIALS_FILE.exists():
        return
    print(f'Ontbrekend: {CREDENTIALS_FILE}', file=sys.stderr)
    print('Maak een OAuth-client (type Desktop) in Google Cloud Console', file=sys.stderr)
    print('en sla de JSON op op die plek. Zie README voor de exacte stappen.', file=sys.stderr)
    sys.exit(2)


def _do_oauth() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        print('Bestaand token is geldig, sla OAuth-flow over.')
        return creds
    if creds and creds.expired and creds.refresh_token:
        print('Token verlopen, refresh...')
        creds.refresh(Request())
    else:
        print('Open browser voor OAuth-consent...')
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f'Token opgeslagen: {TOKEN_FILE}')
    return creds


def _ensure_label(service, name: str) -> str:
    for lbl in service.users().labels().list(userId='me').execute().get('labels', []):
        if lbl['name'] == name:
            print(f'Label bestaat al: {name}')
            return lbl['id']
    try:
        created = service.users().labels().create(
            userId='me',
            body={
                'name': name,
                'labelListVisibility': 'labelShow',
                'messageListVisibility': 'show',
            },
        ).execute()
        print(f'Label aangemaakt: {name}')
        return created['id']
    except HttpError as e:
        if e.resp.status == 409:
            return _ensure_label(service, name)
        raise


def _ensure_filter(service, project_email: str, inbox_label_id: str) -> None:
    existing = service.users().settings().filters().list(userId='me').execute().get('filter', [])
    for f in existing:
        crit = f.get('criteria', {})
        action = f.get('action', {})
        if (crit.get('to') == project_email
                and inbox_label_id in action.get('addLabelIds', [])):
            print(f'Filter bestaat al voor {project_email}')
            return
    service.users().settings().filters().create(
        userId='me',
        body={
            'criteria': {'to': project_email},
            'action': {
                'addLabelIds': [inbox_label_id],
                'removeLabelIds': ['INBOX'],
            },
        },
    ).execute()
    print(f'Filter aangemaakt: to:{project_email} -> label {GmailClient.LABEL_INBOX}, skip inbox')


def _print_manual_steps(projects) -> None:
    print()
    print('=' * 60)
    print('HANDMATIGE STAPPEN per project (niet via API te automatiseren):')
    print('=' * 60)
    for p in projects:
        print()
        print(f'[{p.name}]  (e-mail: {p.email}, pad: {p.path})')
        print(f'  1. Domein-alias: zet {p.email} forward naar hp@harmsen.nl bij je provider.')
        print(f'  2. Gmail Settings -> Accounts -> "Send mail as" -> Add {p.email}')
        print(f'     met display-naam {p.alias_name!r}. Klik de verificatielink uit Gmail.')
        print(f'  3. Zorg dat {p.path} een git-repo is met een push-bare remote.')
        print(f'     Test: cd {p.path} && git push --dry-run origin main')
        print(f'  4. Optioneel: maak {p.path}/CLAUDE.md aan met site-conventies.')
    print()
    print('Voeg in je cron-applicatie een job toe die periodiek (bijv. elke 15 min) draait:')
    print(f'  cd {PROJECT_ROOT} && uv run python -m email_bot.main')
    print()


if __name__ == '__main__':
    raise SystemExit(main())
