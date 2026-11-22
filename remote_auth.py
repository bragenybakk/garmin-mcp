"""Sign-in for running the Garmin MCP server on the internet (for example on Fly.io).

Only used when MCP_TRANSPORT=http. It is a single-user OAuth authorization server
built on the MCP SDK's OAuth support, so claude.ai and the Claude apps can connect:

- Claude registers itself (dynamic client registration). Only redirect URIs on an
  allowlist are accepted: Claude's own callbacks, loopback addresses (Claude Code,
  local testing) and anything in EXTRA_REDIRECT_URIS.
- /authorize sends the browser to a small login page. Typing OWNER_PASSWORD there
  approves the connection. Repeated wrong passwords lock the page for a while.
- The SDK checks PKCE, the redirect URI and code expiry. Access tokens last one
  hour and refresh tokens 90 days; both are rotated on refresh. Only SHA-256 hashes
  of tokens are stored, in DATA_DIR/oauth.json.

It also seeds the server's Garmin session from the GARMIN_TOKENS_B64 secret.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger("garmin-mcp.auth")

ACCESS_TTL = 60 * 60
REFRESH_TTL = 90 * 24 * 60 * 60
CODE_TTL = 5 * 60
LOGIN_TTL = 10 * 60
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
MAX_CLIENTS = 50
MIN_PASSWORD_LENGTH = 12

CLAUDE_REDIRECT_URIS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
OWNER = "owner"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class RemoteConfig:
    public_url: str
    owner_password: str
    data_dir: Path
    extra_redirect_uris: set[str]

    @property
    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        public_url = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
        if not public_url and os.getenv("FLY_APP_NAME"):
            public_url = f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
        if not public_url:
            raise SystemExit("PUBLIC_URL must be set to the server's public https URL.")
        password = os.getenv("OWNER_PASSWORD") or ""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise SystemExit(
                f"OWNER_PASSWORD must be set and at least {MIN_PASSWORD_LENGTH} characters. "
                "It is the password you type to approve a connection from Claude."
            )
        extra = {u.strip() for u in (os.getenv("EXTRA_REDIRECT_URIS") or "").split(",") if u.strip()}
        data_dir = Path(os.getenv("DATA_DIR") or "data").expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(public_url, password, data_dir, extra)

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=self.resource_url,
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )


def seed_garmin_tokens(token_dir: Path) -> None:
    """Write the Garmin session from the GARMIN_TOKENS_B64 secret into token_dir.

    Only done when the secret changes, so tokens the server has refreshed since are
    not overwritten on every restart. Set a new secret to replace the session.
    """
    blob_b64 = os.environ.pop("GARMIN_TOKENS_B64", None)
    if not blob_b64:
        return
    digest = hashlib.sha256(blob_b64.strip().encode()).hexdigest()
    marker = token_dir / ".seed-sha256"
    if marker.exists() and marker.read_text().strip() == digest:
        return
    try:
        blob = base64.b64decode(blob_b64.strip(), validate=True).decode()
        json.loads(blob)
    except ValueError as e:
        raise SystemExit(f"GARMIN_TOKENS_B64 is not base64-encoded token JSON: {e}") from e
    token_dir.mkdir(parents=True, exist_ok=True)
    target = token_dir / "garmin_tokens.json"
    target.write_text(blob)
    os.chmod(target, 0o600)
    marker.write_text(digest)
    log.warning("Stored a new Garmin session from GARMIN_TOKENS_B64.")


# --------------------------------------------------------------------------
# OAuth provider
# --------------------------------------------------------------------------


class _Store:
    """OAuth clients and token hashes in a small JSON file on the data volume."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {"clients": {}, "access": {}, "refresh": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self.data[kind] = {h: r for h, r in self.data[kind].items() if r["expires_at"] > now}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


@dataclass
class _LoginRequest:
    expires_at: float
    client_id: str
    params: AuthorizationParams


