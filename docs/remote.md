# Running the remote server

The extension in the [README](../README.md) works in Claude Desktop on the computer it is installed on. To use the same tools on claude.ai and in the Claude phone apps, the server can run on the internet as a custom connector. The same `server.py` does this when started with `MCP_TRANSPORT=http`. One server can be shared by a few invited people, each with their own Garmin account.

## For the people you invite

1. In Claude (web or desktop): **Settings → Connectors → Add custom connector**, with the URL `https://<app-name>.fly.dev/mcp`.
2. Click **Connect** and sign in with your own Garmin email and password, plus the code Garmin sends if it asks for one.
3. That's it. The connector shows up in the Claude phone apps too, and everyone only sees their own Garmin data.

## How it is protected

- Only Garmin emails listed in `ALLOWED_EMAILS` can sign in. Anyone else is turned away before the server contacts Garmin, and taking an email off the list cuts off that person's access straight away.
- The Garmin password is sent to Garmin once to sign in and is never stored. The server keeps each person's Garmin session in a folder of their own, and every Claude token is tied to the person it was issued to ([`remote_auth.py`](../remote_auth.py)).
- Three wrong passwords for an email lock it for 15 minutes.
- Only Claude's own redirect addresses (and `localhost`, for Claude Code) can register, so a look-alike site can't collect a sign-in.
- Access tokens last an hour and refresh tokens 90 days, both replaced on every refresh. Only hashes of them are stored.
- Whoever runs the server can technically reach the Garmin sessions stored on it. Only invite people who are fine with that.

## Deploy on Fly.io

The included [`Dockerfile`](../Dockerfile) and [`fly.toml`](../fly.toml) run it in Stockholm on a small machine that stops when idle and starts on the next request (a few dollars a month at most). Change `app` in `fly.toml` to a name of your own first.

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

## Inviting or removing someone

Set `ALLOWED_EMAILS` again with the whole new list. Fly restarts the server with it. To also delete a removed person's Garmin session from the server:

```bash
python -c "import hashlib; print('u_' + hashlib.sha256(b'friend@example.com').hexdigest()[:20])"
fly ssh console -C "rm -rf /data/users/<the id printed above>"
```

## Upgrading from the single-user version

Connections made before keep working. `OWNER_PASSWORD` is no longer used and can be removed with `fly secrets unset OWNER_PASSWORD`. `GARMIN_TOKENS_B64` and [`scripts/garmin_login.py`](../scripts/garmin_login.py) only matter for those older connections.

## Testing locally

```bash
MCP_TRANSPORT=http PUBLIC_URL=http://127.0.0.1:8080 ALLOWED_EMAILS=you@example.com HOST=127.0.0.1 python server.py
```
