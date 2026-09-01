"""SPIKE stage 2: this server is the authorization server, Redmine is the
identity provider.

``/authorize`` sends the browser to a login page served here. The user enters
their Redmine credentials; the server validates them with Basic auth against
``GET /users/current.json``, which returns *that user's own* ``api_key``, and
binds the key to the token it issues. Every later tool call runs with the
caller's key, so identity and permissions are the caller's.

Why this shape:

- Redmine does not have to be an OAuth provider. Easy Redmine is not one --
  its ``/easy_oauth`` and SAML endpoints are consumer-side -- so the
  ``oauth`` and ``oauth-proxy`` modes, which need Doorkeeper introspection,
  cannot be used there.
- Nothing per-user lives in an environment variable, which is what
  ``legacy-per-user`` requires and what Claude Cowork cannot supply.

The password is used once, in the browser, to fetch the ``api_key``. It is
never stored and never logged.

Where the key then lives depends on ``REDMINE_MCP_BINDING_STORE``. Unset, it
stays in memory and a restart signs everyone out. Set, the binding is written
to that directory encrypted under the client's own token, so a restart keeps
sessions alive while the server still cannot decrypt anything at rest -- see
:mod:`._binding_store`.
"""

import logging
import secrets
import urllib.parse
from typing import Any, Optional, Tuple

import requests
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ._binding_store import BindingStore

logger = logging.getLogger(__name__)

LOGIN_PATH = "/redmine-login"

# login -> api_key, as resolved at sign-in time.
Binding = Tuple[str, str]


