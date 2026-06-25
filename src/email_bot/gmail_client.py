"""Gmail API wrapper + mail parsing voor de email-bot."""
from __future__ import annotations

import base64
import io
import logging
import mimetypes
import re
import socket
import ssl
import time
import uuid
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {'JPEG', 'PNG', 'WEBP'}

# Lange Claude-sessies (15+ min) laten de Gmail SSL-socket idle dichtvallen.
# De eerstvolgende call krijgt dan BrokenPipeError of een ssl.SSLError. httplib2
# bouwt zelf een nieuwe connectie op bij de volgende request, dus een simpele
# retry is voldoende.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    BrokenPipeError, ConnectionError, ssl.SSLError, TimeoutError, socket.timeout,
)

log = logging.getLogger('email_bot.gmail')


def _execute(request, *, max_attempts: int = 3):
    """Execute een googleapiclient request met retries op transient fouten."""
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except _TRANSIENT_ERRORS as e:
            if attempt == max_attempts:
                raise
            log.warning(f'Gmail API transient error (poging {attempt}/{max_attempts}): {e!r}')
            time.sleep(2 ** (attempt - 1))


@dataclass
class Attachment:
    saved_path: Path | None
    reason: str | None
    original_filename: str
    mime_type: str
    size: int


@dataclass
class ParsedMessage:
    gmail_id: str
    thread_id: str
    sender: str
    sender_raw: str
    recipient: str    # eerste *@harmsen.nl in To:/Cc:/Delivered-To, lowercase. To gaat
                      # voor: dat is de intentie van de afzender, Delivered-To is het
                      # resultaat van forwarding/aliasing en mist dus de route-info.
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None
    references: str
    headers: dict[str, str]
    auth_results: str = ''
    attachments: list[Attachment] = field(default_factory=list)


def parse_raw(gmail_id: str, thread_id: str, raw_bytes: bytes) -> ParsedMessage:
    """Parse RFC822 bytes (van Gmail's format=raw response)."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    sender_raw = msg.get('From', '')
    recipient = _extract_recipient(msg)
    return ParsedMessage(
        gmail_id=gmail_id,
        thread_id=thread_id,
        sender=_extract_address(sender_raw).lower(),
        sender_raw=sender_raw,
        recipient=recipient,
        subject=msg.get('Subject', '') or '',
        body=_get_body_text(msg),
        message_id=msg.get('Message-ID', '') or '',
        in_reply_to=msg.get('In-Reply-To'),
        references=msg.get('References', '') or '',
        headers={k: v for k, v in msg.items()},
        auth_results=msg.get('Authentication-Results', '') or '',
    )


def _extract_address(raw: str) -> str:
    match = re.search(r'<([^>]+)>', raw)
    return (match.group(1) if match else raw).strip()


def _extract_recipient(msg: EmailMessage) -> str:
    """Eerste *@harmsen.nl uit To / Cc / Delivered-To, in die volgorde."""
    candidates: list[str] = []
    for header in ('To', 'Cc', 'Delivered-To'):
        for raw in msg.get_all(header) or []:
            for part in raw.split(','):
                addr = _extract_address(part).lower()
                if addr:
                    candidates.append(addr)
    for addr in candidates:
        if addr.endswith('@harmsen.nl'):
            return addr
    return candidates[0] if candidates else ''


def _get_body_text(msg: EmailMessage) -> str:
    body_part = msg.get_body(preferencelist=('plain', 'html'))
    if body_part is None:
        return ''
    content = body_part.get_content()
    if body_part.get_content_type() == 'text/html':
        return _strip_html(content)
    return content


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip += 1
        if tag in ('p', 'br', 'div', 'tr', 'li'):
            self._parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return ''.join(self._parts)


def _strip_html(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return parser.text().strip()


def is_authenticated(auth_results: str) -> bool:
    """SPF + DKIM + DMARC moeten alle drie 'pass' zijn.
    Interne Gmail-mail (afzender == jezelf via alias) krijgt geen Authentication-Results,
    die laten we door — de whitelist is de echte gate."""
    if not auth_results:
        return True
    found = {m.lower() for m in re.findall(r'\b(spf|dkim|dmarc)=pass\b', auth_results, re.IGNORECASE)}
    return found == {'spf', 'dkim', 'dmarc'}


def is_self_reply(parsed: ParsedMessage, project_email: str) -> bool:
    """Voorkom mail-loops: weiger als afzender == project-adres of als reply op
    een eerdere mail van dat adres."""
    addr = project_email.lower()
    if parsed.sender == addr or addr in parsed.sender_raw.lower():
        return True
    if parsed.in_reply_to and addr in parsed.in_reply_to.lower():
        return True
    return False


def save_image_attachments(msg: EmailMessage, dest_dir: Path) -> list[Attachment]:
    """Re-encode images via Pillow, regenereer filename naar uuid. Strip EXIF."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        filename = part.get_filename() or 'unknown'
        mime = part.get_content_type()
        payload = part.get_payload(decode=True) or b''
        size = len(payload)
        if size > MAX_ATTACHMENT_BYTES:
            out.append(Attachment(None, 'too large', filename, mime, size))
            continue
        if not mime.startswith('image/'):
            out.append(Attachment(None, 'not an image', filename, mime, size))
            continue
        try:
            with Image.open(io.BytesIO(payload)) as im:
                fmt = im.format or ''
                if fmt not in ALLOWED_IMAGE_FORMATS:
                    out.append(Attachment(None, f'format {fmt} not allowed', filename, mime, size))
                    continue
                ext = 'jpg' if fmt == 'JPEG' else fmt.lower()
                safe_path = dest_dir / f'{uuid.uuid4().hex}.{ext}'
                save_im = im.convert('RGB') if fmt == 'JPEG' else im
                save_im.save(safe_path, format=fmt)
        except (UnidentifiedImageError, OSError) as e:
            out.append(Attachment(None, f'invalid image: {e}', filename, mime, size))
            continue
        out.append(Attachment(safe_path, None, filename, mime, size))
    return out


