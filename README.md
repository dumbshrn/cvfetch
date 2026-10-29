# CVFetch

CVFetch is a private web interface for searching `@CVFilesearch_Bot` using your Telegram **user account**, selecting results, and copying files returned by the bot to your configured Telegram channel. It includes browser-based Telegram sign-in, so you do not need terminal access to enter the login code.

Only use it with content you are authorized to access and share. Keep this repository private: the app handles a Telegram user session and can post to the destination channel.

## Deploy on Render

1. Create a **new private GitHub repository** named `cvfetch` and upload the contents of this folder. Do not upload `.env`, Telegram session files, or database credentials.
2. In Render, create a new **Blueprint** from that repository. Render reads `render.yaml` and creates the web service. The service is designed to run on Render's free web-service plan.
3. Create a separate MongoDB database (for example, a free MongoDB Atlas cluster). Copy its connection URI. Allow Render's outbound connections in the database network access settings; for a quick personal setup this may require allowing `0.0.0.0/0`, and the database password must be strong and unique.
4. In the Render service's **Environment** page, fill in the values for the variables marked `sync: false`:

   | Variable | Where to get it |
   | --- | --- |
   | `API_ID` and `API_HASH` | Create an app at [my.telegram.org](https://my.telegram.org) |
   | `AUTH_CHANNEL` | Your destination channel ID or public username; the account must be able to post there |
   | `WEB_PASSWORD` | A long, unique password for the CVFetch website |
   | `FLASK_SECRET_KEY` | A fresh random secret used to protect website sign-in cookies |
   | `SESSION_STORE_URI` | Connection URI for your separate MongoDB database |
   | `SESSION_ENCRYPTION_KEY` | A Fernet key generated as described below |

   Render already sets `PORT`; `render.yaml` includes a default for local/static configuration. Keep the remaining values supplied by the Blueprint.
5. Generate `SESSION_ENCRYPTION_KEY` locally in PowerShell (Python is not needed):

   ```powershell
   $bytes = [byte[]]::new(32)
   [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
   $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_') + '=='
   $key
   ```

   Paste the result into Render as `SESSION_ENCRYPTION_KEY`. Generate a different long random value for `FLASK_SECRET_KEY`; do not reuse the encryption key or website password. Keep these values somewhere safe. If you lose the encryption key, CVFetch cannot decrypt the saved Telegram session and you will need to sign in again.

6. Deploy. Open the Render service URL and sign in with `WEB_PASSWORD`. Then use **Connect Telegram** and enter your phone number, Telegram login code, and (if enabled) two-step verification password directly in the page. Those values are sent from the site to Telegram; never share them in chat. The Telegram session is encrypted and stored in MongoDB so it can be restored after a Render restart.
7. Open the site after deployment and test one search and one authorized result. The destination Telegram account must have posting rights in `AUTH_CHANNEL`.

## Keeping the free service awake

Render's free web service sleeps after inactivity and its local filesystem is temporary. This app stores the Telegram session in MongoDB rather than relying on a local session file. You can configure an external uptime monitor to request `https://YOUR-SERVICE.onrender.com/healthz` every 10 minutes. That may prevent idle sleep, but does not prevent all Render restarts or guarantee uninterrupted availability. The health endpoint reports whether Telegram is connected without exposing credentials.

## How it works

- The web password protects the search UI and login endpoints.
- Browser Telegram login creates a Pyrogram user session. MongoDB stores that session encrypted with `SESSION_ENCRYPTION_KEY`.
- Searches and file-button clicks are sent to `CV_BOT` (default `CVFilesearch_Bot`). CVFetch collects callback-button results across pages and groups them by page in the web UI. Link buttons are shown as Telegram links but cannot be fetched automatically. If the bot reply includes a recognizable channel join button, CVFetch tries to join it, then retries/continues the file selection flow. Some private invites or extra membership checks may require manual action in Telegram.
- Returned media is copied to `AUTH_CHANNEL` with a cleaned caption. Telegram copy preserves the source media attachment and may not change its underlying filename.

## Configuration

| Variable | Required | Default / purpose |
| --- | --- | --- |
| `API_ID`, `API_HASH` | Yes | Telegram application credentials |
| `AUTH_CHANNEL` | Yes | Destination channel ID or username |
| `WEB_PASSWORD` | Yes | Password for the web interface |
| `FLASK_SECRET_KEY` | Yes | Flask cookie signing secret |
| `SESSION_STORE_URI` | Yes | MongoDB URI for encrypted session persistence |
| `SESSION_ENCRYPTION_KEY` | Yes | Fernet key used to encrypt the saved Telegram session |
| `SESSION_STORE_DB` | No | `cvfetch` |
| `SESSION_NAME` | No | `cvfetch_user` |
| `CV_BOT` | No | `CVFilesearch_Bot` |
| `HOST` | No | `127.0.0.1` locally; Render uses `0.0.0.0` |
| `PORT` | No | `5055` locally; Render supplies its service port |
| `REQUEST_TIMEOUT` | No | `45` seconds |
| `FILE_TIMEOUT` | No | `90` seconds |
| `LOG_LEVEL` | No | `INFO` |

## Local use

Python 3.10+ is required. Copy `.env.example` to `.env`, set all required values, then run `pip install -r requirements.txt` and `python bot.py`. Open `http://127.0.0.1:5055`. Local use is optional; the hosted login flow is intended for entering Telegram credentials from a browser.

## Security

Never commit `.env`, `*.session`, `*.session-journal`, database credentials, Telegram API hash, login code, or two-step password. If a Telegram session is exposed, revoke it through Telegram's active-session settings. Use HTTPS (Render provides it), a private GitHub repository, a strong web password, and a dedicated database credential. The session encryption key and Flask secret must remain private.

