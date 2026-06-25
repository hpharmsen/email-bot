# Plan: Orphan commits — Claude commit, runner ziet 't niet, niets gepusht

## Symptoom

Claude commit lokaal in de project-repo, maar `publish()` draait niet en de
commit wordt nooit gepusht. De gebruiker krijgt een misleidende `error`-mail
("Geen wijzigingen") of helemaal niks (bij send_reply broken pipe), terwijl het
werk gewoon in de werkdirectory staat.

Geobserveerd op 2026-06-25:

- Mail `19efe7e7f08d861b`: Claude maakte commit `99b72e9` (13:35), runner
  rapporteerde outcome `error`. Commit bleef lokaal. Retry-run zag een schone
  diff en faalde met `PublishError('Geen wijzigingen')`. Gebruiker kreeg
  error-mail; live site bleef stuk.
- Mail `19efe73fbe06f1a7`: idem met commits `45bbe15` (14:55) en `a6d3011`
  (15:20). Repeated re-processing tot handmatige interventie.

Beide commits droegen `Co-Authored-By: Claude Sonnet 4.6` — onmiskenbaar van de
bot zelf, niet van een parallelle hp-werksessie.

## Root cause hypothese

`claude_runner.run()` (`src/email_bot/claude_runner.py:228`) bepaalt de outcome
strikt op basis van Claude's JSON-output + exit-code. Er zijn drie paden waarop
Claude wél kan committen maar de runner een niet-`committed` outcome teruggeeft:

1. **Niet-nul exit na een geslaagde commit.** Bij `--max-turns` (200) of
   `--max-budget-usd` ($8) afgekapt, eindigt `claude -p` met returncode ≠ 0,
   ook als er al gecommit was in een eerdere turn. De runner mapt elke
   non-zero exit op `Outcome('error', ...)` (regels 262-283).

2. **Claude rapporteert `clarify` of `reject` ná te hebben gecommit.** De
   structured-output validatie (regels 285-294) kijkt alleen naar
   `structured['action']`. Als Claude na een commit alsnog twijfelt en
   `clarify` teruggeeft, slaat `process_message` (`main.py:377`) de
   publish-stap over. De commit blijft lokaal staan.

3. **Output parsing faalt.** Als de stdout om wat voor reden ook geen valide
   JSON is met `action`/`summary`, mappen we op `error` (regels 290-294) ongeacht
   git-state.

In alle drie de gevallen is de symptomatische uitkomst hetzelfde: HEAD is
verplaatst maar de orchestrator weet het niet, dus `publish()` draait niet,
deploy draait niet, en op de volgende cron-tick triggert de "Geen wijzigingen"
fallback.

## Architectuur-invariant

> **HEAD verplaatst ⇒ outcome is `committed`. Outcome `committed` ⇒ HEAD
> verplaatst.**

De runner kan deze invariant niet alleen handhaven — het is een afspraak tussen
runner en orchestrator. Beide kanten moeten 'm checken.

## Opties

### Optie A — Trust Git (voorkeur)

**Idee:** vergelijk HEAD vóór en na Claude. Als HEAD verplaatste, behandel dat
als `committed`, ongeacht wat Claude in z'n JSON zei. Andersom: als Claude
`committed` claimt maar HEAD niet verplaatste, behandel dat als `error`.

**Waar:** in `main.py`, vlak na `claude_runner.run(...)`. Niet in de runner zelf
— de runner kent het project-pad maar zou daar dan ook conclusies uit moeten
trekken; cleaner om dit als orchestrator-laag te houden.

```python
outcome = claude_runner.run(prompt, project.path, log_path)
post_head = _git_head(project.path) if (project.path / '.git').exists() else None
head_moved = post_head and snap.head and post_head != snap.head

if head_moved and outcome.kind != 'committed':
    log.warning(f'Claude commit ({snap.head[:7]}..{post_head[:7]}) maar outcome={outcome.kind}; '
                f'behandel als committed.')
    outcome = Outcome('committed', outcome.summary or 'Wijziging doorgevoerd.',
                      outcome.verify_url)

if outcome.kind == 'committed' and not head_moved:
    outcome = Outcome('error', 'Claude rapporteerde committed maar HEAD bewoog niet.')
```

**Pro:** dekt alle drie de cases. Eenvoudig. Geen dubbele bron-van-waarheid.

