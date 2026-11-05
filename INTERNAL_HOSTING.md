# Internal hosting (network-only, no login)

This app runs on **your PC** and is shared with a few coworkers over the LAN. It
has **no login** — access is controlled entirely by a **Windows Firewall
allowlist** on port `8501`. That firewall rule is what keeps everyone else on
the network out, so it is **required**, not optional. Without it, anyone on the
office network who opens `http://<your-PC-IP>:8501` can see all payroll/comp data.

> Why this host: auto-pull needs both the Snowflake key file
> (`C:\Users\pwil\.snowflake\tickler_app_rsa.p8`) and the synced payroll folder
> (`…\_shared\payroll-audit`). Both live on this PC, so this PC is the host.

## One-time setup

### 1. Find this PC's LAN address
Open a terminal and run:

```bash
ipconfig
```

Note the **IPv4 Address** (e.g. `10.0.12.34`). That's your `<HOST-IP>`. The app
URL you'll share is `http://<HOST-IP>:8501`. (Ask IT for a **DHCP reservation**
so this IP doesn't change.)

### 2. Collect the coworkers' IPs
Get the IPv4 of each machine that should have access (same `ipconfig` on theirs).
Ask IT for DHCP reservations for those too, or they'll drop off the allowlist
when their IP changes.

### 3. Add the firewall allowlist (run as Administrator)
This opens `8501` **only** to the listed machines and blocks everyone else. Run
in an **elevated PowerShell** (replace the IPs with your host + coworkers):

```powershell
New-NetFirewallRule -DisplayName "Cost Reconciler 8501 (allowlist)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8501 `
  -RemoteAddress 10.0.12.34,10.0.12.40,10.0.12.41 `
  -Profile Domain,Private
```

- Include the **host PC's own IP** in the list if you'll also open it locally.
- To change who has access later, update the rule's addresses:

```powershell
Set-NetFirewallRule -DisplayName "Cost Reconciler 8501 (allowlist)" `
  -RemoteAddress 10.0.12.34,10.0.12.40,10.0.12.42
```

- To remove it entirely:

```powershell
Remove-NetFirewallRule -DisplayName "Cost Reconciler 8501 (allowlist)"
```

> Prefer scoping to specific IPs over a whole subnet. A subnet (e.g.
> `10.0.12.0/24`) exposes the app to the entire office segment — which defeats
> the point of network-only access for comp data.

## Running it

Double-click **`run_internal.bat`** (or run it from a terminal). Leave the window
open — closing it stops the app. Then share the URL:

```
http://<HOST-IP>:8501
```

Allowlisted coworkers open that in any browser; blocked machines get a connection
timeout. Because there's no login, **anyone on an allowlisted machine has full
access** — there is no per-user identity or audit of who viewed what.

### Keep it available
- This PC must be **on and awake** for others to reach the app. Set Power &
  sleep so it doesn't sleep (or use "when plugged in, never sleep").
- To start it automatically at logon, put a shortcut to `run_internal.bat` in
  the Startup folder (`shell:startup`), or create a Scheduled Task
  (Trigger: *At log on* → Action: start `run_internal.bat`).

## Updating the app
When the code changes, just restart: close the `run_internal.bat` window and run
it again. If dependencies changed, first run `pip install -r requirements.txt`.

## Security notes (so there are no surprises)
- **No login.** Access = network reach. The firewall allowlist is the only gate.
- **Plain HTTP.** Traffic (including the comp data on screen) is unencrypted on
  the LAN. Fine within a trusted wired office; if it needs to cross Wi-Fi or
  segments, front it with a reverse proxy that adds HTTPS.
- **No per-user identity.** You can't tell which coworker viewed a run.
- **Snowflake key + payroll data stay on this PC** — nothing leaves your network.
- To **re-enable the login screen** later, set `require_login = true` (or delete
  that line) in `.streamlit/secrets.toml`; the shared password + email allowlist
  come back with no other changes. To add real per-user identity, put it behind
  your org SSO via a reverse proxy (IIS Windows auth / nginx) — ask IT.
