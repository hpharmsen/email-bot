"""Tests voor gmail_client parsing + auth + attachments + threading."""
from __future__ import annotations

import base64
import io
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from PIL import Image

from email_bot import gmail_client
from email_bot.gmail_client import (
    GmailClient,
    ParsedMessage,
    compose_reply,
    is_authenticated,
    is_self_reply,
    parse_raw,
    save_image_attachments,
)


AUTH_PASS = (
    'mx.google.com; spf=pass smtp.mailfrom=harmsen.nl; '
    'dkim=pass header.i=@harmsen.nl; dmarc=pass action=none'
)
AUTH_FAIL = 'mx.google.com; spf=fail; dkim=fail; dmarc=fail'

ALIAS_FROM = 'Bra1n <bra1n@harmsen.nl>'


def _make_eml(*, sender='HP <hp@harmsen.nl>', to='bra1n@harmsen.nl',
              subject='Test', body='Hello', auth=AUTH_PASS,
              content_type='text/plain', in_reply_to=None,
              references=None, attachment=None) -> bytes:
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = to
    msg['Subject'] = subject
    msg['Message-ID'] = '<test-001@example.com>'
    msg['Authentication-Results'] = auth
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
    if references:
        msg['References'] = references
    if content_type == 'text/html':
        msg.set_content(body, subtype='html')
    else:
        msg.set_content(body)
    if attachment:
        data, name, maintype, subtype = attachment
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def _jpeg(width=20, height=20) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color='red').save(buf, format='JPEG')
    return buf.getvalue()


def _png(width=20, height=20) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color='green').save(buf, format='PNG')
    return buf.getvalue()


def _parsed(**kw) -> ParsedMessage:
    defaults = dict(
        gmail_id='g', thread_id='t',
        sender='hp@harmsen.nl', sender_raw='HP <hp@harmsen.nl>',
        recipient='bra1n@harmsen.nl',
        subject='Test', body='hi',
        message_id='<a@b>', in_reply_to=None, references='',
        headers={}, auth_results=AUTH_PASS,
    )
    defaults.update(kw)
    return ParsedMessage(**defaults)


# --- parse_raw ---

def test_parse_plain_text_extracts_fields():
    raw = _make_eml(body='Wijzig tagline.')
    parsed = parse_raw('gid', 'tid', raw)
    assert parsed.gmail_id == 'gid'
    assert parsed.thread_id == 'tid'
    assert parsed.sender == 'hp@harmsen.nl'
    assert 'HP' in parsed.sender_raw
    assert parsed.subject == 'Test'
    assert 'Wijzig tagline' in parsed.body
    assert parsed.message_id == '<test-001@example.com>'


def test_parse_extracts_recipient():
    raw = _make_eml(to='juf-marijke@harmsen.nl')
    parsed = parse_raw('x', 'y', raw)
    assert parsed.recipient == 'juf-marijke@harmsen.nl'


def test_parse_recipient_prefers_harmsen_address():
    msg = EmailMessage()
    msg['From'] = 'sender@x'
    msg['To'] = 'unrelated@other.com, bra1n@harmsen.nl'
    msg['Subject'] = 'X'
    msg['Message-ID'] = '<a@b>'
    msg.set_content('body')
    parsed = parse_raw('x', 'y', msg.as_bytes())
    assert parsed.recipient == 'bra1n@harmsen.nl'


def test_parse_recipient_lowercased():
    raw = _make_eml(to='Bra1N@Harmsen.NL')
    parsed = parse_raw('x', 'y', raw)
    assert parsed.recipient == 'bra1n@harmsen.nl'


def test_parse_recipient_prefers_to_over_delivered_to():
    # Aliasing-geval: golfmeester@ is een alias die forward naar hp@.
    # Gmail zet dan Delivered-To: hp@, maar To: bevat de oorspronkelijke alias.
    # We willen routeren op intentie (To), niet op de forwarding-bestemming.
    msg = EmailMessage()
    msg['From'] = 'sender@x'
    msg['Delivered-To'] = 'hp@harmsen.nl'
    msg['To'] = 'golfmeester@harmsen.nl'
    msg['Subject'] = 'X'
    msg['Message-ID'] = '<a@b>'
    msg.set_content('body')
    parsed = parse_raw('x', 'y', msg.as_bytes())
    assert parsed.recipient == 'golfmeester@harmsen.nl'


