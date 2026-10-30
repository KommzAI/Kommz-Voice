# Security Rotation Runbook (P0)

## Scope
- Supabase keys (`service_role`, `anon` if exposed)
- Flask secrets (`SECRET_KEY`, `ADMIN_SECRET`)
- License salts (`VOICE_SECRET_SALT`, `DESKTOP_SECRET_SALT`, `PASSWORD_SALT`)
  - Changing a license salt invalidates every sold key that is not registered in the
    database. Follow `docs/licences-enregistrees.md` first (registration, migration,
    `KOMMZ_LICENSE_REQUIRE_REGISTERED=1`), then rotate.
- Third-party keys (`DEEPL_AUTH_KEY`, SMTP credentials, Make connections)

## 1. Prepare Green Environment
1. Clone production service into a green slot/service.
2. Inject only new secrets into green env.
3. Keep blue live until validation is complete.

## 2. Rotate Providers
1. Supabase:
   - Regenerate `service_role`.
   - Regenerate `anon` if previously exposed.
2. DeepL:
   - Revoke old key.
   - Create new key.
3. SMTP/Email:
   - Rotate SMTP credentials used by Make.
4. Render/Modal:
   - Update all runtime env variables with rotated values.

## 3. Validate on Green
1. `GET /health`
2. `POST /license/voice/verify-web`
3. `POST /register`, `POST /login`
4. `POST /api/upload-reference`
5. `POST /api/transcribe/{file_id}`
6. `POST /api/generate`

## 4. Cutover
1. Shift traffic blue -> green.
2. Monitor 15-30 minutes:
   - 4xx/5xx rates
   - latency
   - auth/license failures
3. If stable, revoke old secrets and decommission blue.

## 5. Git History Purge
1. Rotate secrets first.
2. Run `scripts/purge-git-history.ps1`.
3. Force push rewritten history.
4. Require team re-clone.

## Rollback
1. Re-route traffic to blue.
2. Re-check env mapping and rotated credentials.
3. Re-validate green and retry cutover.
