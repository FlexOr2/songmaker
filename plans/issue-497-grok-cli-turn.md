# #497 – Grok-Co-Writer über die Abo-Anmeldung

**Leser/Entscheidung:** Builder und Reviewer setzen R1 um, ohne zweiten
Credential-Owner oder verdeckten API-Fallback. Vertrag: #497; E1/E2 und
Sicherheitsrahmen: #321; Sicherheits-Owner: `docs/security.md`.

## Befund, der den Schnitt bestimmt

- `~/.grok/auth.json` trägt Zugriff in `key`, Erneuerung in `refresh_token`
  und die Frist in `expires_at`; auf diesem Host sind es 21.600 Sekunden
  (sechs Stunden) ab `create_time`. `GROK_HOME` ist nicht gesetzt; gesetzt
  ersetzt es das gesamte Profil.
- Der #350-Owner existiert: `mirror_agent_cli_credentials.py` schreibt
  `grok.json` in-place, behält den Zugriff, leert `refresh_token` und lehnt
  unbekannte Felder ab. Path-Watcher (Änderung/Ersetzung) und 10-Minuten-Timer
  halten ihn aktuell; Compose mountet nur die read-only-Kopie.
- `grok --help` belegt `--prompt-file`, `--output-format streaming-json`,
  `--deny` und `--max-turns`. NDJSON enthält u.a. `text`, `tool_call`,
  `tool_call_update`, `available_commands`, `usage` und `end`.

## Umsetzung

1. **Ein gemeinsamer Spawn-Owner:** `agent_cli.py` erweitert
   `run_cli_bounded` um einen begrenzten stdout-Zeilenkanal und eine
   thread-sichere Abbruchanforderung. Nur der Runner besitzt bereinigte
   Umgebung, `start_new_session`, begrenztes Lesen/Schreiben, TERM/KILL der
   Prozessgruppe und Reap – auch bei Deadline, Kanalüberlauf oder Abbruch.
   Er erzeugt außerdem die private `0600`-Prompt-Datei und entfernt sie im
   `finally`. Der Adapter kann nur Abbruch anfordern und das Ergebnis
   abwarten; er verwendet weder `Popen` noch eigene Reap-Logik. Migriere die
   passende Claude-Probe/Prompt-Erzeugung auf diese Hilfsmittel, damit diese
   Mechanik einmal gehört; Claude behält Tool-Policy, Pooling und Parser.
2. **Kanal-/Abbruchvertrag:** Der Runner sendet ganze, größenbegrenzte
   stdout-Zeilen in einen begrenzten Kanal und schließt ihn erst mit seinem
   eindeutigen `CliRunOutcome`. Eine Consumer-Cancellation setzt die
   Abbruchanforderung und wartet den Runner ab. Bei Timeout, Abbruch,
   Überlauf, Spawn-/I/O-Fehler oder unvollständigem Prozess gibt es kein
   `FinalEvent`; bereits gepufferte, noch nicht gelesene Zeilen werden
   verworfen. Bei Policy-, Protokoll- oder CLI-Fehlern wirft der Adapter eine
   benannte `ProviderUnavailableError` statt ein Event zu liefern; bei
   Client-Cancellation liefert er nichts mehr.
3. **Grok-Adapter:** Neu `cowriter/grok_cli_adapter.py`. Er flacht System und
   Verlauf wie der bestehende Adapter ab, übergibt Inhalt ausschließlich per
   `--prompt-file` an `grok -p` und setzt Modell, `--output-format
   streaming-json`, `--deny '*'`, `--max-turns 1`, `--no-subagents` und
   `--disable-web-search`. Grok-spezifisch bleiben Flags und NDJSON-Parser;
   Gedanken-, Nutzungs-, Signatur- und stderr-Inhalte erreichen weder Chat
   noch Log.
4. **NDJSON-Vertrag und Werkzeug-Gate:** Jede Zeile muss UTF-8-JSON-Objekt
   mit String-`type` sein. Akzeptiert sind `text` mit String-`data` (wird
   `AssistantTextEvent`), `end` mit String-`stopReason`, `error` mit
   String-`message` sowie korrekt geformte, ignorierte `thought`, `usage`,
   `available_commands` und `plan`-Beobachtungen. Genau ein `end` plus
   erfolgreicher vollständiger Runner-Abschluss erzeugt danach ein
   `FinalEvent`. Ungültige Form, unbekannter Typ oder zweites `end` sind
   `ProviderUnavailableError(grok_cli_stream_protocol_error)`: Abbruch
   anfordern, reapen lassen, nie ein Finale. Jedes `tool_call` oder `tool_call_update`
   – auch mit sonst fehlerhafter Nutzlast – ist
   `ProviderUnavailableError(grok_cli_tool_call_blocked)` mit derselben
   Semantik. Eine angekündigte
   Werkzeugliste ist kein Aufruf. Es gibt weder MCP-Konfiguration noch
   Songwerkzeuge.
