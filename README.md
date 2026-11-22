# Garmin MCP

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any other MCP client) access to my own Garmin Connect data.

I built it so I can talk to Claude about my training and sleep with real data behind it: how I should plan the next weeks of training, whether I'm recovering well, and what separates the nights I sleep well from the ones I don't.

It ships as an **MCP Bundle** (`.mcpb`): one file you open in Claude Desktop. No Python setup needed.

## What it does

All tools are read-only.

| Tool | What it returns |
|---|---|
| `get_recovery_snapshot` | Last night's sleep, HRV against baseline, training readiness, resting HR, body battery and stress in one call |
| `get_sleep_data` | Sleep for one night: stages, sleep score and its factors, HRV, SpO2, respiration |
| `get_sleep_last_n_days` | Sleep for the last N nights (up to 90), with averages |
| `get_hrv` | Overnight HRV with weekly average, status and personal baseline range |
| `get_daily_summary` | Steps, calories, intensity minutes, heart rate, stress and body battery for a day |
| `get_training_readiness` | Training readiness score and the factors behind it |
| `get_training_status` | Training status, acute vs chronic load, 4-week load balance, VO2 max |
| `get_recent_activities` | Latest workouts with pace/speed, heart rate zones, training load and training effect |
| `get_activities_by_date` | All workouts between two dates |
| `get_activity_details` | Laps/splits, heart rate zones, running dynamics and weather for one workout |
| `get_weekly_training_load` | Per week: sessions, hours, distance, load, intensity minutes, zones, sport mix |
| `get_planned_workouts` | Workouts scheduled in the Garmin calendar and active training plans |
| `get_performance_metrics` | VO2 max, race predictions, endurance score, hill score, fitness age, lactate threshold |
| `get_personal_records` | Personal records and the activities they were set in |
| `get_wellness_trend` | Day-by-day sleep, HRV, resting HR, readiness, body battery, stress, steps, intensity minutes, weight or VO2 max, with stats |
| `garmin_submit_mfa_code` | Finishes sign-in when Garmin asks for a verification code |

Example questions I ask Claude:

- *"How has my sleep been the last two weeks, and does it drop after hard sessions?"*
- *"Based on my last 10 runs, am I training too much at high intensity?"*
- *"My HRV is low today. Should I swap the interval session for an easy run?"*
- *"Compare my training load the last 8 weeks. Am I building up too fast?"*
- *"How did the intervals on Saturday go, lap by lap?"*

## How it works

