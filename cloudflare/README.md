# Cloudflare Services

CheatVision uses two production Cloudflare Workers and two separate private R2 buckets.

## 1. Support diagnostics

- Worker source: `cloudflare/support-worker/src/index.js`
- Wrangler config: `cloudflare/support-worker/wrangler.jsonc`
- Worker: `cheatvision-support`
- URL: `https://cheatvision-support.sensoredrooster-com.workers.dev`
- Health: `https://cheatvision-support.sensoredrooster-com.workers.dev/health`
- Upload: `https://cheatvision-support.sensoredrooster-com.workers.dev/upload`
- R2 bucket: `cheatvision-support-logs`
- Deploy workflow: `.github/workflows/deploy-support-worker.yml`

Purpose: receive redacted support ZIPs created by the application after explicit user confirmation.

## 2. Tester Share

- Worker source: `cloudflare/share-worker/src/index.js`
- Wrangler config: `cloudflare/share-worker/wrangler.jsonc`
- Worker: `cheatvision-share`
- URL: `https://cheatvision-share.sensoredrooster-com.workers.dev`
- Health: `https://cheatvision-share.sensoredrooster-com.workers.dev/health`
- R2 bucket: `cheatvision-share`
- Deploy workflow: `.github/workflows/deploy-share-portal.yml`
- Syntax check: `.github/workflows/share-portal-check.yml`

Purpose: authenticated file sharing between the developer and testers.

Authentication uses a dedicated D1 database (`cheatvision-share-auth`). The manual deploy workflow creates the database if needed and applies its versioned schema before deploying the Worker. R2 continues to hold the shared files; this auth change does not move or delete any R2 objects.

Portal folders:

- `Releases`
- `Tester Uploads`
- `VODs/Incoming`
- `VODs/Reviewed`
- `VODs/Archived`
- `Screenshots`
- `Bug Reports`
- `Logs`
- `Archived`

Tester role can browse/download and upload only to tester-facing folders. VOD uploads from testers go only to `VODs/Incoming`; reviewed and archived VOD areas are admin-managed. Admin role additionally manages releases, the **Latest** pointer, deletes, and archived content.

Files up to 50 MB use the simple upload endpoint. Larger files automatically use R2 multipart upload in 50 MB parts, with a 10 GB per-file safety ceiling. Multipart uploads are aborted on client-side failure when possible, avoiding the previous 75 MB whole-file bottleneck without making the R2 bucket public.

## Separation rules

Do not reuse one project's Worker or bucket for another project.

Do not point the support Worker at the Tester Share bucket or vice versa.

Do not make the R2 buckets public. Browser access should always pass through the Worker.

## Credentials

Cloudflare deployment uses the repository Actions secret `CLOUDFLARE_API_TOKEN`.

That Cloudflare API token must be permitted to edit Workers scripts/secrets, D1 databases, and R2 buckets in this account; the new auth setup adds D1 access to the previous Worker/R2 deployment needs.

Before deploying the per-user account system, add the repository Actions secret `SHARE_PORTAL_BOOTSTRAP_SECRET`. Use a private, high-entropy value and keep it in a password manager. The workflow installs it as the Worker secret `BOOTSTRAP_SECRET`; it is used only to create or recover the single admin account.

After deployment, open `/setup` and create the admin account with a unique password of at least 14 characters. The former shared `Admin123` and `Tester123` passwords are not used by the per-user system. From the admin portal, create each tester account and privately share its one-time setup link. Testers who forget their password ask the admin for a one-time reset link. Setup/reset links expire after 20 minutes and can be used once. The admin can disable a tester without deleting their uploaded files.

## Redeployment

Production deployment workflows are manual-only after initial verification. Use GitHub Actions when a Worker or Wrangler configuration changes, and confirm the workflow's post-deploy `/health` check passes. The deployment action provisions the D1 auth database and updates the generated binding in its temporary checkout; the deployed Worker uses D1 for users/sessions and the existing private R2 bucket for files.
