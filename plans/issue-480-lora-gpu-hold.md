# #480 — GPU-Hold während LoRA-Training

Leser: die Person, die S4 implementiert; Entscheidung: eine LoRA darf die GPU
erst nach der vorhandenen Generierungsarbeit exklusiv nutzen, ohne wartende
Musiker oder den Worker-Status anzulügen. Grundlage ist Ruling B in #480.

## Ein Owner, keine zweite Wahrheit

Der **ACE-Step-Worker** besitzt eine pro Worker in Redis abgelegte
Training-Hold-Lease. Nur seine interne Reserve-/Train-Ausführung darf sie
anlegen, verlängern oder mit ihrem Token freigeben. Der Scheduler ist Leser und
Zulasser, nicht zweiter Besitzer: Heartbeat und `/loaded_models` spiegeln
dieselbe Lease nur. Das ist belastbarer als ein Scheduler-Flag: Der Worker weiß
auch nach einem verlorenen SSE-Consumer, ob sein Subprozess noch trainiert.
`ModelCache.acquire_for_use()` bleibt Eviction-Schutz; es wird nicht als Lock
umdefiniert.

Die Lease enthält eine nicht erratbare Inhaberkennung und hat eine kurze,
worker-seitig erneuerte TTL, abgeleitet von der bestehenden Heartbeat-Expiry
(heute 15 s). Beim Worker-Neustart wird sie zusammen mit dem bereits bereinigten
Queue-Zähler gelöscht. TTL ist nur das Netz für Prozessverlust, keine zweite
Trainings-Zeitgrenze.

## Ablauf

1. Das LoRA-Job wählt nach der vorhandenen Pool-Politik einen Online-Worker;
   es lädt noch kein Modell und startet noch kein Training. Es ruft dafür einen
   neuen authentisierten Worker-Reserve-Schritt **vor** `/load_model` und
   `/tasks/train_lora` auf; der heutige Train-Endpunkt käme dafür zu spät.
2. Der Job fragt diese Reservierung periodisch ab. Der Worker führt eine
   Redis-atomare Operation aus: nur wenn seine `queue_depth` null und keine
   Hold-Lease existiert, setzt er die Lease und gibt deren Token zurück. Die
   Generation-Zulassung benutzt denselben atomaren Schlüssel: entweder erhöht
   sie zuerst den Queue-Zähler (dann kann Reservierung nicht gewinnen), oder
   die Reservierung gewinnt zuerst (dann bleibt die Generation ohne INCR in
   Wartestellung). Damit gibt es kein Check-then-act-Fenster.
3. Polling (kein Pub/Sub) ist absichtlich: Redis hat heute kein zuverlässiges
   Frei-Ereignis, und ein kurzer benannter Poll-Intervall bleibt nach einem
   verlorenen Wake-up korrekt. Die Drain-Wartezeit ist genau die vorhandene
   `STALE_JOB_THRESHOLDS[JobType.LORA_TRAINING].queued_seconds`-Grenze (heute
   1100 s), kein S4-Timeout. Läuft sie aus, werden LoRA und Job mit „Generation
   queue did not drain before LoRA training could start“ fehlgeschlagen, ohne
   Lease; Generierungen laufen weiter. #479 behält seine getrennte Grenze und
   Heartbeat-Kadenz für einen bereits gestarteten Langläufer.
4. Nach Reservierung lädt der Job das Modell, übergibt den Token an
   `/tasks/train_lora` und konsumiert dessen Stream. Der Worker hält die Lease
   während Load und Task, erneuert sie lokal und gibt sie im `finally` bei Erfolg,
   Workerfehler oder Abbruch frei. Scheitert der Übergang davor, gibt der Job
   ausschließlich mit seinem Token frei. Ein Neustart löscht die alte Lease;
   falls er ohne sauberen Start verschwindet, verfällt sie.
5. Die Generation-Zulassung überspringt gehaltene Worker zugunsten anderer
   Kandidaten. Ist der gewählte Worker gehalten (oder der einzige), bleibt der
   Job `queued`, bis die atomare Zulassung gelingt; erst dann wird er `running`.
   Er erhält im bestehenden Job-Stream ein neues, nicht terminales
   `queue_reason`-Feld: `Waiting for LoRA training on this GPU.` Die UI zeigt
   diesen Grund neben dem vorhandenen Queued-Label/der Position, nicht im
   Fehlerfeld. Nach Zulassung wird der Grund gelöscht.