- Built with the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`MCPServer`). Each tool is a plain Python function with a docstring, which the model reads to decide when to call it.
- Uses [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) to talk to Garmin Connect. Sign-in tokens are saved in `~/.garminconnect` and refreshed automatically, so the password is only used when there is no valid session.
- Garmin's raw responses are huge (one night of sleep is close to 200,000 characters). The tools return compact summaries instead, and leave out GPS positions and profile details. `detailed=true` gives the raw time series when it is really needed.
- Runs locally over stdio in Claude Desktop. Credentials and data never leave my machine except for the calls to Garmin itself. It can also run as a remote server behind OAuth (see [Run it remotely](#run-it-remotely-claudeai-and-the-claude-phone-apps)).

## Install in Claude Desktop

1. Get `garmin-mcp-<version>.mcpb`, from the releases page or by building it yourself (see [Build the bundle](#build-the-bundle)).
2. Double-click the file, or drag it into Claude Desktop (**Settings → Extensions**).
3. Fill in the email and password you use for Garmin Connect. The password is stored in your system keychain.
4. Ask *"How did I sleep last night?"* to check that it works.

Claude Desktop installs Python and the dependencies itself the first time the extension starts, so the first call can take a little longer.

**Two-step verification:** if your Garmin account uses it, the first request makes Garmin send you a code. Claude asks you for it and finishes the sign-in. After that the saved session is used.

**Upgrading from the old version?** Remove the `"garmin"` entry from `claude_desktop_config.json` (Settings → Developer → Edit Config) so you don't get the tools twice. The old `.garth` folder can be deleted; those tokens no longer work.

## Run it remotely (claude.ai, the phone apps, and friends)

The bundle only works in Claude Desktop on the computer it is installed on. To use the same tools on claude.ai and in the Claude phone apps, run the server on the internet and add it as a custom connector. The same `server.py` does this when started with `MCP_TRANSPORT=http`. One server can be shared by a few invited people, each with their own Garmin account.

### For the people you invite

1. In Claude (web or desktop): **Settings → Connectors → Add custom connector**, with the URL `https://<app-name>.fly.dev/mcp`.
2. Click **Connect** and sign in with your own Garmin email and password, plus the code Garmin sends if it asks for one.
3. That's it. The connector shows up in the Claude phone apps too, and everyone only sees their own Garmin data.

### How it is protected

- Only Garmin emails listed in `ALLOWED_EMAILS` can sign in. Anyone else is turned away before the server contacts Garmin, and taking an email off the list cuts off that person's access straight away.
- The Garmin password is sent to Garmin once to sign in and is never stored. The server keeps each person's Garmin session in a folder of their own, and every Claude token is tied to the person it was issued to ([`remote_auth.py`](remote_auth.py)).
- Three wrong passwords for an email lock it for 15 minutes.
- Only Claude's own redirect addresses (and `localhost`, for Claude Code) can register, so a look-alike site can't collect a sign-in.
- Access tokens last an hour and refresh tokens 90 days, both replaced on every refresh. Only hashes of them are stored.
- Whoever runs the server can technically reach the Garmin sessions stored on it. Only invite people who are fine with that.

### Deploy on Fly.io

The included [`Dockerfile`](Dockerfile) and [`fly.toml`](fly.toml) run it in Stockholm on a small machine that stops when idle and starts on the next request (a few dollars a month at most). Change `app` in `fly.toml` to a name of your own first.

1. Install [flyctl](https://fly.io/docs/flyctl/install/) and sign in with `fly auth login`.
2. Create the app and the disk for the sign-in state and Garmin sessions:
   ```bash
   fly apps create <your-app-name>
   fly volumes create garmin_data --region arn --size 1
   ```
3. List the Garmin emails that may sign in, separated by commas:
   ```bash
   fly secrets set ALLOWED_EMAILS="you@example.com,friend@example.com" --stage
   ```
4. Deploy: `fly deploy --ha=false`
5. Connect as described above.

**Inviting or removing someone:** set `ALLOWED_EMAILS` again with the whole new list. Fly restarts the server with it. To also delete a removed person's Garmin session from the server:

```bash
python -c "import hashlib; print('u_' + hashlib.sha256(b'friend@example.com').hexdigest()[:20])"
fly ssh console -C "rm -rf /data/users/<the id printed above>"
```

**Upgrading from the single-user version:** connections made before keep working. `OWNER_PASSWORD` is no longer used and can be removed with `fly secrets unset OWNER_PASSWORD`. `GARMIN_TOKENS_B64` and `scripts/garmin_login.py` only matter for those older connections.

Run it locally in HTTP mode for testing with `MCP_TRANSPORT=http PUBLIC_URL=http://127.0.0.1:8080 ALLOWED_EMAILS=you@example.com HOST=127.0.0.1 python server.py`.

## Development

You need [uv](https://docs.astral.sh/uv/) (or Python 3.12+ with `pip install -r requirements.txt`).

```bash
git clone https://github.com/bragenybakk/garmin-mcp.git
cd garmin-mcp
cp .env.example .env    # fill in GARMIN_EMAIL and GARMIN_PASSWORD
uv run server.py
```

The `.env` file stays on your computer and is never uploaded (it is listed in `.gitignore`), and it is never put in the bundle.

Try the tools interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run --directory . server.py
```

Use it from Claude Code:

```bash
claude mcp add garmin -- uv run --directory /full/path/to/garmin-mcp server.py
```

### Build the bundle

Needs Node.js (for `npx`):

```bash
python scripts/build_mcpb.py
```

This copies only the files the bundle needs into `build/bundle/`, validates `manifest.json`, packs `dist/garmin-mcp-<version>.mcpb` with the official [mcpb](https://github.com/modelcontextprotocol/mcpb) CLI, and refuses to finish if anything like `.env` or a token file ended up inside. The version in `manifest.json`, `pyproject.toml` and `server.py` must match.

After changing dependencies in `pyproject.toml`, run `uv lock` so `uv.lock` matches.

## Troubleshooting

- **"Found old garth-format tokens":** Garmin changed its sign-in in 2026 and the old `garth` library stopped working. Make sure the email and password are filled in; a fresh sign-in creates new tokens.
- **Sign-in keeps failing:** delete the `~/.garminconnect` folder and try again. It will sign in fresh with your email and password.
- **"Garmin is rate limiting requests":** wait a few minutes. Repeated failed sign-ins make Garmin block for a while.
- **The tools don't show up:** check that the extension is enabled under Settings → Extensions, and restart Claude Desktop.

## Ideas for later

- Create and schedule workouts on the Garmin calendar (write access, behind a confirmation)
- Caching responses to avoid repeated calls to Garmin
