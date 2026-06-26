# Hoe deze applicatie werkt

Multi-project e-mail bot. Een mail naar `<project>@yourdomain` wordt door
Claude Code uitgevoerd in de juiste project-folder, gecommit + gepusht, en
beantwoord in dezelfde thread.

## High-level flow

```
externe cron (elke ~15 min)
   └─> uv run python -m email_bot.main
         └─> main.main()
               ├─ acquire file-lock (config/run.lock)
               ├─ load_projects(config.toml)         # routing-tabel email -> Project
               ├─ gmail_service()                    # OAuth refresh
               └─ for msg in gmail.fetch_pending():
                     process_message(...)
```

Geen daemon, geen scheduler in proces. De "loop" is de externe cron-tick;
binnen één tick wordt iedere gelabelde mail één keer verwerkt.

## Per-message pijplijn (`process_message`)

`src/email_bot/main.py:384`

1. **Routeer** op recipient (`msg.recipient` → `Project`).
2. **Self-reply check** — voorkomt loops als de bot per ongeluk zichzelf mailt.
3. **Auth-check** — SPF/DKIM/DMARC moeten pass zijn (Gmail `Authentication-Results`-header).
4. **Whitelist** — `project.whitelist` (de owner uit `OWNER_EMAIL` zit er impliciet altijd in).
5. **Rate-limit** — max 6 succesvolle commits per uur (globaal, sliding window in `config/state/rate.json`).
6. **Attachments** — image-attachments worden gedecodeerd en opgeslagen onder `<project>/assets/img/`.
7. **Snapshot** — `git rev-parse HEAD` voor reconciliation achteraf.
8. **Claude Code** — zie hieronder.
9. **Reconcile** — vergelijk pre/post HEAD met `outcome.kind`; git is bron van waarheid.
10. **Scope-validate + push** — `publish()` weigert paden die `.git/`, `.github/`, `.claude/`, `CNAME` raken en doet `git reset --hard` bij overtreding.
11. **Optioneel deploy** — als `project.publish_script` is gezet wordt het uitgevoerd (max 10 min).
12. **Reply + label** — succes / clarify / reject / error reply gaat naar dezelfde thread, status-label wordt áltijd gezet (ook als de reply faalt, anders ontstaat een oneindige re-processing).

State leeft uitsluitend in Gmail-labels (`email-bot`, `email-bot-done`,
`email-bot-clarify`, `email-bot-error`, `email-bot-rejected`) + twee JSON-files
op disk (`run.lock`, `rate.json`).

## Hoe Claude Code wordt opgestart

Alles zit in `src/email_bot/claude_runner.py`. Aanroep is een **subprocess**,
headless, met structured JSON output.

### Resolve de binary

`_resolve_claude_bin()` zoekt in deze volgorde:
1. expliciet doorgegeven pad
2. `$CLAUDE_BIN` env var
3. `shutil.which('claude')`
4. Fallback-paden: `/Applications/cmux.app/Contents/Resources/bin/claude`,
   `/opt/homebrew/bin/claude`, `/usr/local/bin/claude`,
   `~/.claude/local/claude`

