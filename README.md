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

Requires Python 3.10+.

```bash
git clone https://github.com/bragenybakk/garmin-mcp.git
cd garmin-mcp
pip install -r requirements.txt
cp .env.example .env   # then fill in your Garmin email and password
```

Add the server to Claude Desktop in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "python",
      "args": ["C:/path/to/garmin-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop, and the Garmin tools show up in the chat.

## Ideas for later

- Training readiness and VO2 max
- Weekly training load summaries
- Caching responses to avoid repeated calls to Garmin