def _with_quoted_original(body_text: str, parsed: ParsedMessage) -> str:
    original = (parsed.body or '').rstrip()
    if not original:
        return body_text
    quoted = '\n'.join('> ' + line for line in original.splitlines())
    date = parsed.headers.get('Date', '').strip()
    sender = parsed.sender_raw or parsed.sender
    intro = f'Op {date} schreef {sender}:' if date else f'{sender} schreef:'
    return f'{body_text}\n\n{intro}\n{quoted}\n'


def compose_reply(
    parsed: ParsedMessage,
    body_text: str,
    alias_from: str,
    attachment_paths: list[Path] | None = None,
) -> str:
    """RFC822 reply als base64url string, klaar voor Gmail send."""
    reply = EmailMessage()
    reply['To'] = parsed.sender_raw
    reply['From'] = alias_from
    reply['Reply-To'] = alias_from
    subject = parsed.subject or '(geen onderwerp)'
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'
    reply['Subject'] = subject
    reply['Message-ID'] = make_msgid(domain='harmsen.nl')
    if parsed.message_id:
        reply['In-Reply-To'] = parsed.message_id
        existing = parsed.references.strip()
        reply['References'] = f'{existing} {parsed.message_id}'.strip()
    reply.set_content(_with_quoted_original(body_text, parsed))
    for path in attachment_paths or []:
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or 'application/octet-stream').split('/', 1)
        reply.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return base64.urlsafe_b64encode(reply.as_bytes()).decode()


