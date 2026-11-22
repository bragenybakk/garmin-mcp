"""Sign-in for running the Garmin MCP server on the internet (for example on Fly.io).

Only used when MCP_TRANSPORT=http. It is a small OAuth authorization server built on
the MCP SDK's OAuth support, so claude.ai and the Claude apps can connect, and a few
invited people can share one server, each with their own Garmin account:

- Claude registers itself (dynamic client registration). Only redirect URIs on an
  allowlist are accepted: Claude's own callbacks, loopback addresses (Claude Code,
  local testing) and anything in EXTRA_REDIRECT_URIS.
- /authorize sends the browser to a login page where the person signs in with their
  Garmin email and password, plus a verification code if Garmin asks for one. Only
  emails in ALLOWED_EMAILS are accepted, and that is checked before Garmin is
  contacted. The Garmin password goes to Garmin once and is never stored; the server
  keeps only that person's Garmin session tokens, in a folder of their own.
- The SDK checks PKCE, the redirect URI and code expiry. Access tokens last one hour
  and refresh tokens 90 days; both are rotated on refresh. Only SHA-256 hashes of
  tokens are stored, in DATA_DIR/oauth.json. Every token records whose it is, and the
  tools only ever use that person's Garmin session.
"""

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
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
MAX_FAILURES_PER_EMAIL = 3
MAX_FAILURES_TOTAL = 20
LOCKOUT_SECONDS = 15 * 60
MAX_CLIENTS = 50

CLAUDE_REDIRECT_URIS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
# Tokens issued before Garmin sign-in existed belong to the original single owner,
# whose Garmin session lives in the server's default token folder.
LEGACY_OWNER = "owner"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def user_id(email: str) -> str:
    return "u_" + hashlib.sha256(normalize_email(email).encode()).hexdigest()[:20]


def mask_email(email: str) -> str:
    """For logs: enough to tell people apart, not the full address."""
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class RemoteConfig:
    public_url: str
    data_dir: Path
    allowed_emails: set[str]
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
        allowed = {normalize_email(e) for e in (os.getenv("ALLOWED_EMAILS") or "").split(",") if e.strip()}
        if not allowed:
            log.warning("ALLOWED_EMAILS is empty: nobody can sign in until it lists some Garmin emails.")
        extra = {u.strip() for u in (os.getenv("EXTRA_REDIRECT_URIS") or "").split(",") if u.strip()}
        data_dir = Path(os.getenv("DATA_DIR") or "data").expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(public_url, data_dir, allowed, extra)

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=self.resource_url,
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )

    def token_dir(self, subject: str, legacy_dir: str) -> str:
        """The folder with this person's Garmin session."""
        if subject == LEGACY_OWNER:
            return legacy_dir
        if not re.fullmatch(r"u_[0-9a-f]{20}", subject):
            raise ValueError("Unknown user.")
        return str(self.data_dir / "users" / subject / "garminconnect")


def seed_garmin_tokens(token_dir: Path) -> None:
    """Write the original owner's Garmin session from the GARMIN_TOKENS_B64 secret.

    Only done when the secret changes, so tokens the server has refreshed since are
    not overwritten on every restart.
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
    """OAuth clients, users and token hashes in a small JSON file on the data volume."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict = {"clients": {}, "access": {}, "refresh": {}, "users": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text()))
        self.data.setdefault("users", {})

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
    # Set while waiting for a Garmin verification code:
    email: str | None = None
    garmin: Any = None
    mfa_state: Any = None


