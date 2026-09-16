# Rolling telemetry

Live aim / gate / track samples written while CheatVision runs.

- Active session: `roll_YYYYMMDD-HHMMSS.partial.jsonl`
- Finished session (after quit or rotate): `roll_YYYYMMDD-HHMMSS.jsonl`

One JSON object per line (~20 Hz). Large path arrays are omitted so files stay sift-able.
These files are local-only (gitignored).