def test_parse_lowercases_address_with_mixed_case():
    raw = _make_eml(sender='HP Harmsen <HP@Harmsen.NL>')
    parsed = parse_raw('x', 'y', raw)
    assert parsed.sender == 'hp@harmsen.nl'


def test_parse_html_only_body_stripped():
    raw = _make_eml(
        body='<html><body><p>Update kleur</p><script>alert(1)</script></body></html>',
        content_type='text/html',
    )
    parsed = parse_raw('x', 'y', raw)
    assert 'Update kleur' in parsed.body
    assert 'alert(1)' not in parsed.body
    assert '<p>' not in parsed.body


def test_parse_includes_auth_results():
    raw = _make_eml()
    parsed = parse_raw('x', 'y', raw)
    assert 'spf=pass' in parsed.auth_results


def test_parse_in_reply_to_and_references():
    raw = _make_eml(in_reply_to='<prev@x>', references='<root@x> <prev@x>')
    parsed = parse_raw('x', 'y', raw)
    assert parsed.in_reply_to == '<prev@x>'
    assert '<root@x>' in parsed.references


# --- is_authenticated ---

def test_authenticated_all_pass():
    assert is_authenticated(AUTH_PASS) is True


def test_authenticated_missing_one_returns_false():
    assert is_authenticated('spf=pass; dkim=pass') is False


def test_authenticated_any_fail_returns_false():
    assert is_authenticated('spf=fail; dkim=pass; dmarc=pass') is False


def test_authenticated_empty_returns_true():
    assert is_authenticated('') is True


# --- is_self_reply (per-project) ---

def test_self_reply_from_project_address():
    p = _parsed(sender='bra1n@harmsen.nl', sender_raw='bra1n@harmsen.nl')
    assert is_self_reply(p, 'bra1n@harmsen.nl') is True


def test_self_reply_from_user_returns_false():
    assert is_self_reply(_parsed(), 'bra1n@harmsen.nl') is False


def test_self_reply_in_reply_to_project_message():
    p = _parsed(in_reply_to='<msg-from-bra1n@harmsen.nl@harmsen.nl>')
    assert is_self_reply(p, 'bra1n@harmsen.nl') is True


def test_self_reply_other_project_not_self():
    """Mail van project A is geen self-reply voor project B."""
    p = _parsed(sender='bra1n@harmsen.nl', sender_raw='bra1n@harmsen.nl')
    assert is_self_reply(p, 'juf-marijke@harmsen.nl') is False


# --- save_image_attachments ---

def _parse_email(raw_bytes: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw_bytes)