class OwnerOAuthProvider:
    """OAuth authorization server for a single owner who approves with a password."""

    def __init__(self, config: RemoteConfig):
        self.config = config
        self.store = _Store(config.data_dir / "oauth.json")
        self.login_requests: dict[str, _LoginRequest] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failed_logins: list[float] = []

    # -- registration ------------------------------------------------------

    def redirect_allowed(self, uri: str) -> bool:
        if uri in CLAUDE_REDIRECT_URIS or uri in self.config.extra_redirect_uris:
            return True
        parsed = urlparse(uri)
        return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self.redirect_allowed(str(uri)):
                raise RegistrationError(
                    "invalid_redirect_uri", f"This server only accepts Claude's redirect URIs, not {uri}"
                )
        with self.store.lock:
            clients = self.store.data["clients"]
            clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
            while len(clients) > MAX_CLIENTS:  # dicts keep insertion order: drop the oldest
                clients.pop(next(iter(clients)))
            self.store.save()

    # -- authorization -----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") not in (self.config.resource_url, self.config.public_url):
            raise AuthorizeError("invalid_target", "This server only issues tokens for its own /mcp endpoint.")
        now = time.time()
        self.login_requests = {k: v for k, v in self.login_requests.items() if v.expires_at > now}
        request_id = secrets.token_urlsafe(24)
        self.login_requests[request_id] = _LoginRequest(now + LOGIN_TTL, client.client_id, params)
        return f"{self.config.public_url}/login?request={request_id}"

    def locked_out(self) -> bool:
        cutoff = time.time() - LOCKOUT_SECONDS
        self.failed_logins = [t for t in self.failed_logins if t > cutoff]
        return len(self.failed_logins) >= MAX_FAILED_LOGINS

    def password_ok(self, password: str) -> bool:
        return hmac.compare_digest(password.encode(), self.config.owner_password.encode())

    def issue_code(self, request_id: str) -> str:
        request = self.login_requests.pop(request_id)
        code = secrets.token_urlsafe(32)
        now = time.time()
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=request.params.scopes or [],
            expires_at=now + CODE_TTL,
            client_id=request.client_id,
            code_challenge=request.params.code_challenge,
            redirect_uri=request.params.redirect_uri,
            redirect_uri_provided_explicitly=request.params.redirect_uri_provided_explicitly,
            resource=self.config.resource_url,
            subject=OWNER,
        )
        return construct_redirect_uri(str(request.params.redirect_uri), code=code, state=request.params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)  # single use
        return self._issue(client.client_id, authorization_code.scopes)

    # -- tokens --------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        with self.store.lock:
            self.store.data["access"][_hash(access)] = {
                "client_id": client_id, "scopes": scopes, "expires_at": now + ACCESS_TTL, "refresh": _hash(refresh),
            }
            self.store.data["refresh"][_hash(refresh)] = {
                "client_id": client_id, "scopes": scopes, "expires_at": now + REFRESH_TTL,
            }
            self.store.save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.store.data["refresh"].get(_hash(refresh_token))
        if not record or record["client_id"] != client.client_id or record["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.config.resource_url,
            subject=OWNER,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._revoke_refresh(_hash(refresh_token.token))  # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.data["access"].get(_hash(token))
        if not record or record["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.config.resource_url,
            subject=OWNER,
        )

    def _revoke_refresh(self, refresh_hash: str) -> None:
        with self.store.lock:
            self.store.data["refresh"].pop(refresh_hash, None)
            self.store.data["access"] = {
                h: r for h, r in self.store.data["access"].items() if r.get("refresh") != refresh_hash
            }
            self.store.save()

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._revoke_refresh(_hash(token.token))
            return
        record = self.store.data["access"].get(_hash(token.token))
        if record:
            self._revoke_refresh(record["refresh"])


# --------------------------------------------------------------------------
# Login page
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garmin MCP</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --text:#16202b; --muted:#5b6774; --accent:#0f766e; --err:#b42318; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b1520; --card:#132231; --text:#e6edf3; --muted:#9aa7b4; --accent:#2dd4bf; --err:#f97066; }} }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--text);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ width:min(420px, calc(100vw - 32px)); background:var(--card); border-radius:16px; padding:28px;
          box-shadow:0 1px 3px rgba(0,0,0,.08), 0 8px 24px rgba(0,0,0,.06); }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p {{ margin:0 0 16px; color:var(--muted); }}
  label {{ display:block; font-weight:600; margin-bottom:6px; }}
  input {{ width:100%; box-sizing:border-box; padding:12px; font-size:16px; border-radius:10px;
           border:1px solid color-mix(in srgb, var(--muted) 40%, transparent); background:transparent; color:var(--text); }}
  .row {{ display:flex; flex-direction:row-reverse; gap:12px; margin-top:16px; }}
  button {{ flex:1; padding:12px; font-size:16px; border-radius:10px; border:0; cursor:pointer; }}
  .primary {{ background:var(--accent); color:#fff; font-weight:600; }}
  .secondary {{ background:transparent; color:var(--muted); border:1px solid color-mix(in srgb, var(--muted) 40%, transparent); }}
  .error {{ color:var(--err); font-weight:600; }}
</style></head>
<body><main>{body}</main></body></html>"""

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(body=body), status_code=status, headers=SECURITY_HEADERS)


def _message(title: str, text: str, status: int) -> HTMLResponse:
    return _page(f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>", status)


def _form(request_id: str, client_name: str, redirect_host: str, error: str | None = None) -> str:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""
<h1>Connect your Garmin data</h1>
<p><strong>{html.escape(client_name)}</strong> wants read-only access to your Garmin Connect data
through this server. After approving you are sent back to <strong>{html.escape(redirect_host)}</strong>.</p>
{err}
<form method="post" action="/login">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label for="password">Server password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
  <div class="row">
    <!-- Approve must be the first submit button: pressing Enter submits with the first one.
         row-reverse still shows Deny on the left. -->
    <button class="primary" type="submit" name="action" value="approve">Approve</button>
    <button class="secondary" type="submit" name="action" value="deny" formnovalidate>Deny</button>
  </div>
</form>"""


def add_routes(server, provider: OwnerOAuthProvider) -> None:
    """Register /login and /health on an MCPServer."""

    async def _describe(request_id: str) -> tuple[_LoginRequest, str, str] | None:
        login = provider.login_requests.get(request_id)
        if not login or login.expires_at < time.time():
            return None
        client = await provider.get_client(login.client_id)
        name = (client.client_name if client else None) or "An MCP client"
        host = urlparse(str(login.params.redirect_uri)).hostname or "the app"
        return login, name, host

    @server.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
        else:
            form = await request.form()
            request_id = str(form.get("request", ""))
        described = await _describe(request_id)
        if not described:
            return _message("This link has expired", "Start connecting again from Claude.", 400)
        login_request, client_name, redirect_host = described

        if request.method == "GET":
            return _page(_form(request_id, client_name, redirect_host))

        if form.get("action") == "deny":
            log.warning("Connection from %s denied on the login page.", client_name)
            provider.login_requests.pop(request_id, None)
            target = construct_redirect_uri(
                str(login_request.params.redirect_uri), error="access_denied", state=login_request.params.state
            )
            return RedirectResponse(target, status_code=302, headers=SECURITY_HEADERS)

        if provider.locked_out():
            log.warning("Login page locked after repeated wrong passwords.")
            return _message("Too many attempts", "Wait 15 minutes and try again.", 429)
        if not provider.password_ok(str(form.get("password", ""))):
            provider.failed_logins.append(time.time())
            log.warning("Wrong password on the login page (%d recent).", len(provider.failed_logins))
            await asyncio.sleep(1)
            return _page(_form(request_id, client_name, redirect_host, "Wrong password."), 401)

        provider.failed_logins.clear()
        log.warning("Connection from %s approved.", client_name)
        return RedirectResponse(provider.issue_code(request_id), status_code=302, headers=SECURITY_HEADERS)

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})
