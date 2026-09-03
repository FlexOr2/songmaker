# #480 — GPU-Hold während LoRA-Training

Entscheidung: Ein ACE-Step-Worker lässt LoRA erst nach seinen bereits
zugelassenen Generierungen exklusiv trainieren. Während des Holds bleibt jede
nicht zugelassene Generierung ehrlich `queued`; sie teilt weder GPU noch
ARQ-Slot mit dem Training.

## Grenzen und belegte Ausgangslage

- Redis ist Owner der Occupancy. Der neue Hold ist
  `songmaker:acestep:hold:{worker_id}`; Heartbeat, `/loaded_models`, Admin und
  UI sind ausschließlich Projektionen. Kein Flag im Heartbeat-JSON und kein
  fusionierter Queue-/Hold-Key: `publish_once()` überschreibt das JSON alle 5 s
  (`src/acestep_worker/heartbeat.py:54-58`), der Queue-Key ist ein Integer
  ohne TTL (`src/songmaker_cli/acestep_state.py:76-87`).
- Es gibt keinen zweiten Reaper und keine S3-Timeränderung. Der einzige
  periodische Job-Reaper ist `stale_job_reaper_loop`
  (`src/songmaker_cli/lifecycle.py:434-437`); `ModelCache.acquire_for_use()`
  bleibt allein Eviction-Schutz (`src/acestep_worker/model_cache.py:208-215`).
- „Queue leer“ vor dem Training heißt exakt `queue_depth` des gewählten
  Workers ist 0; DB-/ARQ-queued Generierungen zählen nicht. Danach wartet nur
  eine Generate ohne erfolgreiches Admit.
- S4a baut nach #479, weil es dessen stabilen LoRA-Langläufervertrag konsumiert,
  ohne Zeitgrenzen, Heartbeat-Kadenz oder Epochen zu kopieren. S4b baut nach
  S4a und #481, weil #481 die Queued-Flächen in `SongDetailView`/`TakesList`
  verändert; es übernimmt deren gelandete Beschriftung statt sie zu duplizieren.

## S4a — Redis-Occupancy, Hold und Scheduling

### Vertrag

1. Reserve gewinnt nur atomar, wenn der Queue-Zähler des Workers fehlt oder 0
   und kein Hold besteht. Admit gewinnt nur atomar, wenn kein Hold besteht;
   es erhöht dann den Queue-Zähler. Zwei konkurrierende Aufrufe können nicht
   beide gewinnen.
2. `POST /gpu_hold/reserve` liefert `{token}` oder 409; `renew` und `release`
   akzeptieren nur denselben Token. Der Hold-Key enthält den nicht erratbaren
   Token, hat `EX = HeartbeatLoop`-TTL (heute 15 s), und jede Erneuerung hat
   die bestehende 5-s-Heartbeat-Kadenz (also strikt unter TTL).
3. Vor dem gebundenen Worker-Task erneuert nur der LoRA-Job seinen Hold (sein
   Tod lässt die TTL räumen). Ab Load/`train_lora` erneuert der Worker bis
   `finally`; `TrainLoraRequest.hold_token` ist Pflicht und ein alter Token
   nach Restart darf weder renewen noch Training starten. Worker-Startup löscht
   Queue- **und** Hold-Key; `/generate` liefert bei Hold 409.
4. Solange Reserve 409 liefert, bleibt LoRA `queued` und pollt begrenzt bis
   `STALE_JOB_THRESHOLDS[JobType.LORA_TRAINING].queued_seconds`; dann schlägt
   sie ohne Lease mit `Generation queue did not drain before LoRA training
   could start` fehl. Sie wird erst nach Reserve `running`; kein 1100-s-Wait
   ohne Heartbeat im bestehenden 300-s-RUNNING-Reaperfenster.
5. `pick_worker` liest den Hold-Key, nicht den 5-s-Heartbeat-Spiegel: freie
   Online-Worker gehen vor, alle gehaltenen Worker deferieren Generate. Der
   Wait-Pfad setzt `Job.queue_reason` auf `Waiting for LoRA training on this
   GPU.` und löscht ihn bei Admit, Fehler oder Cancel.
6. Generate bleibt bis erfolgreiches Lua-Admit in der DB `queued`. Bei Hold
   setzt der ARQ-Lauf den Grund, deferiert/re-enqueuet mit
   `GPU_HOLD_POLL_INTERVAL_SECONDS` (5 s) und gibt den Slot frei; erst nach
   erfolgreichem INCR wird er `running`. `music_max_jobs` und
   `arq_job_timeout` gelten damit nur für echte Arbeit, nicht für Hold-Warten.

### Dateien und Umsetzung

- `src/songmaker_cli/acestep_state.py`, `scheduler.py`: gleicher Lua-Text über
  Queue- und Hold-Key für Admit/Reserve/Renew/Release; Scheduler ist Zulasser,
  kein Hold-Owner, und `pick_worker` filtert den Redis-Hold. Ein Gleichheitstest
  sichert den paketübergreifenden Key-/Script-Vertrag.
- `src/acestep_worker/heartbeat.py`, `wrapper.py`, `models.py`: Hold-Key,
  Reserve/Renew/Release-Endpoints, tokengebundenes `train_lora`, 409-Generate,
  Startup-DEL und Worker-Renewal während Load/Task.
- `src/songmaker_cli/jobs/lora_training.py`, `jobs/generation.py`,
  `music_worker.py`, `constants.py`: queued Drain, Token-Übergabe,
  Admit-vor-RUNNING und benanntes ARQ-Defer; #479s eigene Uhren bleiben dort.