def test_save_jpg_attachment(tmp_path):
    raw = _make_eml(attachment=(_jpeg(), 'headshot HP.jpg', 'image', 'jpeg'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert len(result) == 1
    att = result[0]
    assert att.saved_path is not None
    assert att.saved_path.exists()
    assert att.saved_path.suffix == '.jpg'
    assert 'headshot' not in att.saved_path.name
    assert ' ' not in att.saved_path.name


def test_save_png_attachment(tmp_path):
    raw = _make_eml(attachment=(_png(), 'logo.png', 'image', 'png'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert result[0].saved_path is not None
    assert result[0].saved_path.suffix == '.png'


def test_skip_non_image_attachment(tmp_path):
    raw = _make_eml(attachment=(b'%PDF-1.4 fake', 'doc.pdf', 'application', 'pdf'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert result[0].saved_path is None
    assert 'not an image' in result[0].reason


def test_skip_oversized_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_client, 'MAX_ATTACHMENT_BYTES', 100)
    raw = _make_eml(attachment=(_jpeg(width=100, height=100), 'big.jpg', 'image', 'jpeg'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert result[0].saved_path is None
    assert 'too large' in result[0].reason


def test_skip_invalid_image_bytes(tmp_path):
    raw = _make_eml(attachment=(b'\x00\x01 not an image', 'fake.jpg', 'image', 'jpeg'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert result[0].saved_path is None
    assert 'invalid image' in result[0].reason


def test_traversal_filename_does_not_escape_dest(tmp_path):
    raw = _make_eml(attachment=(_jpeg(), '../../../etc/passwd.jpg', 'image', 'jpeg'))
    result = save_image_attachments(_parse_email(raw), tmp_path)
    assert result[0].saved_path is not None
    assert result[0].saved_path.parent == tmp_path
    assert 'passwd' not in result[0].saved_path.name


# --- compose_reply ---

def _decode_b64(raw_b64: str) -> str:
    return base64.urlsafe_b64decode(raw_b64).decode()


def test_compose_reply_threading_headers():
    p = _parsed(message_id='<orig-001@example.com>', subject='Tagline aanpassing')
    decoded = _decode_b64(compose_reply(p, 'Done.', ALIAS_FROM))
    assert 'In-Reply-To: <orig-001@example.com>' in decoded
    assert 'References: <orig-001@example.com>' in decoded
    assert 'Subject: Re: Tagline aanpassing' in decoded
    assert 'From: Bra1n <bra1n@harmsen.nl>' in decoded


def test_compose_reply_uses_per_project_alias():
    p = _parsed(message_id='<orig@x>')
    decoded = _decode_b64(compose_reply(p, 'Klaar.', 'Juf Marijke <juf-marijke@harmsen.nl>'))
    assert 'From: Juf Marijke <juf-marijke@harmsen.nl>' in decoded


def test_compose_reply_no_double_re_prefix():
    p = _parsed(subject='Re: Already a reply', message_id='<orig@x>')
    decoded = _decode_b64(compose_reply(p, 'Done.', ALIAS_FROM))
    assert 'Subject: Re: Already a reply' in decoded
    assert 'Re: Re:' not in decoded


def test_compose_reply_appends_to_references_chain():
    p = _parsed(message_id='<latest@x>', references='<root@x> <mid@x>')
    decoded = _decode_b64(compose_reply(p, 'Done.', ALIAS_FROM))
    assert '<root@x>' in decoded
    assert '<mid@x>' in decoded
    assert '<latest@x>' in decoded


def test_compose_reply_with_attachment(tmp_path):
    img_path = tmp_path / 'preview.jpg'
    img_path.write_bytes(_jpeg())
    p = _parsed(message_id='<orig@x>')
    decoded = _decode_b64(compose_reply(p, 'See attached.', ALIAS_FROM, attachment_paths=[img_path]))
    assert 'filename="preview.jpg"' in decoded
    assert 'Content-Type: image/jpeg' in decoded


def test_compose_reply_handles_missing_subject():
    p = _parsed(subject='', message_id='<orig@x>')
    decoded = _decode_b64(compose_reply(p, 'Done.', ALIAS_FROM))
    assert 'Subject: Re: (geen onderwerp)' in decoded


def test_compose_reply_sets_reply_to_alias():
    p = _parsed(message_id='<orig@x>')
    decoded = _decode_b64(compose_reply(p, 'Done.', 'Juf Marijke <juf-marijke@harmsen.nl>'))
    assert 'Reply-To: Juf Marijke <juf-marijke@harmsen.nl>' in decoded


def test_compose_reply_quotes_original_body():
    p = _parsed(
        message_id='<orig@x>',
        sender_raw='HP <hp@harmsen.nl>',
        body='Eerste regel.\nTweede regel.',
        headers={'Date': 'Wed, 17 Jun 2026 14:16:06 +0200'},
    )
    decoded = _decode_b64(compose_reply(p, 'Klaar.', ALIAS_FROM))
    assert 'Klaar.' in decoded
    assert 'Op Wed, 17 Jun 2026 14:16:06 +0200 schreef HP <hp@harmsen.nl>:' in decoded
    assert '> Eerste regel.' in decoded
    assert '> Tweede regel.' in decoded


def test_compose_reply_handles_empty_body():
    p = _parsed(message_id='<orig@x>', body='', headers={})
    decoded = _decode_b64(compose_reply(p, 'Klaar.', ALIAS_FROM))
    assert 'Klaar.' in decoded
    assert 'schreef' not in decoded


# --- _revive_replied_threads ---

class _FakeGmail:
    """Minimal Gmail API mock for label/thread/message endpoints."""

    def __init__(self, threads_by_query: dict, threads_by_id: dict, labels: list[dict]):
        self._threads_by_query = threads_by_query
        self._threads_by_id = threads_by_id
        self._labels = labels
        self.modify_calls: list[dict] = []
        self.messages_list_calls: list[str] = []

    def users(self):
        return self

    def threads(self):
        return _ThreadsAPI(self)

    def messages(self):
        return _MessagesAPI(self)

    def labels(self):
        return _LabelsAPI(self._labels)


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _ThreadsAPI:
    def __init__(self, root: _FakeGmail):
        self._root = root

    def list(self, userId, q, maxResults):
        return _Exec({'threads': self._root._threads_by_query.get(q, [])})

    def get(self, userId, id, format):
        return _Exec(self._root._threads_by_id[id])


class _MessagesAPI:
    def __init__(self, root: _FakeGmail):
        self._root = root

    def list(self, userId, q, maxResults):
        self._root.messages_list_calls.append(q)
        return _Exec({'messages': []})

    def modify(self, userId, id, body):
        self._root.modify_calls.append({'id': id, 'body': body})
        return _Exec({})


class _LabelsAPI:
    def __init__(self, labels):
        self._labels = labels

    def list(self, userId):
        return _Exec({'labels': self._labels})


def _labels_setup() -> tuple[list[dict], dict[str, str]]:
    names = [GmailClient.LABEL_INBOX, *GmailClient.STATUS_LABELS.values()]
    labels = [{'name': n, 'id': f'id-{n}'} for n in names]
    ids = {n: f'id-{n}' for n in names}
    return labels, ids


def test_revive_strips_status_and_adds_inbox_label_on_new_reply():
    labels, ids = _labels_setup()
    status_query = ' OR '.join(f'label:{n}' for n in GmailClient.STATUS_LABELS.values())
    thread = {
        'messages': [
            {'id': 'm1', 'labelIds': [ids['email-bot-done']]},
            {'id': 'm2', 'labelIds': ['SENT']},               # bot's reply
            {'id': 'm3', 'labelIds': ['INBOX']},              # nieuwe user-reply
        ],
    }
    fake = _FakeGmail(
        threads_by_query={status_query: [{'id': 't1'}]},
        threads_by_id={'t1': thread},
        labels=labels,
    )
    client = GmailClient(fake)
    client._revive_replied_threads()

    mods = {(c['id'], frozenset(c['body'].get('removeLabelIds', [])),
             frozenset(c['body'].get('addLabelIds', []))) for c in fake.modify_calls}
    assert ('m1', frozenset({ids['email-bot-done']}), frozenset()) in mods
    assert ('m3', frozenset(), frozenset({ids['email-bot']})) in mods


def test_revive_skips_when_last_message_is_bot_reply():
    labels, ids = _labels_setup()
    status_query = ' OR '.join(f'label:{n}' for n in GmailClient.STATUS_LABELS.values())
    thread = {
        'messages': [
            {'id': 'm1', 'labelIds': [ids['email-bot-done']]},
            {'id': 'm2', 'labelIds': ['SENT']},
        ],
    }
    fake = _FakeGmail(
        threads_by_query={status_query: [{'id': 't1'}]},
        threads_by_id={'t1': thread},
        labels=labels,
    )
    client = GmailClient(fake)
    client._revive_replied_threads()
    assert fake.modify_calls == []


def test_revive_skips_when_last_message_already_has_status():
    labels, ids = _labels_setup()
    status_query = ' OR '.join(f'label:{n}' for n in GmailClient.STATUS_LABELS.values())
    thread = {
        'messages': [
            {'id': 'm1', 'labelIds': [ids['email-bot-done']]},
        ],
    }
    fake = _FakeGmail(
        threads_by_query={status_query: [{'id': 't1'}]},
        threads_by_id={'t1': thread},
        labels=labels,
    )
    client = GmailClient(fake)
    client._revive_replied_threads()
    assert fake.modify_calls == []


def test_revive_keeps_existing_inbox_label():
    """Als de Gmail-filter al email-bot heeft toegevoegd, alleen status strippen."""
    labels, ids = _labels_setup()
    status_query = ' OR '.join(f'label:{n}' for n in GmailClient.STATUS_LABELS.values())
    thread = {
        'messages': [
            {'id': 'm1', 'labelIds': [ids['email-bot-clarify']]},
            {'id': 'm2', 'labelIds': ['SENT']},
            {'id': 'm3', 'labelIds': [ids['email-bot']]},
        ],
    }
    fake = _FakeGmail(
        threads_by_query={status_query: [{'id': 't1'}]},
        threads_by_id={'t1': thread},
        labels=labels,
    )
    client = GmailClient(fake)
    client._revive_replied_threads()
    ids_modified = {c['id'] for c in fake.modify_calls}
    assert ids_modified == {'m1'}
    assert fake.modify_calls[0]['body'] == {'removeLabelIds': [ids['email-bot-clarify']]}


def test_revive_handles_all_status_labels():
    labels, ids = _labels_setup()
    status_query = ' OR '.join(f'label:{n}' for n in GmailClient.STATUS_LABELS.values())
    threads_by_id = {}
    thread_refs = []
    for i, status_name in enumerate(GmailClient.STATUS_LABELS.values()):
        tid = f't{i}'
        thread_refs.append({'id': tid})
        threads_by_id[tid] = {
            'messages': [
                {'id': f'{tid}-old', 'labelIds': [ids[status_name]]},
                {'id': f'{tid}-new', 'labelIds': ['INBOX']},
            ],
        }
    fake = _FakeGmail(
        threads_by_query={status_query: thread_refs},
        threads_by_id=threads_by_id,
        labels=labels,
    )
    client = GmailClient(fake)
    client._revive_replied_threads()
    revived_new = {c['id'] for c in fake.modify_calls
                   if ids['email-bot'] in c['body'].get('addLabelIds', [])}
    assert revived_new == {f't{i}-new' for i in range(len(GmailClient.STATUS_LABELS))}


# --- _execute: retry op transient connection errors ---

class _Req:
    """Mimics a googleapiclient request: .execute() called n times,
    failt de eerste `fail_n` keer met `error`, daarna succes."""

    def __init__(self, fail_n: int, error: Exception, result=None):
        self.fail_n = fail_n
        self.error = error
        self.result = result
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise self.error
        return self.result


def test_execute_retries_on_broken_pipe(monkeypatch):
    monkeypatch.setattr(gmail_client.time, 'sleep', lambda s: None)
    req = _Req(fail_n=1, error=BrokenPipeError('idle'), result={'ok': True})
    assert gmail_client._execute(req) == {'ok': True}
    assert req.calls == 2


def test_execute_retries_on_ssl_error(monkeypatch):
    monkeypatch.setattr(gmail_client.time, 'sleep', lambda s: None)
    req = _Req(fail_n=2, error=ssl.SSLError('boom'), result='ok')
    assert gmail_client._execute(req) == 'ok'
    assert req.calls == 3


def test_execute_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(gmail_client.time, 'sleep', lambda s: None)
    req = _Req(fail_n=5, error=BrokenPipeError('persistent'))
    with pytest.raises(BrokenPipeError):
        gmail_client._execute(req, max_attempts=3)
    assert req.calls == 3


def test_execute_does_not_retry_unrelated_errors(monkeypatch):
    monkeypatch.setattr(gmail_client.time, 'sleep', lambda s: None)
    req = _Req(fail_n=1, error=ValueError('not transient'))
    with pytest.raises(ValueError):
        gmail_client._execute(req)
    assert req.calls == 1