Bei genau einem GPU-Worker bedeutet die Lease folgerichtig: jede neue
Generierung bleibt mit diesem Queued-Grund sichtbar stehen, bis das Training
endet; sie wird nicht als laufende Generierung oder als kaputte GPU dargestellt.

## Beobachtung und Wiederherstellung

`build_state_payload()` und `/loaded_models` liefern zusätzlich den aktiven
Training-Hold (mindestens „LoRA training“, sinnvollerweise ohne fremde Daten).
Die Admin-/Pool-Projektion übernimmt ihn als Anzeige, nicht als Owner.
`worker_is_online()` bleibt GPU-/Heartbeat-Liveness und wird durch „training“
nicht zu offline; der Scheduler benutzt die Lease für Kapazität.

Es kommt kein Reaper hinzu: Der vorhandene Job-Reaper arbeitet weiter mit
Job-Heartbeats und den bestehenden Liveness-Signalen. #479 hält während eines
echten Trainings dessen Job-Heartbeat frisch. Fehlt der Worker-Heartbeat nach
Restart/Crash, greifen dessen TTL, die bestehende Restart-Grace und die normale
Terminalisierung/Reconciliation; die Worker-Startup-Bereinigung entfernt die
Lease sofort. S4 ändert weder Reaper-Takt noch Liveness-Policy.

## Tests nach Vertrag

| Vertragssatz | Beweis |
|---|---|
| Training beginnt nicht vor leerer Queue | Scheduler-/LoRA-Test mit kontrolliertem Redis: Reserve scheitert bei Tiefe >0, erst Decrement lässt `/load_model` und Train-Aufruf zu. |
| Keine Generation teilt die GPU danach | Paralleltest: Hold gesetzt → Zulassung hält Generation `queued` mit `queue_reason`; Release startet sie und leert den Grund. |
| Jede Freigabe ist sicher | Erfolg, Worker-Fehler, Cancellation vor/nach Task und Worker-Startup/TTL prüfen tokengebundene Freigabe bzw. Bereinigung. |
| Status ist ehrlich | Worker-Test prüft Hold in Heartbeat und `/loaded_models`; API-/Svelte-Test prüft Queued-Label mit Grund, im Ein-Worker-Fall ohne „running“. |
| Reaper erfindet keinen zweiten Zustand | Liveness-/Lifecycle-Test: Training-Heartbeat bleibt #479 überlassen; verschwundener Worker folgt der vorhandenen Grace/Reconciliation und die Hold-Lease blockiert nicht weiter. |

Vorhandene Fakes: `tests/test_scheduler.py` hat `_InMemoryRedis` mit
get/set/incr/decr und Worker-DB-Seeding; `tests/test_acestep_state.py`,
`tests/acestep_worker/test_heartbeat.py` sowie
`tests/test_acestep_worker_train_lora.py` nutzen `fakeredis`/`FakeAsyncRedis`.
Die Worker-Endpoint-Tests besitzen bereits einen blockierbaren Train-Runner;
die Job-Tests patchen `pick_worker`, HTTP und `_iterate_task_events`.

## Geplante Grenzen

Anfassen: Scheduler-/Redis-Zulassung und Generation-Statusübergang
(`src/songmaker_cli/scheduler.py`, `acestep_state.py`, `jobs/generation.py`),
LoRA-Reservierung (`jobs/lora_training.py`), Worker-Lease/Projektion und API
(`acestep_worker/wrapper.py`, `heartbeat.py`, `models.py`), Job-Response/
Migration und Queued-Anzeige (`db`, `api_models`, `jobs_api`, Frontend) sowie
die oben genannten gezielten Tests und die zwei Worker-Pool-Dokus.

Bewusst nicht: kein neuer Reaper, keine Änderung von `worker_liveness.py` oder
der Cache-Eviction-Semantik, kein Scheduler-Parallel-Owner und keine zweite
Zeitgrenze/Heartbeat-Kadenz/Epochen-Konfiguration. S3 #479 baut diese Grenzen
parallel; S4 konsumiert ihr Ergebnis statt es zu kopieren.
