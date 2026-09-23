# CheatVision Documentation Index

This folder contains the maintained operational documentation for CheatVision.

## Current cloud services

Diagnostics and tester file sharing are intentionally isolated:

| Service | Worker | Private R2 bucket |
|---|---|---|
| Support diagnostics | `cheatvision-support` | `cheatvision-support-logs` |
| Tester Share | `cheatvision-share` | `cheatvision-share` |

- Support: `https://cheatvision-support.sensoredrooster-com.workers.dev`
- Tester Share: `https://cheatvision-share.sensoredrooster-com.workers.dev`

The diagnostics bucket is only for redacted support bundles. The Tester Share bucket is only for project files exchanged with testers. They must not be pointed at the same bucket.

## Documentation map

- [README.md](README.md) — Main project overview and operating guide.
- [SUPPORT.md](SUPPORT.md) — Local-first support telemetry, privacy boundaries, and production diagnostics service.
- [TESTER_SHARE.md](TESTER_SHARE.md) — Authenticated tester/admin file portal behavior and maintenance.

## Deployment workflows

- `.github/workflows/deploy-support-worker.yml` — manual production diagnostics deployment.
- `.github/workflows/deploy-share-portal.yml` — manual Tester Share deployment.
- `.github/workflows/share-portal-check.yml` — syntax validation for the Tester Share Worker.

Normal app pushes do not redeploy the production Workers. Deployment workflows are intentionally manual after verification.

## Security notes

- Cloudflare API credentials live only in GitHub Actions secrets.
- R2 buckets are private.
- Support uploads happen only after explicit user confirmation.
- Tester Share plaintext passwords are not committed; only SHA-256 password hashes live in the Worker source.
- Rotate any exposed tester/admin password by replacing its hash and redeploying the share Worker.