class RedmineLoginProvider(InMemoryOAuthProvider):
    """Authorization server whose login step authenticates against Redmine."""

    def __init__(
        self,
        base_url: str,
        redmine_url: str,
        store: Optional[BindingStore] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            resource_base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            **kwargs,
        )
        self._base_url = base_url.rstrip("/")
        self._redmine_url = redmine_url.rstrip("/")
        # Without a store the bindings are memory-only and a restart signs
        # everyone out, which is the previous behaviour.
        self._store = store
        if store is not None:
            removed = store.prune()
            if removed:
                logger.info("Binding store: pruned %d expired record(s).", removed)
        # login_id -> (client_id, params), one entry per browser round-trip
        self._pending: dict[str, Tuple[str, AuthorizationParams]] = {}
        # authorization code -> binding, consumed at token exchange
        self._code_bindings: dict[str, Binding] = {}
        # access/refresh token -> binding
        self._token_bindings: dict[str, Binding] = {}

    # --- authorize: hand the browser our own login page ----------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if client.client_id is None:
            raise AuthorizeError(
                error="invalid_client", error_description="Client ID is required"
            )
        login_id = secrets.token_urlsafe(24)
        self._pending[login_id] = (client.client_id, params)
        query = urllib.parse.urlencode({"login_id": login_id})
        return f"{self._base_url}{LOGIN_PATH}?{query}"

    def has_pending(self, login_id: str) -> bool:
        return login_id in self._pending

    async def complete_login(
        self, login_id: str, login: str, api_key: str
    ) -> Optional[str]:
        """Mint an authorization code for a verified login.

        Returns the client's redirect URI carrying code and state, or ``None``
        when the login_id is unknown (expired, replayed, or forged).
        """
        entry = self._pending.pop(login_id, None)
        if entry is None:
            return None
        client_id, params = entry
        client = await self.get_client(client_id)
        if client is None:
            return None
        # The base class mints and stores the code; we only bind the key to it.
        redirect = await super().authorize(client, params)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query).get(
            "code", [None]
        )[0]
        if code:
            self._code_bindings[code] = (login, api_key)
        return redirect

    # --- token exchange: carry the binding onto the tokens -------------

    def _bind(self, token: OAuthToken, binding: Binding) -> None:
        self._token_bindings[token.access_token] = binding
        if token.refresh_token:
            self._token_bindings[token.refresh_token] = binding
        if self._store is None:
            return
        # Persist enough to rebuild the token object after a restart, since
        # the base class keeps issued tokens in memory only.
        login, api_key = binding
        access = self.access_tokens.get(token.access_token)
        if access is not None:
            self._store.put_token(
                token.access_token,
                {
                    "kind": "access",
                    "login": login,
                    "api_key": api_key,
                    "client_id": access.client_id,
                    "scopes": list(access.scopes),
                    "expires_at": access.expires_at,
                    "resource": access.resource,
                },
            )
        if token.refresh_token:
            refresh = self.refresh_tokens.get(token.refresh_token)
            if refresh is not None:
                self._store.put_token(
                    token.refresh_token,
                    {
                        "kind": "refresh",
                        "login": login,
                        "api_key": api_key,
                        "client_id": refresh.client_id,
                        "scopes": list(refresh.scopes),
                        "expires_at": refresh.expires_at,
                    },
                )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        binding = self._code_bindings.pop(authorization_code.code, None)
        token = await super().exchange_authorization_code(client, authorization_code)
        if binding:
            self._bind(token, binding)
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        binding = self._token_bindings.get(refresh_token.token)
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        if binding:
            self._bind(token, binding)
        return token

    async def revoke_token(self, token: Any) -> None:  # type: ignore[override]
        value = getattr(token, "token", None)
        if value:
            self._token_bindings.pop(value, None)
            if self._store is not None:
                self._store.delete_token(value)
        await super().revoke_token(token)

    # --- client registrations: survive a restart ----------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        if self._store is not None and client_info.client_id:
            self._store.put_client(
                client_info.client_id, client_info.model_dump(mode="json")
            )

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        client = await super().get_client(client_id)
        if client is not None:
            return client
        if self._store is None:
            return None
        data = self._store.get_client(client_id)
        if data is None:
            return None
        try:
            client = OAuthClientInformationFull.model_validate(data)
        except Exception:  # pragma: no cover - would mean we wrote garbage
            logger.warning(
                "Binding store: client record for %s is unusable.", client_id
            )
            return None
        self.clients[client_id] = client
        return client

    # --- verification: expose the binding to the Redmine client --------

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        access = await super().load_access_token(token)
        if access is None:
            access = self._rehydrate_access(token)
            if access is None:
                return None
        binding = self._token_bindings.get(token)
        if binding is None:
            # A token this process did not bind -- refuse rather than fall back
            # to some other identity.
            logger.info("Access token carries no Redmine binding; rejecting.")
            return None
        login, api_key = binding
        access.subject = login
        access.claims = {
            **(access.claims or {}),
            "redmine_login": login,
            "redmine_api_key": api_key,
        }
        return access

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        token = await super().load_refresh_token(client, refresh_token)
        if token is not None:
            return token
        record = self._payload(refresh_token, "refresh")
        if record is None or record["client_id"] != client.client_id:
            return None
        token = RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=list(record.get("scopes") or []),
            expires_at=record.get("expires_at"),
        )
        self.refresh_tokens[refresh_token] = token
        self._token_bindings[refresh_token] = (record["login"], record["api_key"])
        return token

    # --- rehydration from the store ------------------------------------

    def _payload(self, token: str, kind: str) -> Optional[dict[str, Any]]:
        """Decrypt the stored payload for ``token``, if it is of ``kind``."""
        if self._store is None:
            return None
        record = self._store.get_token(token)
        if record is None or record.get("kind") != kind:
            return None
        if not record.get("login") or not record.get("api_key"):
            return None
        return record

    def _rehydrate_access(self, token: str) -> Optional[AccessToken]:
        """Rebuild an access token issued before a restart."""
        record = self._payload(token, "access")
        if record is None:
            return None
        access = AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=list(record.get("scopes") or []),
            expires_at=record.get("expires_at"),
            resource=record.get("resource"),
        )
        self.access_tokens[token] = access
        self._token_bindings[token] = (record["login"], record["api_key"])
        logger.info(
            "Rehydrated a session for %s from the binding store.", record["login"]
        )
        return access


def fetch_api_key(
    redmine_url: str,
    login: str,
    password: str,
    *,
    timeout: float = 15.0,
    verify: Any = True,
) -> Tuple[Optional[Binding], Optional[str]]:
    """Validate credentials against Redmine and return ``(login, api_key)``.

    Blocking; call it off the event loop. Returns ``(None, message)`` on
    failure. The password is not logged, and neither is the key.
    """
    url = f"{redmine_url}/users/current.json"
    try:
        response = requests.get(
            url, auth=(login, password), timeout=timeout, verify=verify
        )
    except requests.RequestException as exc:
        logger.warning("Redmine login probe failed: %s", exc.__class__.__name__)
        return None, "Redmine ist nicht erreichbar."
    if response.status_code in (401, 403):
        return None, "Benutzername oder Passwort ist falsch."
    if response.status_code >= 400:
        logger.warning("Redmine login probe returned %s", response.status_code)
        return None, f"Redmine antwortete mit HTTP {response.status_code}."
    try:
        user = response.json().get("user") or {}
    except ValueError:
        return None, "Redmine lieferte keine verwertbare Antwort."
    api_key = user.get("api_key")
    if not api_key:
        return None, (
            "Redmine hat keinen API-Key mitgeliefert. Ist die REST-API unter "
            "Administration → Konfiguration → API aktiviert?"
        )
    return (user.get("login") or login, api_key), None
