# email-bot

Multi-project e-mail bot. Mailt een wijziging naar `<project>@harmsen.nl`,
Claude Code voert 'm uit in de juiste project-folder, commit + pusht, en mailt
je een samenvatting in dezelfde thread.

Eén Gmail-account, één set labels (`email-bot`, `email-bot-done`,
`email-bot-clarify`, `email-bot-error`, `email-bot-rejected`), één
OAuth-token. Routering gebeurt op het ontvangende e-mailadres
(`bra1n@harmsen.nl` → project `bra1n`, `juf-marijke@harmsen.nl` → project
`juf-marijke`, etc.).

Draait lokaal via een externe cron-applicatie.

## Hoe het werkt

```
mail -> <project>@harmsen.nl
   -> Gmail filter (per project) zet label 'email-bot', skip inbox
   -> cron-tick polled Gmail API
   -> route op recipient -> juist project
   -> auth-check (SPF/DKIM/DMARC) -> whitelist -> rate-limit
   -> Claude Code (--add-dir <project_path>)
   -> scope-validate -> git push naar project-remote
   -> reply met commit-link + live-URL
```

State leeft uitsluitend in Gmail-labels.

## Projecten configureren

Bewerk `config.toml`:

```toml
[projects.bra1n]
path = "/Users/hp/ai/Bra1n/website"
email = "bra1n@harmsen.nl"
whitelist = ["michael@anderswinst.com"]
live_url = "https://hpharmsen.github.io/bra1n-website/"
alias_name = "Bra1n"

[projects.juf-marijke]
path = "/Users/hp/Sites/juf-marijke"
email = "juf-marijke@harmsen.nl"
whitelist = ["marijke@example.com"]
live_url = "https://juf-marijke.nl"
alias_name = "Juf Marijke"
publish_script = "publish.sh"
```

Velden:
- `path` — absolute pad naar project (moet git-repo zijn met push-bare remote)
- `email` — ontvangend adres, altijd `*@harmsen.nl`
- `whitelist` — extra senders; `hp@harmsen.nl` staat altijd impliciet in elke whitelist
- `live_url` — optioneel; wordt in succes-replies opgenomen
- `alias_name` — optioneel; display-naam in `From`-header (default: project-key gecapitaliseerd)
- `publish_script` — optioneel; relatief pad naar deploy-script dat na de git push
  wordt uitgevoerd (timeout 10 min). Bij falen volgt een error-reply met de log
  als bijlage. Laat weg als `git push` zelf al deployt (bv. GitHub Pages).

## Eenmalige setup

### 1. Google Cloud project + OAuth-client

1. https://console.cloud.google.com → Create Project "email-bot".
2. APIs & Services → Enable APIs → "Gmail API" → Enable.
3. OAuth consent screen → External → app name "email-bot", support+developer
   email `hp@harmsen.nl`, test user `hp@harmsen.nl`. Save (Testing-status volstaat).
4. Credentials → Create credentials → OAuth client ID → type "Desktop".
5. Download JSON → opslaan als `config/credentials.json`. `chmod 600`.

### 2. Install + bootstrap

```bash
cd ~/proj/email-bot
uv sync
uv run python bootstrap.py
```

`bootstrap.py` doet OAuth-consent, schrijft `config/token.json`, maakt de vijf
labels aan en voor elk project in `config.toml` een Gmail-filter
`to:<email> → label email-bot, skip inbox`. Idempotent — kan opnieuw na het
toevoegen van een project.

### 3. Per project: handmatige stappen

Voor elk nieuw project moet je een paar stappen handmatig doen (Gmail
verifieert verificatielinks bv. niet via API). `bootstrap.py` print deze lijst.

1. **Domein-alias** bij je provider: `<project>@harmsen.nl` forwarden naar
   `hp@harmsen.nl` (of catch-all aan laten staan).
2. **Gmail "Send mail as"**: Settings → Accounts → Add → vul `<project>@harmsen.nl`
   in met display-naam. Klik de verificatielink uit Gmail.
3. **Git remote + push-access**: zorg dat de project-folder een git-repo is met
   een remote waar de bot naartoe kan pushen. Test: `git push --dry-run origin main`.
4. **CLAUDE.md** in de project-folder (optioneel) — site-conventies, stijl, talen.

### 4. SSH-config voor `git push`

```
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    UseKeychain yes
    AddKeysToAgent yes
```

Eenmalig: `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`.

### 5. Cron-job

Voeg in je cron-app een job toe die periodiek (15 min) draait:

```bash
cd /Users/hp/proj/email-bot && uv run python -m email_bot.main
```

Test handmatig:
```bash
uv run python -m email_bot.main
tail -f logs/email_bot.log
```

## Gebruik

Stuur mail naar `<project>@harmsen.nl`. Binnen ~15-30 min komt een reply in
dezelfde thread met de samenvatting, commit-link, en live-URL (indien
geconfigureerd).

## Geweigerd

- Mail van niet-geverifieerde afzenders (SPF/DKIM/DMARC fail).
- Mail van afzenders die niet in de whitelist staan (`hp@harmsen.nl` altijd OK).
- Mail die `<project>@harmsen.nl` als afzender of in `In-Reply-To` heeft
  (loop-bescherming).
- Wijzigingen die `.git/`, `.github/`, `.claude/`, `CNAME` raken
  (defense-in-depth boven `--add-dir`, met automatische revert).
- Boven 6 succesvolle commits per uur (globaal over alle projecten).

## Configuratie

| Bestand | Doel |
|---|---|
| `config.toml` | Project-definities (commit) |
| `config/credentials.json` | Google OAuth client (gitignored) |
| `config/token.json` | OAuth refresh-token (gitignored, auto-geschreven) |
| `config/run.lock` | Lock-file voor concurrent runs |
| `config/state/rate.json` | Rate-limit history (1 uur sliding window) |
| `logs/` | Logs (gitignored) |

## Debug

1. `tail -f logs/email_bot.log`
2. Test handmatig: `uv run python -m email_bot.main`
3. Bij git push fail: `ssh -vT git@github.com` om keychain te checken.

## Tests

```bash
uv run python tests/main.py
```

## Architectuur

```
src/email_bot/
  main.py          orchestrator: lock + project-routing + auth + whitelist + rate + reply + scope
  projects.py      config.toml loader + Project dataclass
  gmail_client.py  Gmail API + RFC822 parsing + attachment re-encode + threading
  claude_runner.py subprocess wrapper rond `claude -p` met --json-schema output
```

Geen framework. Lock-file en rate-limit zijn JSON op disk (geen database).
State leeft in Gmail-labels.
