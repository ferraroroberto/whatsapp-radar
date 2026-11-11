// The transcription status words the sidecar writes on a voice note's record.
// The Python reader owns the full set (src/db/transcription_status.py) and
// tests/webapp/test_status_vocabulary.py fails when this copy drifts from it.
export const VOICE_STATUS = Object.freeze({
  PENDING: "pending",
  DONE: "done",
  FAILED: "failed",
  SKIPPED_OLD: "skipped_old",
});