Cron heeft een minimale `PATH`; `_enrich_path()` voegt
`/opt/homebrew/bin` en `/usr/local/bin` toe zodat `node` (Claude's runtime)
en `git` bereikbaar zijn.

### Env

`config/.env` wordt geladen (`_load_env_file`) en in `env` ingevoegd. Met
name `CLAUDE_CODE_OAUTH_TOKEN` staat daar — daarmee logt Claude in zónder
keychain (nodig onder cron, en bestendiger dan de keychain-flow die periodiek
verloopt).

### Commando

`claude_runner.run()` bouwt deze CLI-aanroep:

```
claude -p <prompt>
       --permission-mode dontAsk
       --allowedTools <expliciete tool-allowlist>
       --add-dir <project_path>
       --max-turns 200
       --max-budget-usd 8.00
       --output-format json
       --json-schema <{action, summary, verify_url}>
       --no-session-persistence
       --debug-file <log_path>
```

Belangrijk:
- `--add-dir` is het sandbox-baspad. Defense-in-depth komt boven via de
  `FORBIDDEN_PREFIXES` scope-check in `publish()`.
- `--no-session-persistence` — elke mail is een schone sessie.
- `--json-schema` dwingt Claude tot één van drie acties (`committed`,
  `clarify`, `reject`); de schema-validatie gebeurt door Claude zelf.
- `start_new_session=True` op `Popen` zet Claude in een eigen process-group,
  zodat we na afloop `killpg` kunnen doen op orphans (background dev-server,
  `http.server` uit de verificatie-stap) zonder onze eigen Python te raken.

### Allowed tools

Limitatieve allowlist (`_ALLOWED_TOOLS` op regel 28). Samengevat:

| Categorie       | Tools |
|---|---|
| Bestanden       | `Read`, `Write`, `Edit` |
| Inspect         | `ls`, `find`, `grep`, `cat`, `head`, `tail`, `file`, `wc` |
| Git             | `git add/diff/status/commit/log/restore` (geen `push` — die doet de bot) |
| Verificatie     | `npm`, `npx`, `yarn`, `pnpm`, `node`, `python`, `python3`, `uv`, `pytest`, `make` |
| HTTP + server   | `curl`, `sleep`, `lsof`, `kill` |
| Browser         | `agent-browser` (screenshots, mobiel + desktop) |

`git push`, `rm`, netwerk-tools voor exfiltratie etc. staan **niet** in de
allowlist.

### Retry-beleid

Alleen retry op aantoonbaar transient API-fouten (`stream idle timeout`,
`connection error`, `econnreset`, `etimedout`, `overloaded`, `broken pipe`)
mits `usage.input_tokens == 0 && output_tokens == 0 && num_turns <= 1` — dus
gegarandeerd geen werk gedaan. Backoff 30s / 90s, max 2 retries. Reden:
voorkomt dubbele commits voor één mail.

### Outcome

`stdout` is JSON met `structured_output` (volgt het schema). Vier mogelijke
kinds: `committed`, `clarify`, `reject`, `error`. `error` is de bot zelf
(timeout, max-turns, parsing-fail); de andere drie komen van Claude.

## Zit er een agentic loop in?

**In de bot zelf: nee.** `main.main()` doet één pass over `fetch_pending()`
en eindigt. De herhaling is een externe cron-job (15 min) — dat is dus
extern aan deze codebase.

**Binnen Claude Code: ja.** Claude doet zijn standaard agentic-loop
(think → tool-call → observe → ...) tot het een JSON-antwoord teruggeeft of
één van de stop-condities raakt:

- `--max-turns 200` (`DEFAULT_MAX_TURNS`)
- `--max-budget-usd 8.00` (`DEFAULT_BUDGET_USD`)
- `--max-turns` hit produceert `subtype=error_max_turns` → bot mapt naar
  een `error` outcome met de hint "splits het verzoek op".
- Wall-clock timeout `1500s` (`DEFAULT_TIMEOUT`) op de Python-`Popen.communicate`.

De prompt (`build_prompt`) duwt Claude expliciet door een 4-staps
verificatie-cyclus vóór commit (zelf-review van de diff, tests draaien indien
aanwezig, browser-render check op desktop + mobiel via `agent-browser`,
dan pas `git add` + `git commit`). Die verificatie zit dus in de prompt,
niet in Python.

## Welke skills worden geladen?

**Geen skills worden expliciet door de bot geladen.** Er is geen
`--skill`-flag of `Skill`-tool in de allowlist, geen `.claude/`-folder in
deze repo (`.claude/` staat zelfs op de scope-blacklist voor wijzigingen),
en geen verwijzing naar skill-files in `claude_runner.py`.

Wat Claude wél meekrijgt aan project-context:

1. **`CLAUDE.md` van het doelproject** (niet van email-bot zelf). `build_prompt`
   checkt `project_path / 'CLAUDE.md'` en voegt, als die bestaat, de regel
   `- Volg <pad>/CLAUDE.md voor stijl en conventies.` aan de instructies toe.
   Claude leest die file zelf via de `Read`-tool wanneer relevant.
2. **De prompt zelf** (`build_prompt` in `claude_runner.py:156`) — bevat
   `VERIFIED SENDER`, `SUBJECT`, bijlage-paden, `LIVE-URL BASIS`, de
   `UNTRUSTED USER CONTENT`-markers rond de mailbody, en de verplichte
   verificatie-stappen.
3. **Globale en user-level CLAUDE Code config** (alles wat in
   `~/.claude/` staat) wordt door de Claude-binary zelf geladen — dat valt
   buiten deze applicatie en is niet project-specifiek.

Kortom: deze bot rust volledig op (a) de hardcoded prompt, (b) de
expliciete tool-allowlist, en (c) optioneel een `CLAUDE.md` in het
doelproject. Geen skills, geen MCP-servers, geen sessie-persistence.

## Belangrijke bestanden

| Bestand | Rol |
|---|---|
| `src/email_bot/main.py` | orchestrator: lock, routing, auth, whitelist, rate-limit, publish, reply |
| `src/email_bot/claude_runner.py` | subprocess-wrapper rond `claude -p`, prompt-bouw, retry, JSON-parsing |
| `src/email_bot/gmail_client.py` | Gmail API + RFC822 parsing + attachment re-encode + threading |
| `src/email_bot/projects.py` | `config.toml` loader + `Project` dataclass |
| `config.toml` | project-definities |
| `config/.env` | `CLAUDE_CODE_OAUTH_TOKEN` (door `claude_runner` ingevoegd in env) |
| `config/token.json` | Gmail OAuth refresh-token |
| `config/run.lock` | file-lock tegen overlappende cron-runs |
| `config/state/rate.json` | sliding-window commit-historie |
| `logs/email_bot.log` | hoofd-log; per-message debug-log in `logs/<ts>-<msg_id>.log` |
