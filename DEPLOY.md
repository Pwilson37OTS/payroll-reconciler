# Deploying the Cost Reconciler to Streamlit Community Cloud

This app runs on Streamlit Community Cloud so the team can reach it at a URL. It
gets its data three ways:

- **Bullhorn** — pulled live from Snowflake (`BULLHORN_MIRROR`) via key-pair auth.
- **Payroll register + C2C** — committed into the repo's `data/` folder by
  `refresh_data.ps1` (Community Cloud can't see your OneDrive sync), then read
  from `data/`.
- **Saved audit history** — persisted to Snowflake (`BULLHORN_MIRROR.ANALYTICS`),
  because Community Cloud's disk is wiped on every redeploy.

The deploy mirrors the Commission Console / invoice-reconciler apps.

## One-time setup

1. **Create a PRIVATE GitHub repo** and push this folder to it.
   `data/` holds payroll files with compensation PII, so the repo **must be
   private**.
   ```bash
   git init && git add -A && git commit -m "Cost Reconciler"
   git branch -M main
   git remote add origin https://github.com/<you>/oaktree-cost-reconciler.git
   git push -u origin main
   ```

2. **Create the app on [share.streamlit.io](https://share.streamlit.io)** →
   *New app* → pick the repo, branch `main`, main file `app.py`.

3. **Paste the secrets** (Manage app → Settings → Secrets). Start from
   [`.streamlit/secrets.cloud.toml.example`](.streamlit/secrets.cloud.toml.example)
   and fill in:
   - `app_password` and `allowed_emails` (the login gate — `require_login = true`).
   - `[connections.snowflake]` with **`private_key_pem`** = the full contents of
     `tickler_app_rsa.p8` (no key file on Cloud). Keep `private_key_file` OUT.
   - `[payroll] root_dir = "data"` and `[storage] backend = "snowflake"` are
     already in the template.

4. **Seed the data** (already done once): `data/` is committed. To refresh it,
   see below.

That's it — the app builds, gates on the password, pulls Bullhorn from Snowflake,
reads payroll/C2C from `data/`, and saves history to Snowflake.

## Monthly / ongoing

1. Drop the new **payroll register** (`Payroll_Register_Export_*.xlsx`) and the
   updated **`C2C - CTS.xlsx` / `C2C - OTS.xlsx`** into the synced
   `…/_shared/payroll-audit` folder as usual.
2. **Double-click `refresh_data.ps1`** (or run it in PowerShell). It copies the
   newest register → `data/payroll_register.xlsx`, copies the C2C files, commits,
   and pushes. Streamlit Cloud auto-redeploys in ~30s with the fresh data.
3. Open the app, run each month in order, and **Save** — history goes to
   Snowflake and persists across redeploys.

## Notes

- **History table**: `BULLHORN_MIRROR.ANALYTICS.COST_RECON_AUDIT` (+ `_FILE` for
  raw snapshots), created automatically on first save. Role `CLAUDEAI` has write
  access.
- **PII**: the payroll register in `data/` contains wages for all contractors and
  is in the repo's git history — keep the repo private.
- **Redeploys don't lose history** (it's in Snowflake), but they DO reset the
  ephemeral disk — which is why the local-file store is not used on Cloud.

## Running locally (unchanged)

Use `.streamlit/secrets.toml` (from `secrets.toml.example`): `private_key_file`
pointing at the `.p8` on disk, `[payroll] root_dir` pointing at the synced
folder, and either `require_login = false` (internal) or a password. Leave
`[storage] backend` off (defaults to the local file store) unless you want to
test the Snowflake backend.
