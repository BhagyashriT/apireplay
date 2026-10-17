# APIReplay demo

`bash demo/run.sh` proves the whole loop in ~15 seconds:

1. **Record** — the v1 demo service runs behind the capture proxy while
   `make_traffic.py` sends 7 requests covering every endpoint.
2. **Sanitize** — the pack is redacted (the demo `Authorization` header
   becomes `***REDACTED***`); the report still works because the demo
   service ignores it.
3. **Replay** — the sanitized pack is replayed against the v2 service.
4. **Diff** — v2 seeds exactly 3 regressions and the report catches all 3:
   - `POST /api/orders` with bad input returns **400** instead of v1's **422**,
   - `GET /api/users/{id}` renames **`full_name` → `name`**,
   - `GET /api/search` regresses from ~50 ms to ~600 ms (**latency flag**).

Outputs land in `demo/`: `pack.json`, `results.json`, `diff.json`,
`report.md`. Ports used: 18080 (v1), 18081 (v2), 18888 (proxy).
