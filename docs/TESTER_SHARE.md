# CheatVision Tester Share

Private tester file sharing is provided by a Cloudflare Worker backed by a private R2 bucket. Individual user accounts, reset links, and sessions are stored in a dedicated D1 database.

- Portal: https://cheatvision-share.sensoredrooster-com.workers.dev
- R2 bucket: `cheatvision-share`
- D1 database: `cheatvision-share-auth`
- Storage is separate from the project's support-diagnostics R2 bucket.
- The R2 bucket remains private; downloads and uploads pass through the authenticated Worker.

## Roles

**Tester**

- Browse and download files.
- Upload to `Tester Uploads`, `VODs/Incoming`, `Screenshots`, `Bug Reports`, and `Logs`.
- Large files use resumable-style R2 multipart uploads in 50 MB parts.
- Cannot upload releases, write directly to reviewed/archived VOD areas, delete files, or change the Latest build.

**Admin**

- All tester capabilities.
- Upload releases and mark one as **Latest**.
- Delete files and clear the Latest pointer when the selected file is removed.
- Create tester accounts and generate private, one-time setup/reset links.
- Disable tester access without deleting that tester's uploaded files.

## Folders

- `Releases`
- `Tester Uploads`
- `VODs/Incoming` — tester VOD drop area
- `VODs/Reviewed` — administrator-managed reviewed VODs
- `VODs/Archived` — administrator-managed archived VODs
- `Screenshots`
- `Bug Reports`
- `Logs`
- `Archived`

R2 uses object keys rather than real directories; the portal presents these prefixes as folders.

## Account setup and recovery

Before the first deployment, add the repository Actions secret `SHARE_PORTAL_BOOTSTRAP_SECRET`. After deploying, open `/setup` and create the administrator account. The private setup key is also the recovery key for that same admin email; store it in a password manager.

The admin creates each tester account from the **Tester accounts** panel and privately shares the generated setup link. When a tester forgets a password, they contact the admin, who generates and privately shares a reset link. Setup/reset links are single-use and expire after 20 minutes. No email service is configured or required.

Each person has an individual email-based login and password. Passwords are stored as salted PBKDF2-HMAC-SHA-256 digests in D1; the former shared `Admin123` / `Tester123` passwords are not used after deployment. Browser sessions use random opaque tokens, and only token hashes are stored. The session cookie is Secure, HttpOnly, and SameSite=Strict.

Login attempts and uploads are rate limited. Files up to 50 MB use the simple upload path. Larger files automatically use R2 multipart upload with 50 MB parts, with a 10 GB per-file safety ceiling. This avoids the old 75 MB whole-file limit while keeping each request below Cloudflare's request-body ceiling. Failed multipart uploads are aborted by the browser when possible. The Worker sets no-store and common browser security headers. Disabling an account invalidates its active sessions but leaves its files untouched.

## Deployment

The manual GitHub Actions workflow is `.github/workflows/deploy-share-portal.yml`.

It creates the dedicated R2 bucket and D1 auth database when needed, applies D1 migrations, installs the setup key as a Worker secret, deploys the Worker, and verifies `/health` including D1 availability. D1 stores accounts/tokens/sessions only; existing R2 files are not migrated or deleted.

The syntax check workflow validates the Worker whenever share-portal code changes.