- `src/songmaker_cli/db/models.py`, `db/migrations/versions/*`,
  `db/queries/jobs.py`, `api_models/__init__.py` und `scripts/generate_types.py`:
  nullable `Job.queue_reason`, Migration, `JobResponse` und Frontend-Typ.
- `docs/acestep.md`: Redis-Tabelle ergänzt Hold-Key und 15-s-TTL: Worker/HTTP
  mutiert allein den Hold, der Scheduler führt den Generate-Admit aus und
  mutiert damit den Queue-Zähler. `docs/architecture.md`: Generate-Flow sagt:
  Lua-Admit führt INCR atomar aus; Hold → queued Defer, erst danach RUNNING.

### Tests und Done when

- `tests/test_acestep_state.py` und `tests/test_scheduler.py`: Occupancy gegen
  `fakeredis`/`eval`, nicht gegen das eval-lose `_InMemoryRedis`: beide
  Rennreihenfolgen, Token-Mismatch, TTL, Multi-Worker-Skip und kein INCR bei
  Hold.
- `tests/test_acestep_worker_train_lora.py`, `tests/acestep_worker/test_heartbeat.py`
  und `test_wrapper.py`: Reserve/Renew/Release, Startup-DEL, alter Token,
  `/generate` 409 sowie Release bei Erfolg, Fehler und Cancel.
- `tests/test_jobs_lora_training.py`, `tests/test_jobs.py`,
  `test_lifecycle_job_reaper.py` und `test_music_worker.py`: queued Drain
  überlebt den Reaper, Job-Tod räumt per TTL, Generate deferiert slotfrei und
  wird erst nach Admit RUNNING.
- Done when: alle sechs Vertragssätze, einschließlich Ein-GPU-Fall, sind an
  realen Grenzstellen bewiesen; der Reaper, `worker_liveness.py`, Cache-Schutz
  und #479s Konfiguration sind unverändert.

## S4b — Sichtbarkeit ist eine Projektion

### Vertrag

1. Der Job-Stream liefert bei jedem sichtbaren Wechsel `queue_reason` **und**
   `queue_position`; ein Grundwechsel bei weiterem `queued` wird emittiert.
2. `SongDetailView` zeigt den Grund neben dem gelandeten Queued-Label und der
   Position, niemals als Fehler. Ein gehaltener Worker bleibt online; das
   Pool-/Admin-Bild sagt Hold/LoRA-Training statt fälschlich Idle.
3. Keine Sichtbarkeitsfläche schreibt, verlängert oder räumt Redis. Fehlt die
   Projektion, bleibt die Redis-Entscheidung korrekt.

### Dateien und Umsetzung

- `src/songmaker_cli/jobs_api.py`, `api_models/__init__.py`,
  `db/queries/jobs.py`, `scripts/generate_types.py`: Stream-Snapshot trägt
  Position und Grund, und dessen Dirty-Check beobachtet beide Felder.
- `frontend/src/lib/components/SongDetailView.svelte`,
  `editor/TakesList.svelte` und ihre Tests: Queued-Grund neben Label/Position.
- `src/acestep_worker/wrapper.py`, `src/songmaker_cli/api_models/workers.py`,
  `admin_api.py`, `frontend/src/lib/components/WorkerPoolPanel.svelte` und
  Tests: `training_hold` nur spiegeln; `_derive_worker_status` bleibt online.
- `docs/acestep.md`: Redis-Tabelle kennzeichnet Heartbeat/Admin als
  Hold-Projektion. `docs/architecture.md`: Generate-Flow unterscheidet Redis-
  Entscheidung von Stream/UI-Projektion.

### Tests und Done when

- `tests/test_jobs_api.py` beweist Position+Grund im initialen und folgenden
  SSE-Ereignis ohne Statuswechsel; `tests/test_generate_types.py` sichert den
  generierten Vertrag.
- `SongDetailView.test.ts`, `TakesList.test.ts`, `WorkerPoolPanel.test.ts` und
  Worker/Admin-Tests beweisen Queued-Text, Position, Hold statt Idle und online
  statt offline.
- Done when: ein Ein-GPU-Hold ist über Stream, Song-Detail und Pool/Admin
  ehrlich sichtbar, während S4a weiterhin allein die Redis-Wahrheit besitzt.

## Review-Auflösung 1–10

1. Einarbeitet: Zwei-Key-Lua, Token/TTL und kein JSON-/Key-Merge (S4a 1–2).
2. Einarbeitet: Reserve/Renew/Release, Token-Prüfung, 409 und Startup-DEL (S4a 3).
3. Einarbeitet: Phasengetrenntes Renew, TTL-Crashpfad und alter Token (S4a 3).
4. Einarbeitet: queued ARQ-Defer, `GPU_HOLD_POLL_INTERVAL_SECONDS`, RUNNING nach INCR (S4a 6).
5. Einarbeitet: queued LoRA-Drain mit vorhandener Grenze, keine S3-Uhr (S4a 4).
6. Einarbeitet: persistenter einzelner Owner `Job.queue_reason` und Stream/UI (S4a 5; S4b 1–2).
7. Einarbeitet: Redis-Hold-Filter, Ein-GPU- und Admin-Projektion (S4a 5; S4b 2).
8. Einarbeitet: fakeredis/eval- und Lifecycle-/Defer-Testmatrix (S4a Tests).
9. Einarbeitet: beide genannten Doku-Deltas je Scheibe.
10. Einarbeitet: exakte Queue-/Drain-Definition in „Grenzen“ und S4a 4.
