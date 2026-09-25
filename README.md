# Garmin MCP

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any other MCP client) access to my own Garmin Connect data.

I built it so I can talk to Claude about my training and sleep with real data behind it: how I should plan the next weeks of training, whether I'm recovering well, and what separates the nights I sleep well from the ones I don't.

## What it does

The server exposes these tools:

| Tool | What it returns |
|---|---|
| `get_sleep_data` | Sleep for one night: duration, sleep stages, sleep score |
| `get_sleep_last_n_days` | Sleep for the last N nights, for spotting trends |
| `get_recent_activities` | Latest workouts with duration, distance, heart rate and training effect |
| `get_daily_summary` | Steps, resting heart rate, stress and body battery for a day |
| `get_hrv` | Heart rate variability for a day |

Example questions I ask Claude:

- *"How has my sleep been the last two weeks, and does it drop after hard sessions?"*
- *"Based on my last 10 runs, am I training too much at high intensity?"*
- *"My HRV is low today. Should I swap the interval session for an easy run?"*

## How it works

- Built with [FastMCP](https://github.com/jlowin/fastmcp) from the official MCP Python SDK. Each tool is a plain Python function with a docstring, which the model reads to decide when to call it.
- Uses [garth](https://github.com/matin/garth) to authenticate against Garmin Connect. The session is cached locally in `.garth/`, so it only logs in with the password when the session has expired.
- Runs locally over stdio. Credentials and data never leave my machine except for the calls to Garmin itself.

## Setup

You need Python 3.10 or newer and the [Claude Desktop](https://claude.ai/download) app.

### 1. Download and install

```bash
git clone https://github.com/bragenybakk/garmin-mcp.git
cd garmin-mcp
pip install -r requirements.txt
```

### 2. Add your Garmin login

Make a copy of `.env.example` and name it `.env`. Open it and fill in the email and password you use for Garmin Connect:

```
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-password
```

The `.env` file stays on your computer and is never uploaded (it is listed in `.gitignore`).

### 3. Find two paths

You need the full path to Python and to `server.py`. Run these from inside the `garmin-mcp` folder:

**Windows (PowerShell)**

```powershell
(Get-Command python).Source
(Resolve-Path server.py).Path
```

**macOS / Linux**

```bash
which python3
realpath server.py
```

Copy both paths somewhere, you need them in the next step.

### 4. Connect it to Claude Desktop

1. Open Claude Desktop and go to **Settings → Developer → Edit Config**. This opens a file called `claude_desktop_config.json`.
2. Paste this into the file, and replace the two paths with the ones you found in step 3:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "C:\\Users\\you\\miniconda3\\python.exe",
      "args": ["C:\\Users\\you\\garmin-mcp\\server.py"]
    }
  }
}
```

   On Windows, every `\` in a path has to be written as `\\` in this file. On macOS the paths look like `/Users/you/garmin-mcp/server.py` and can be pasted as they are.

   If the file already has an `"mcpServers"` section, add the `"garmin": { ... }` block inside it instead of pasting a new one.

3. Save the file and **quit Claude Desktop completely** (on Windows: right-click the Claude icon by the clock and choose Quit). Closing the window is not enough.
4. Open Claude Desktop again. Ask *"How did I sleep last night?"* to check that it works.

### Using Claude Code instead?

One command does the same thing:

```bash
claude mcp add garmin -- python /full/path/to/garmin-mcp/server.py
```

### Troubleshooting

- **The Garmin tools don't show up:** make sure Claude Desktop was fully quit and reopened, and check for typos in the paths.
- **"GARMIN_EMAIL and GARMIN_PASSWORD must be set":** the `.env` file is missing or is still named `.env.example`.
- **Login fails:** delete the `.garth` folder and try again. It will log in fresh with the password from `.env`.

## Ideas for later

- Training readiness and VO2 max
- Weekly training load summaries
- Caching responses to avoid repeated calls to Garmin