class GarminOAuthProvider:
    """OAuth authorization server where invited people sign in with their Garmin account."""

    def __init__(self, config: RemoteConfig):
        self.config = config
        self.store = _Store(config.data_dir / "oauth.json")
        self.login_requests: dict[str, _LoginRequest] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.failures: dict[str, list[float]] = {}
        # Called with the user id after a new Garmin session is saved, so the server
        # can drop a cached client for that person.
        self.on_new_session: Callable[[str], None] = lambda subject: None

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

    def _recent_failures(self) -> None:
        cutoff = time.time() - LOCKOUT_SECONDS
        self.failures = {e: [t for t in ts if t > cutoff] for e, ts in self.failures.items()}
        self.failures = {e: ts for e, ts in self.failures.items() if ts}

    def locked_out(self, email: str) -> bool:
        self._recent_failures()
        total = sum(len(ts) for ts in self.failures.values())
        return len(self.failures.get(email, [])) >= MAX_FAILURES_PER_EMAIL or total >= MAX_FAILURES_TOTAL

    def record_failure(self, email: str) -> None:
        self.failures.setdefault(email, []).append(time.time())

    def signed_in(self, email: str) -> str:
        """Record a successful Garmin sign-in and return the person's user id."""
        subject = user_id(email)
        self.failures.pop(email, None)
        now = int(time.time())
        with self.store.lock:
            user = self.store.data["users"].setdefault(subject, {"email": email, "first_sign_in": now})
            user["last_sign_in"] = now
            self.store.save()
        self.on_new_session(subject)
        return subject

    def issue_code(self, request_id: str, subject: str) -> str:
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
            subject=subject,
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
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject or LEGACY_OWNER)

    # -- tokens --------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], subject: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        with self.store.lock:
            self.store.data["access"][_hash(access)] = {
                "client_id": client_id, "scopes": scopes, "subject": subject,
                "expires_at": now + ACCESS_TTL, "refresh": _hash(refresh),
            }
            self.store.data["refresh"][_hash(refresh)] = {
                "client_id": client_id, "scopes": scopes, "subject": subject, "expires_at": now + REFRESH_TTL,
            }
            self.store.save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def subject_allowed(self, subject: str) -> bool:
        """Still invited? Taking an email off ALLOWED_EMAILS cuts off its existing tokens too."""
        if subject == LEGACY_OWNER:
            return True
        user = self.store.data["users"].get(subject)
        return bool(user) and user.get("email") in self.config.allowed_emails

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.store.data["refresh"].get(_hash(refresh_token))
        if not record or record["client_id"] != client.client_id or record["expires_at"] < time.time():
            return None
        if not self.subject_allowed(record.get("subject", LEGACY_OWNER)):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
            resource=self.config.resource_url,
            subject=record.get("subject", LEGACY_OWNER),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._revoke_refresh(_hash(refresh_token.token))  # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject or LEGACY_OWNER)

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.data["access"].get(_hash(token))
        if not record or record["expires_at"] < time.time():
            return None
        if not self.subject_allowed(record.get("subject", LEGACY_OWNER)):
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
            resource=self.config.resource_url,
            subject=record.get("subject", LEGACY_OWNER),
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
  label {{ display:block; font-weight:600; margin:12px 0 6px; }}
  input {{ width:100%; box-sizing:border-box; padding:12px; font-size:16px; border-radius:10px;
           border:1px solid color-mix(in srgb, var(--muted) 40%, transparent); background:transparent; color:var(--text); }}
  .row {{ display:flex; flex-direction:row-reverse; gap:12px; margin-top:20px; }}
  button {{ flex:1; padding:12px; font-size:16px; border-radius:10px; border:0; cursor:pointer; }}
  .primary {{ background:var(--accent); color:#fff; font-weight:600; }}
  .secondary {{ background:transparent; color:var(--muted); border:1px solid color-mix(in srgb, var(--muted) 40%, transparent); }}
  .error {{ color:var(--err); font-weight:600; }}
  .note {{ font-size:14px; margin-top:16px; margin-bottom:0; }}
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


def _error(error: str | None) -> str:
    return f'<p class="error">{html.escape(error)}</p>' if error else ""


def _buttons(primary_action: str, primary_label: str) -> str:
    # The primary button must come first: pressing Enter submits with the first
    # submit button. row-reverse still shows Cancel on the left.
    return f"""<div class="row">
    <button class="primary" type="submit" name="action" value="{primary_action}">{primary_label}</button>
    <button class="secondary" type="submit" name="action" value="deny" formnovalidate>Cancel</button>
  </div>"""


def _signin_form(request_id: str, client_name: str, redirect_host: str, email: str = "", error: str | None = None) -> str:
    return f"""
<h1>Sign in with Garmin</h1>
<p><strong>{html.escape(client_name)}</strong> wants read-only access to your Garmin Connect data.
Afterwards you are sent back to <strong>{html.escape(redirect_host)}</strong>.</p>
{_error(error)}
<form method="post" action="/login">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label for="email">Garmin email</label>
  <input id="email" name="email" type="email" autocomplete="username" value="{html.escape(email)}" required autofocus>
  <label for="password">Garmin password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  {_buttons("signin", "Sign in")}
</form>
<p class="note">Your password is sent to Garmin once to sign in and is not stored. Only invited
Garmin accounts can use this server.</p>"""


def _mfa_form(request_id: str, error: str | None = None) -> str:
    return f"""
<h1>Verification code</h1>
<p>Garmin has sent a code to your email or phone. Enter it to finish signing in.</p>
{_error(error)}
<form method="post" action="/login">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label for="code">Code</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{4,12}}" required autofocus>
  {_buttons("mfa", "Continue")}
</form>"""


def _garmin_start(email: str, password: str) -> tuple[str | None, Any, Garmin]:
    api = Garmin(email, password, return_on_mfa=True)
    status, state = api.login()  # no token store: always a fresh sign-in
    return status, state, api


def add_routes(server, provider: GarminOAuthProvider) -> None:
    """Register /login and /health on an MCPServer."""
    config = provider.config

    async def finish(request_id: str, login_request: _LoginRequest, email: str, api: Garmin) -> Response:
        subject = user_id(email)
        token_dir = config.token_dir(subject, legacy_dir="")
        Path(token_dir).mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(api.client.dump, token_dir)
        provider.signed_in(email)
        log.warning("%s signed in with Garmin.", mask_email(email))
        return RedirectResponse(provider.issue_code(request_id, subject), status_code=302, headers=SECURITY_HEADERS)

    def garmin_unavailable(e: Exception) -> Response:
        if isinstance(e, GarminConnectTooManyRequestsError):
            return _message("Garmin is busy", "Garmin is limiting sign-ins right now. Try again in a few minutes.", 429)
        log.warning("Garmin sign-in failed: %s", e)
        return _message("Could not reach Garmin", "Signing in to Garmin failed. Try again in a few minutes.", 502)

    @server.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request) -> Response:
        form = await request.form() if request.method == "POST" else None
        request_id = str(form.get("request", "")) if form else request.query_params.get("request", "")
        login_request = provider.login_requests.get(request_id)
        if not login_request or login_request.expires_at < time.time():
            return _message("This link has expired", "Start connecting again from Claude.", 400)
        client = await provider.get_client(login_request.client_id)
        client_name = (client.client_name if client else None) or "An MCP client"
        redirect_host = urlparse(str(login_request.params.redirect_uri)).hostname or "the app"

        if form is None:
            if login_request.garmin is not None:
                return _page(_mfa_form(request_id))
            return _page(_signin_form(request_id, client_name, redirect_host))

        action = form.get("action")
        if action == "deny":
            provider.login_requests.pop(request_id, None)
            target = construct_redirect_uri(
                str(login_request.params.redirect_uri), error="access_denied", state=login_request.params.state
            )
            return RedirectResponse(target, status_code=302, headers=SECURITY_HEADERS)

        if action == "mfa":
            if login_request.garmin is None or not login_request.email:
                return _message("This link has expired", "Start connecting again from Claude.", 400)
            email = login_request.email
            if provider.locked_out(email):
                provider.login_requests.pop(request_id, None)
                return _message("Too many attempts", "Wait 15 minutes and try again.", 429)
            code = re.sub(r"\s", "", str(form.get("code", "")))
            try:
                await asyncio.to_thread(login_request.garmin.resume_login, login_request.mfa_state, code)
            except GarminConnectAuthenticationError:
                provider.record_failure(email)
                return _page(_mfa_form(request_id, "Garmin did not accept that code."), 401)
            except Exception as e:
                provider.login_requests.pop(request_id, None)
                return garmin_unavailable(e)
            return await finish(request_id, login_request, email, login_request.garmin)

        # Sign in with email and password
        email = normalize_email(str(form.get("email", "")))
        password = str(form.get("password", ""))
        if not email or not password:
            return _page(_signin_form(request_id, client_name, redirect_host, email, "Enter your Garmin email and password."), 400)
        if email not in config.allowed_emails:
            log.warning("Sign-in refused for %s: not on ALLOWED_EMAILS.", mask_email(email))
            return _page(_signin_form(
                request_id, client_name, redirect_host, email,
                "This Garmin account is not invited to this server. Ask the person who runs it to add your email.",
            ), 403)
        if provider.locked_out(email):
            return _message("Too many attempts", "Wait 15 minutes and try again.", 429)
        try:
            status, state, api = await asyncio.to_thread(_garmin_start, email, password)
        except GarminConnectAuthenticationError:
            provider.record_failure(email)
            await asyncio.sleep(1)
            return _page(_signin_form(
                request_id, client_name, redirect_host, email, "Garmin did not accept that email and password."
            ), 401)
        except Exception as e:
            return garmin_unavailable(e)
        if status == "needs_mfa":
            login_request.email, login_request.garmin, login_request.mfa_state = email, api, state
            login_request.expires_at = time.time() + LOGIN_TTL
            return _page(_mfa_form(request_id))
        return await finish(request_id, login_request, email, api)

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})
