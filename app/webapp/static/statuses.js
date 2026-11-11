/* The status words the PWA branches on, each declared once.
 *
 * The backend owns RUN_STATUSES (src/db/runs.py), TRANSCRIPTION_STATUSES
 * (src/db/transcription_status.py) and SYNC_STATUSES (src/analysis/source_funnel.py);
 * tests/webapp/test_status_vocabulary.py fails when a word here drifts from them.
 * LOAD_PHASE is the PWA's own: the `data-state` of a panel that loads data.
 * A view compares against these constants and never spells the word itself.
 */

// How a run ended; `running` is the only non-terminal word.
export const RUN_STATUS = Object.freeze({
  RUNNING: 'running',
  COMPLETED: 'completed',
  COMPLETED_WITH_ERRORS: 'completed_with_errors',
  FAILED: 'failed',
  ABANDONED: 'abandoned',
});

export function isRunning(status) { return status === RUN_STATUS.RUNNING; }
export function isFailed(status) { return status === RUN_STATUS.FAILED; }
export function isAbandoned(status) { return status === RUN_STATUS.ABANDONED; }
export function isCompletedWithErrors(status) { return status === RUN_STATUS.COMPLETED_WITH_ERRORS; }

// Where a downloaded voice note stands on its transcript.
export const VOICE_STATUS = Object.freeze({
  PENDING: 'pending',
  DONE: 'done',
  FAILED: 'failed',
  SKIPPED_OLD: 'skipped_old',
});

// How one source's sync ended inside a run's source funnel.
export const SYNC_STATUS = Object.freeze({
  SKIPPED: 'skipped',
  SUCCESS: 'success',
  FAILED: 'failed',
});

// The `data-state` of a panel that loads data: `stale` keeps the last good rows
// after a failed refresh, `error` has nothing to show.
export const LOAD_PHASE = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  EMPTY: 'empty',
  STALE: 'stale',
  ERROR: 'error',
});
