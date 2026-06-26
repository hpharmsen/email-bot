# email-bot

Send a plain e-mail to `<project>@yourdomain` → [Claude Code](https://claude.com/claude-code)
applies the change in the matching project directory, commits, pushes, and
replies in the same thread with a summary, commit link, and live URL.

One Gmail account, one OAuth token, multiple projects routed by recipient
address.

## Flow

```
e-mail → <project>@yourdomain
   → Gmail filter labels it 'email-bot', skips inbox
   → cron tick polls Gmail API
   → routed by recipient → matching project
   → auth check (SPF/DKIM/DMARC) → whitelist → rate limit
   → Claude Code runs with --add-dir <project_path>
   → scope-validate → git push to the project's remote
   → optional deploy script
   → reply with commit link + live URL
```

State lives entirely in Gmail labels (`email-bot`, `email-bot-done`,
`email-bot-clarify`, `email-bot-error`, `email-bot-rejected`) plus two
JSON files on disk (`run.lock`, `rate.json`). No database.

The "loop" is an external cron tick — there is no daemon. Each tick
processes every labelled message exactly once and exits.

## Requirements

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv)
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) authenticated
  (either via keychain, or `CLAUDE_CODE_OAUTH_TOKEN` in `config/.env` —
  more reliable under cron)
- A Gmail account with API access
- Git push access (SSH key in agent/keychain) to each project's remote
- A cron-style scheduler (cron, launchd, an external CI, etc.) to invoke
  the bot every 5–15 minutes

## Setup

### 1. Google Cloud project + OAuth client

1. Create a project at https://console.cloud.google.com.
2. **APIs & Services → Enable APIs** → enable **Gmail API**.
3. **OAuth consent screen** → External, app name "email-bot", add yourself
   as a test user. Testing status is enough.
4. **Credentials → Create credentials → OAuth client ID** → type "Desktop".
5. Download the JSON and save it as `config/credentials.json`, then
   `chmod 600 config/credentials.json`.

### 2. Configure projects

Copy the template and edit it for your projects:

```bash
cp config.toml.example config.toml
```

`config.toml` is gitignored. Each project entry:

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | Absolute path to the project (must be a git repo with a pushable remote) |
| `email` | yes | Recipient address that routes to this project |
| `whitelist` | no | Extra senders allowed to mail this project. The owner (see `OWNER_EMAIL` in `src/email_bot/main.py`) is always implicitly whitelisted |
| `live_url` | no | Included in success replies |
| `alias_name` | no | Display name for the `From` header (default: capitalised project key) |
| `publish_script` | no | Path (relative to the project) of a deploy script run after a successful push |

### 3. Install + bootstrap

```bash
uv sync
uv run python bootstrap.py
```

`bootstrap.py` is idempotent. It does the OAuth consent flow, writes
`config/token.json`, creates the five Gmail labels, and adds a filter
per project (`to:<email> → label email-bot, skip inbox`). Re-run it any
time you add a project.

### 4. Per-project manual steps

For each new project, do these once (Gmail's API doesn't cover them):

1. **Domain alias** at your DNS / mail provider: forward
   `<project>@yourdomain` to the Gmail account.
2. **Gmail "Send mail as"**: Settings → Accounts → Add →
   `<project>@yourdomain` with the display name. Click the verification
   link Gmail sends.
3. **Git remote with push access**. Verify with
   `git push --dry-run origin main`.
4. **`CLAUDE.md`** (optional) in the project — style, conventions,
   languages.

### 5. SSH config for `git push` under cron

Cron has no agent by default; pin your key like this in `~/.ssh/config`:

```
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    UseKeychain yes
    AddKeysToAgent yes
```

One-time on macOS: `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`.

### 6. Schedule it

Run every 5–15 minutes via cron / launchd / any scheduler:

```bash
cd /absolute/path/to/email-bot && uv run python -m email_bot.main
```

A file lock prevents overlapping runs.

## Usage

Send mail to `<project>@yourdomain`. Within one or two cron ticks you'll
get a threaded reply with the change summary, commit link, and
(if configured) live URL.

If the request is ambiguous, the bot replies with a clarifying question
and waits for your answer — keep replying in the same thread.

## What gets rejected

- Mail from unverified senders (SPF / DKIM / DMARC fail).
- Mail from senders not in the whitelist (owner is always allowed).
- Mail from `<project>@yourdomain` itself or with that address in
  `In-Reply-To` (loop protection).
- Changes that touch `.git/`, `.github/`, `.claude/`, `CNAME`, `*.sh`,
  or `pyproject.toml` (defense-in-depth on top of `--add-dir`, with an
  automatic `git reset --hard`).
- More than 6 successful commits per hour across all projects.

## Files

| File | Purpose |
|---|---|
| `config.toml.example` | Template; copy to `config.toml` (gitignored) |
| `config/credentials.json` | Google OAuth client (gitignored) |
| `config/token.json` | OAuth refresh token (gitignored, auto-written) |
| `config/.env` | Optional `CLAUDE_CODE_OAUTH_TOKEN` for headless cron use |
| `config/run.lock` | File lock against overlapping cron runs |
| `config/state/rate.json` | Rate-limit sliding window |
| `logs/` | Per-run debug logs (gitignored) |

## Architecture

```
src/email_bot/
  main.py          orchestrator: lock + routing + auth + whitelist + rate + reply + scope
  projects.py      config.toml loader + Project dataclass
  gmail_client.py  Gmail API + RFC822 parsing + attachment re-encode + threading
  claude_runner.py subprocess wrapper around `claude -p`, retry-with-backoff, JSON parsing
```

No framework. No database. Lock file and rate limit are JSON on disk;
state lives in Gmail labels.

## Tests

```bash
uv run python tests/main.py
```

## Debug

- `tail -f logs/email_bot.log` — main log
- `logs/<unix-ts>-<gmail-id>.log` — per-message Claude debug output
- Manual run: `uv run python -m email_bot.main`
- SSH push fails: `ssh -vT git@github.com` to verify the key is loaded

## Why this exists

Sometimes a small site change is faster to describe in a sentence than
to context-switch into the editor. The bot collapses "I want X" → live
deploy into one e-mail thread. The verification gate (self-review,
tests if present, mobile + desktop screenshots via `agent-browser`)
runs inside the Claude prompt before any commit lands.