**Con:** verbergt mogelijk Claude-bugs (als Claude steeds verkeerd rapporteert
zien we 't niet meer). Mitigatie: `log.warning` + tellen hoe vaak dit pad
gebruikt wordt.

### Optie B — Hard rollback

**Idee:** als outcome ≠ `committed` maar HEAD bewoog, doe `git reset --hard
snap.head`. Forceer dat Claude's commit weggegooid wordt als Claude zelf zegt
"ik weet 't niet" / "afgewezen".

**Pro:** strikte semantiek: Claude's eigen oordeel telt.

**Con:** vernietigt werk. In de echte gevallen die ik vandaag zag was de
commit gewoon goed (CSS fix, JS fix) en wilde de gebruiker 'm live. Rollback
zou betekenen dat de gebruiker opnieuw moet mailen. Slechtere ervaring.

### Optie C — Strikter prompten

**Idee:** verduidelijk in de prompt dat Claude NA committen alleen
`{"action":"committed",...}` mag teruggeven, en bij twijfel niet moet
committen.

**Pro:** geen orchestrator-wijziging.

**Con:** Claude maakt al de fout ondanks vrij expliciete instructies. Prompt
engineering blijft fragiel; dekt geen exit-code-mismatch.

## Voorstel

**Optie A**, plus twee kleinere zaken:

1. **`publish()` accepteert geen `snap.head == post_head` meer als success.**
   Logica zit er al — `PublishError('Geen wijzigingen')` — maar moet niet meer
   getriggerd worden door dit specifieke pad. Optie A voorkomt dat door
   `outcome` te corrigeren vóór `publish()`.

2. **Logging op outcome-correctie.** Elke keer dat we Claude's outcome
   overrulen op basis van git-HEAD, log een `WARNING` met message-ID, oude
   en nieuwe HEAD, originele outcome. Zodat we kunnen meten of dit blijft
   gebeuren of dat Claude inmiddels netjes rapporteert.

## Stappen

1. `Outcome` dataclass uitbreiden met optionele `original_kind` zodat we de
   originele Claude-rapportage in de log kunnen tonen (defensief, niet
   functioneel nodig).
2. Helper `_reconcile_outcome(snap, project, outcome) -> Outcome` in `main.py`
   die de invariant afdwingt.
3. Aanroepen tussen `claude_runner.run(...)` en de outcome-branches.
4. Tests:
   - HEAD bewoog + outcome=`committed` → ongewijzigd doorlaten.
   - HEAD bewoog + outcome=`error` → gepromoveerd naar `committed` met
     `summary` uit Claude's error-message.
   - HEAD bewoog + outcome=`clarify` → gepromoveerd naar `committed`. (Vraag
     gaat verloren — accepteren, want werk staat er al.)
   - HEAD niet bewoog + outcome=`committed` → gedegradeerd naar `error`.
   - HEAD niet bewoog + outcome=`error/clarify/reject` → ongewijzigd.
   - Geen git-repo (`snap.head is None`) → reconciliatie skippen.
5. Cleanup van de "Geen wijzigingen" foutmelding in `publish()`: blijft staan
   als laatste vangnet, maar wordt in normaal gebruik niet meer geraakt.

## Out of scope (apart traject)

- `--max-turns` en `--max-budget-usd` overschrijdingen verminderen door Claude
  korter te laten verifiëren of het verificatie-budget aparter te maken.
- Per-mail Claude-cost rapportage in de reply (zou inzichtelijk maken hoe vaak
  Claude tegen z'n limiet aanloopt).
- Watchdog-mechanisme: cron-job die orphan commits detecteert (HEAD vooruit
  op origin/main maar geen `email-bot-done` thread in dezelfde tijdsperiode).
  Net iets te speculatief voor nu — eerst Optie A meten.

## Risico's

- **Optie A maakt het mogelijk dat een Claude-commit met scope-violation
  alsnog door publish() probeert te pushen.** Bestaande scope-check in
  `publish()` (FORBIDDEN_PREFIXES + ScopeViolation) blijft van kracht; daar
  verandert niks. Een Claude die de scope respecteert kan z'n commit niet
  meer "verstoppen" door 't outcome-veld; een Claude die de scope schendt
  wordt nog steeds door publish() teruggedraaid.
- **Claude commit per ongeluk verkeerd werk en rapporteert clarify omdat ie
  zelf twijfelt.** Met Optie A pushen we dat werk alsnog. Mitigatie: prompt
  blijft expliciet "bij twijfel: clarify, niet committen". Als 't toch
  gebeurt, kan de gebruiker via een vervolg-mail laten reverten.