class GmailClient:
    """Dunne wrapper rond googleapiclient gmail service. State staat in Gmail-labels.
    Eén gedeelde label-set voor alle projecten — routering gebeurt op recipient-adres."""

    LABEL_INBOX = 'email-bot'
    STATUS_LABELS = {
        'done': 'email-bot-done',
        'clarify': 'email-bot-clarify',
        'error': 'email-bot-error',
        'rejected': 'email-bot-rejected',
    }

    def __init__(self, service) -> None:
        self._svc = service
        self._label_cache: dict[str, str] = {}

    def fetch_pending(self) -> list[ParsedMessage]:
        # Threads waar de gebruiker na een status-label nog gereply'd heeft
        # opnieuw als pending markeren — anders blijft een vervolg op een
        # done/clarify/error/rejected thread liggen.
        self._revive_replied_threads()
        # `label:email-bot` zonder `is:unread`: na verwerking is het inbox-label
        # weg (set_status verplaatst naar email-bot-done/error/etc.), dus dit
        # voorkomt her-verwerking. Bij een crash vóór set_status blijft de mail
        # zichtbaar voor retry op de volgende cron-tick.
        resp = _execute(self._svc.users().messages().list(
            userId='me', q='label:email-bot', maxResults=50,
        ))
        out: list[ParsedMessage] = []
        for ref in resp.get('messages', []):
            raw_msg = _execute(self._svc.users().messages().get(
                userId='me', id=ref['id'], format='raw',
            ))
            raw_bytes = base64.urlsafe_b64decode(raw_msg['raw'])
            out.append(parse_raw(raw_msg['id'], raw_msg['threadId'], raw_bytes))
        return out

    def fetch_raw_email(self, parsed: ParsedMessage) -> EmailMessage:
        """Voor attachment-extraction: haal opnieuw op en parse als EmailMessage."""
        raw_msg = _execute(self._svc.users().messages().get(
            userId='me', id=parsed.gmail_id, format='raw',
        ))
        raw_bytes = base64.urlsafe_b64decode(raw_msg['raw'])
        return BytesParser(policy=policy.default).parsebytes(raw_bytes)

    def send_reply(self, parsed: ParsedMessage, body_text: str, alias_from: str,
                   attachment_paths: list[Path] | None = None) -> None:
        raw = compose_reply(parsed, body_text, alias_from, attachment_paths)
        sent = _execute(self._svc.users().messages().send(
            userId='me', body={'raw': raw, 'threadId': parsed.thread_id},
        ))
        # Reply skipt de inbox: de status-labels op de oorspronkelijke mail
        # houden het thread vindbaar; INBOX zou alleen ruis zijn.
        _execute(self._svc.users().messages().modify(
            userId='me', id=sent['id'],
            body={'removeLabelIds': ['INBOX']},
        ))

    def set_status(self, parsed: ParsedMessage, status: str) -> None:
        target_id = self._label_id(self.STATUS_LABELS[status])
        inbox_id = self._label_id(self.LABEL_INBOX)
        _execute(self._svc.users().messages().modify(
            userId='me', id=parsed.gmail_id,
            body={
                'addLabelIds': [target_id, 'UNREAD'],
                'removeLabelIds': [inbox_id],
            },
        ))

    def _revive_replied_threads(self) -> None:
        """Detecteer threads met een status-label waar de gebruiker daarna nog
        gereply'd heeft, en zet ze terug op 'email-bot' zodat de volgende stap
        in fetch_pending ze oppakt."""
        status_label_names = list(self.STATUS_LABELS.values())
        status_label_ids = {self._label_id(name) for name in status_label_names}
        inbox_id = self._label_id(self.LABEL_INBOX)

        query = ' OR '.join(f'label:{name}' for name in status_label_names)
        try:
            resp = self._svc.users().threads().list(
                userId='me', q=query, maxResults=50,
            ).execute()
        except Exception:
            # Faalt niet de hele tick; normale fetch hieronder draait gewoon door.
            return

        for thread_ref in resp.get('threads', []):
            try:
                thread = self._svc.users().threads().get(
                    userId='me', id=thread_ref['id'], format='minimal',
                ).execute()
            except Exception:
                continue
            messages = thread.get('messages', [])
            if not messages:
                continue
            last = messages[-1]
            last_labels = set(last.get('labelIds', []))
            # SENT = bot's eigen reply; status-label = al door bot afgehandeld.
            if 'SENT' in last_labels or last_labels & status_label_ids:
                continue
            # Nieuwe user-reply: strip stale status-labels en hang email-bot
            # op de nieuwe message zodat hij in dezelfde tick opgepakt wordt.
            for m in messages[:-1]:
                to_remove = list(set(m.get('labelIds', [])) & status_label_ids)
                if to_remove:
                    self._svc.users().messages().modify(
                        userId='me', id=m['id'],
                        body={'removeLabelIds': to_remove},
                    ).execute()
            if inbox_id not in last_labels:
                self._svc.users().messages().modify(
                    userId='me', id=last['id'],
                    body={'addLabelIds': [inbox_id]},
                ).execute()

    def _label_id(self, name: str) -> str:
        if name in self._label_cache:
            return self._label_cache[name]
        labels = self._svc.users().labels().list(userId='me').execute().get('labels', [])
        for lbl in labels:
            if lbl['name'] == name:
                self._label_cache[name] = lbl['id']
                return lbl['id']
        raise RuntimeError(f'Label {name!r} bestaat niet. Run bootstrap.py.')
