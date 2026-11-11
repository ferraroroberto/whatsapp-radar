# Status vocabularies

Each lifecycle word the app stores or branches on is declared in one module and declared again in `.fleet.toml` as a `[[state_machines]]` entry, so the conventions ratchet's state rules (`state-literal`, `state-set-repeat`, `state-write`) stop reporting that module and keep reporting any other file that spells the word itself. A reader asks the owner module (a constant or a derive function) and never compares a bare string.

| Machine | Owner | Words | Derive | Read by |
|---|---|---|---|---|
| `run-lifecycle` | `src/db/runs.py` | `running`, then one terminal word: `completed`, `completed_with_errors`, `failed`, `abandoned` | `finalize_run` is the only terminal writer; a terminal row never moves | the pipeline, the CLI, the run.json writers in `app/webapp/runs.py` and `app/cli/runlog.py`, the audit and execution routers |
| `delivery-status` | `src/db/delivery_status.py` | one send ends `sent`, `skipped` or `failed`; a run's delivery adds `none`, `dry_run`, `offline`, `classifier_offline` | `run_delivery_ok` | `src/delivery/`, the pipeline result, the CLI exit code, the audit coverage gaps |
| `transcription-status` | `src/db/transcription_status.py` | `pending`, `done`, `failed`, `skipped_old` | `awaiting_transcript` (the cursor barrier) | `src/db/messages.py`, `src/analysis/transcription.py` |

The store refuses a word outside its set: `record_notification` and `RunFunnel` check the delivery words, and `mark_transcription` checks the transcription words and refuses `done` without a transcript.

## Copies across a process boundary

Two readers cannot import the Python module, so each holds a copy that a test keeps equal:

- The PWA's `app/webapp/static/statuses.js` holds `RUN_STATUS`, `VOICE_STATUS` and `SYNC_STATUS` (the words of `SYNC_STATUSES` in `src/analysis/source_funnel.py`). A view imports the constant or the `isRunning` / `isFailed` helpers.
- The sidecar's `src/sidecar/voice-status.js` holds the voice words it writes into the NDJSON buffer.

`tests/webapp/test_status_vocabulary.py` fails when either copy drifts from the Python set. `LOAD_PHASE` in `statuses.js` is the PWA's own vocabulary: the `data-state` of a panel that loads data (`loading`, `ready`, `empty`, `stale`, `error`), where `stale` keeps the last good rows after a failed refresh.

The sidecar state API also carries a derived `linking` flag (`SidecarStateInfo.is_linking`), so the Reconnect button asks the backend's state rather than comparing the `connecting` word.

## Adding a word or a reader

Add the word to the owner module and to each copy above, and handle it where the derive function or a view branches on it. A new reader imports the constant or calls the derive function; a bare lifecycle string in a comparison is a ratchet hit, and the baseline stays at zero.

Each machine has at least one test marked `state-machine-proof: <name>` that shows the owner refusing an illegal write or a drifted copy; `project-scaffolding`'s `scripts/state_scoreboard.py` counts those markers.
