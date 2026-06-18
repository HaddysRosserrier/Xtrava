# Hosting Xtrava on your machine / LAN

Xtrava runs on one machine (your PC or a home server) and is reached by other
people over your local network. This keeps the manual Strava login working,
since the first sign-in needs a visible browser window.

## 1. One-time setup

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Sign in to Strava once (creates session.json)

The scraper needs a logged-in session. Run any scrape once and log in when the
browser window opens:

```powershell
python main.py leaderboard
```

A browser opens; log in to Strava, then wait. It saves `session.json` and future
scrapes run headless. **`session.json` holds your Strava cookies — keep it
private and never commit it** (it is already in `.gitignore`).

> When Strava eventually logs you out, the dashboard's **Refresh** button (and
> the Update / Auto-Audit buttons) will start failing. Re-run the command above
> to sign in again.

## 3. Configure the deployment (.env file)

All deployment config lives in a `.env` file in the project root, which
`serve.py` loads on startup. Copy the template and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

```ini
# .env
XTRAVA_PASSWORD=choose-a-strong-password   # required — login password
XTRAVA_USER=admin                          # login username (default "admin")
XTRAVA_HOST=0.0.0.0                         # 0.0.0.0 = LAN, 127.0.0.1 = local only
XTRAVA_PORT=5000                           # port to listen on
```

`.env` is gitignored — your password never gets committed. The server refuses to
start without `XTRAVA_PASSWORD`, so it can't be exposed without a login by
accident. (A value set in the actual terminal environment still overrides `.env`,
which is handy for one-off changes.)

## 4. Start the server

```powershell
python serve.py
```

It listens on the configured host/port — by default `http://0.0.0.0:5000`, i.e.
all network interfaces. Other people reach it at `http://<this-machine-LAN-ip>:5000`
(find the IP with `ipconfig`).

The first time a browser connects it prompts for the username/password from `.env`.

## 5. Let LAN devices connect

Windows Firewall will likely block incoming connections on the port. Allow it
once (run PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "Xtrava" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

## Notes & limits

- **One scrape at a time.** The Refresh / Update / Auto-Audit buttons each drive
  a real browser (~10–30s). They're serialized server-side: if one is running,
  others get "another scrape is already running — try again in a moment".
- **Trust the network.** Basic Auth sends credentials with each request; over a
  plain-HTTP LAN that's fine for a trusted network, but don't port-forward this
  to the public internet without putting HTTPS (e.g. a reverse proxy) in front.
- **Keep the terminal open.** Closing it stops the server. To run it
  unattended, use a scheduled task or a tool like NSSM to run `python serve.py`
  as a Windows service (with the env vars set for that service).