5. **Spiegel und Ablauf:** Kein neuer Watcher, keine Kopie des Refresh-Tokens:
   #350 bleibt alleiniger Host-Refresh-Owner. Ergänze den Verhaltensnachweis
   „aktualisiertes Host-`key` erscheint im Spiegel, `refresh_token` bleibt
   leer“. OIDC/401 oder abgelaufenes gespiegeltes Login wird
   `cli_login_expired`; der ausgewählte CLI-Turn endet dort, nie über Key.
6. **Dispatch bis R2:** `dispatch.py` entscheidet *vor* Turn-Beginn genau
   einen Weg: eingeloggte Grok-CLI zuerst, sonst `XAI_API_KEY`, sonst
   benannter Nichtverfügbarkeitsfehler. Ein CLI-Fehler startet nie HTTP.
   R1 ändert weder Judge- noch Katalog-Erreichbarkeit und berührt
   `cowriter/catalog.py` nicht; der CLI-Katalog gehört ausdrücklich zu R5.
7. **Sicherheitsdokument:** `docs/security.md` ergänzt nur sechs Stunden
   Zugriffsgültigkeit, #350 als Refresh-Owner und `cli_login_expired`; die
   vorhandene Redaktions- und Mount-Tabelle wird nicht dupliziert.

## Tests und Abnahme

- `tests/test_agent_cli.py`: 0600-Prompt, gescrubbte Umgebung,
  Prozessgruppen-Reap bei Abschluss/Timeout/Abbruch, Kanalgrenze und dass nur
  der Runner nach einer Adapter-Abbruchanforderung terminiert/reapet.
- Neuer `tests/test_grok_cli_adapter.py`: vollständiges argv enthält
  `--prompt-file`, `--deny '*'`, `--max-turns 1`, `--no-subagents` und
  `--disable-web-search`; gefälschtes NDJSON wird Text plus genau ein Finale.
  `tool_call` und `tool_call_update` werfen die benannte
  `ProviderUnavailableError`, reapen und liefern kein `FinalEvent`; ebenso
  ungültige/unklare Events, zwei `end`, Timeout und Abbruch. 401 wird
  `cli_login_expired`; keine Secret-/stderr-Leaks.
- `tests/test_cowriter_dispatch.py`: CLI geht Key vor, fehlende CLI nimmt den
  Key, fehlende beide ergeben den benannten Fehler, und ein CLI-Fehler wechselt
  innerhalb desselben Turns nie zu HTTP.
- `tests/test_conversation_api.py`: Ein Adapterfehler nutzt den bestehenden
  Provider-Fehlerpfad; weder leere Assistant-Nachricht noch Final-SSE wird
  persistiert/gesendet.
- `tests/test_mirror_agent_cli_credentials.py` und Install-Tests: jeder
  Host-Refresh ersetzt den gespiegelten Zugriff in-place, hält den Refresh
  leer und Path/Timer beobachten `~/.grok/auth.json`.
- Live nach Deploy: ein Stack-Turn ohne `XAI_API_KEY` liefert Text; ein
  abgelaufenes Login und künstliches `tool_call` sind sichtbar benannt.

## Flag-Experiment (03.09.2026)

`env -u XAI_API_KEY grok -p 'Antworte nur OK' --deny '*' --max-turns 1
--output-format streaming-json --cwd /tmp` endete Exit 0 nach einem Turn.
Trotz `--deny '*'` kündigte der Stream 24 eingebaute Werkzeuge an, enthielt
aber keinen `tool_call` und antwortete `Contract loaded.` und `OK`. Die Flag
ist akzeptiert, beweist aber keine werkzeugfreie Oberfläche; das Stream-Gate
ist deshalb zwingend.

## Dateien

Ändern: `src/songmaker_cli/agent_cli.py`, `claude/provider.py`, neuer
`cowriter/grok_cli_adapter.py`, `cowriter/dispatch.py`, die genannten Tests
und `docs/security.md`.

Bewusst nicht: `cowriter/catalog.py`, Judge-/Katalog-Erreichbarkeit und
CLI-Katalog (**R5**); MCP/Songwerkzeuge (**R4**); Admin-Schalter/UI (**R2**);
Codex-Turn (**R3**); API-Vertrags- oder Persistenzänderungen.
