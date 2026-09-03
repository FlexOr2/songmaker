# #518 – Codex-Co-Writer über die Abo-Anmeldung

**Leser/Entscheidung:** Builder und Reviewer bauen R3 ohne API-Key-Fallback,
Credential-Duplikat oder stillen Werkzeugeinsatz. Vertrag: #518; Schnitt und
Sicherheitsrahmen: #321; Vorbild: #497; Credential-Owner: #350.

## Befund und Schnitt

- #497 besitzt den gemeinsamen Spawn-Owner `agent_cli.run_cli_bounded`:
  gescrubbte Umgebung, begrenztes I/O, Prozessgruppen-Reap, `CliLineChannel`
  und private Arbeitsverzeichnisse. Codex ergänzt nur Konstanten,
  `cowriter/codex_cli_adapter.py`, den Dispatch-Zweig und seine Parser-/Flag-
  Regeln; kein zweiter Prozess- oder Prompt-Owner.
- `~/.codex/auth.json` hat nur `auth_mode`, `tokens.id_token`,
  `tokens.access_token`, `tokens.refresh_token`, `tokens.account_id` und
  `last_refresh` (Feldnamen, keine Werte). #350 spiegelt das Dokument
  in-place, hält `refresh_token` leer und nullt `OPENAI_API_KEY`.
- Der Dispatcher prüft allein, ob das gemountete
  `/home/songmaker/.codex/auth.json` ein nichtleeres String-
  `tokens.access_token` hat. Das ist der CLI-Weg; leeres/fehlendes/falsch
  geformtes Feld führt wie bisher zum API-Key-Weg. Ein vorhandenes, aber
  abgelaufenes Token bleibt CLI und fällt im Turn niemals auf HTTP zurück.

## Umsetzung

1. **Codex-Adapter:** Er flacht System und Nachrichten mit den bestehenden
   Claude-Helfern ab und übergibt sie als `stdin_payload` an
   `codex exec ... -`; es gibt keinen dritten Flacher und keine neue
   Prompt-Datei. Er liest JSONL über den gemeinsamen Kanal, sendet nur
   Assistant-Text und nach sauberem Abschluss genau ein `FinalEvent`.
2. **Gepinnter Werkzeug-Gate:** argv ist `codex exec --json --sandbox
   read-only --ignore-user-config --ignore-rules -c approval_policy="never"
   -c mcp_servers={} -` (plus Modell). `scrubbed_env()` entfernt
   `OPENAI_API_KEY`; `--ignore-user-config` und die leere `mcp_servers`-
   Tabelle verhindern konfiguriertes MCP. Read-only und never verhindern
   schreibende bzw. zu bestätigende Shell-Aktionen, sind aber kein Ersatz für
   den Stream-Gate: jede gemeldete Werkzeugausführung (insbesondere
   `command_execution`, MCP- oder Web-Werkzeug-Item) bricht ab mit
   `codex_cli_tool_call_blocked`, reapt und liefert kein Finale.
3. **JSON-Stream:** Das Host-Experiment vom 03.09.2026 (`env -u
   OPENAI_API_KEY codex exec --json` mit obigen Gates und „Antworte nur OK“)
   endete 0. Es sah ausschließlich `thread.started` (`thread_id`),
   `turn.started`, `item.completed` mit `item={id,text,type=agent_message}`
   und `turn.completed` mit `usage`; Textlänge 2. Der Parser erlaubt nur diese
   Reihenfolge/Formen und explizit harmloses Reasoning, das verworfen wird;
   unbekanntes, doppeltes oder unvollständiges Ereignis ist
   `codex_cli_stream_protocol_error`. Er protokolliert nie Event-Payloads.
4. **Fehler:** Ein strukturiertes `turn.failed` mit Auth-Markierung oder ein
   nicht erfolgreicher Runner mit Auth-Markierung in intern gehaltenem stderr
   wird `ProviderUnavailableError("codex", "cli_login_expired")`. Alle
   anderen Fehler sind `codex_cli_error`. Auth-Marker und stderr dienen nur
   der Klassifikation; Logs enthalten höchstens Returncode und stderr-Länge,
   nie dessen Text, Chat/SSE oder Exception-Text.
5. **Gemeinsamer #497-Reviewrest:** `agent_cli` löscht eine vorhandene
   Runner-Prompt-Datei vor dem terminalen Kanalschluss. Ein `OSError` beim
   Löschen darf den Consumer nicht hängen lassen: er wird zum fehlerhaften
   `CliRunOutcome`, dieser wird publiziert und der Kanal danach geschlossen;
   `FileNotFoundError` bleibt idempotent. Der Codex-stdin-Weg erzeugt keine
   solche Datei, beweist aber den gemeinsamen Runner-Vertrag.
6. **Dokumentations-Owner:** `docs/security.md` erhält Spiegel-,
   Refresh-/Fehler- und Nicht-Leak-Fakten; `docs/architecture.md` nur den
   Dispatch-/Adapter-Owner. Keine Kopie der Mirror-Feldtabelle; #350 bleibt
   deren Quelle.

## Tests, Abnahme und Requirement-Ritual

- `tests/test_codex_cli_adapter.py`: gefälschte JSONL-Sequenz liefert Text und
  ein Finale; argv, stdin, gescrubbte Umgebung, privater cwd und kein
  `OPENAI_API_KEY` sind gepinnt. Jede Werkzeugform, unbekanntes/defektes
  JSON, falsche Reihenfolge, zweites Ende, fehlendes Ende, Abbruch und
  Kanalüberlauf reapt und liefert kein Finale. `turn.failed`/stderr mit und
  ohne Auth-Marker beweisen `cli_login_expired` bzw. `codex_cli_error` ohne
  Diagnosetext im Log.
- `tests/test_cowriter_dispatch.py`: nichtleeres `access_token` nimmt Codex
  CLI vor HTTP; leeres/fehlendes/falsch geformtes Token nimmt den bisherigen
  Key-Pfad; vorhandenes abgelaufenes Token (auch mit Key) nimmt CLI und endet
  `cli_login_expired`, nie HTTP. Der Stream wird geschlossen weitergereicht.
- `tests/test_agent_cli.py`: der normale Prompt-Datei-Fall, `FileNotFoundError`
  und ein anderes `OSError` beweisen die Löschung-vor-Kanalschluss und dass
  der Kanal immer ein Terminalergebnis erhält.
- **REQ-Ritual:** `ACC-COWRITER-12` verknüpft den Codex-CLI-Alleinweg mit
  `REQ-COWRITER-09`; `ACC-COWRITER-13` verknüpft den benannten Codex-Fehler
  ohne HTTP-Fallback mit `REQ-COWRITER-11`. Beide markieren ihre
  Integrationstests; die Umsetzung aktualisiert Manifestlisten,
  `docs/PRODUCT.md`, Zähltest und `check_requirements`. `REQ-COWRITER-10`,
  `-12` und `-13` erhalten keine erfundene Kante: Default bzw. bestehender
  Gesprächs-/Persistenzpfad ändern sich nicht und werden nicht als bewiesen
  ausgegeben.
- Nach Deploy: ein Stack-Turn ohne `OPENAI_API_KEY`, ein simuliert
  abgelaufenes Spiegel-Login und ein gefälschtes Werkzeug-Event zeigen die
  drei benannten Ergebnisse.

## Bewusst nicht

Kein Admin-Schalter/UI (R2), kein MCP oder Songwerkzeug (R4), kein
Codex-CLI-Katalog (R5), keine Änderung an Judge/API-Calls oder Persistenz,
kein Refresh im Container und keine zweite Credential-Kopie.
