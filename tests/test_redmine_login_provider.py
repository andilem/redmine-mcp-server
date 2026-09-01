"""A restart keeps sessions alive, and only the right token opens them."""

import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from redmine_mcp_server._binding_store import BindingStore
from redmine_mcp_server._redmine_login import LOGIN_PATH, RedmineLoginProvider

BASE = "https://mcp.example.invalid"
REDMINE = "https://redmine.example.invalid"
LOGIN = "alr"
API_KEY = "k" * 40


def _client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=["http://localhost:9999/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
    )


def _provider(tmp_path, subdir="bindings") -> RedmineLoginProvider:
    return RedmineLoginProvider(
        base_url=BASE, redmine_url=REDMINE, store=BindingStore(tmp_path / subdir)
    )


async def _issue(provider, client):
    """Walk authorize -> login -> code -> token, returning the OAuthToken."""
    from mcp.server.auth.provider import AuthorizationParams

    await provider.register_client(client)
    params = AuthorizationParams(
        state="s",
        scopes=[],
        code_challenge="c" * 43,
        redirect_uri=client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
        resource=f"{BASE}/mcp",
    )
    url = await provider.authorize(client, params)
    login_id = url.split("login_id=")[1]
    redirect = await provider.complete_login(login_id, LOGIN, API_KEY)
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, auth_code)


@pytest.mark.asyncio
async def test_authorize_points_at_the_login_page(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    from mcp.server.auth.provider import AuthorizationParams

    url = await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=[],
            code_challenge="c" * 43,
            redirect_uri=client.redirect_uris[0],
            redirect_uri_provided_explicitly=True,
            resource=None,
        ),
    )
    assert url.startswith(f"{BASE}{LOGIN_PATH}?login_id=")


@pytest.mark.asyncio
async def test_login_id_is_single_use(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    from mcp.server.auth.provider import AuthorizationParams

    url = await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=[],
            code_challenge="c" * 43,
            redirect_uri=client.redirect_uris[0],
            redirect_uri_provided_explicitly=True,
            resource=None,
        ),
    )
    login_id = url.split("login_id=")[1]
    assert provider.has_pending(login_id)
    assert await provider.complete_login(login_id, LOGIN, API_KEY) is not None
    assert not provider.has_pending(login_id)
    assert await provider.complete_login(login_id, LOGIN, API_KEY) is None


@pytest.mark.asyncio
async def test_the_token_carries_the_callers_key(tmp_path):
    provider = _provider(tmp_path)
    token = await _issue(provider, _client())
    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.subject == LOGIN
    assert access.claims["redmine_api_key"] == API_KEY


@pytest.mark.asyncio
async def test_a_restart_keeps_the_session(tmp_path):
    """A fresh provider on the same store still honours the token."""
    issued = await _issue(_provider(tmp_path), _client())

    restarted = _provider(tmp_path)
    assert restarted.access_tokens == {}  # nothing in memory
    access = await restarted.load_access_token(issued.access_token)
    assert access is not None
    assert access.claims["redmine_api_key"] == API_KEY


@pytest.mark.asyncio
async def test_a_restart_keeps_the_client_registration(tmp_path):
    await _issue(_provider(tmp_path), _client())
    restarted = _provider(tmp_path)
    assert restarted.clients == {}
    assert (await restarted.get_client("client-1")) is not None
    assert (await restarted.get_client("client-unknown")) is None


@pytest.mark.asyncio
async def test_refresh_works_after_a_restart_and_rotates_the_binding(tmp_path):
    issued = await _issue(_provider(tmp_path), _client())
    assert issued.refresh_token

    restarted = _provider(tmp_path)
    client = await restarted.get_client("client-1")
    refresh = await restarted.load_refresh_token(client, issued.refresh_token)
    assert refresh is not None
    rotated = await restarted.exchange_refresh_token(client, refresh, [])

    access = await restarted.load_access_token(rotated.access_token)
    assert access is not None
    assert access.claims["redmine_api_key"] == API_KEY


@pytest.mark.asyncio
async def test_a_refresh_token_of_another_client_is_refused(tmp_path):
    issued = await _issue(_provider(tmp_path), _client())
    restarted = _provider(tmp_path)
    other = _client("client-2")
    await restarted.register_client(other)
    assert await restarted.load_refresh_token(other, issued.refresh_token) is None


@pytest.mark.asyncio
async def test_an_unbound_token_is_rejected(tmp_path):
    provider = _provider(tmp_path)
    assert await provider.load_access_token("made-up-token") is None


@pytest.mark.asyncio
async def test_a_session_is_not_shared_across_stores(tmp_path):
    """The store is the only carrier; a different directory knows nothing."""
    issued = await _issue(_provider(tmp_path, "one"), _client())
    elsewhere = _provider(tmp_path, "two")
    assert await elsewhere.load_access_token(issued.access_token) is None


@pytest.mark.asyncio
async def test_without_a_store_a_restart_signs_everyone_out(tmp_path):
    provider = RedmineLoginProvider(base_url=BASE, redmine_url=REDMINE, store=None)
    issued = await _issue(provider, _client())
    assert await provider.load_access_token(issued.access_token) is not None

    restarted = RedmineLoginProvider(base_url=BASE, redmine_url=REDMINE, store=None)
    assert await restarted.load_access_token(issued.access_token) is None


@pytest.mark.asyncio
async def test_revoking_removes_the_stored_record(tmp_path):
    provider = _provider(tmp_path)
    issued = await _issue(provider, _client())
    access = await provider.load_access_token(issued.access_token)
    await provider.revoke_token(access)

    restarted = _provider(tmp_path)
    assert await restarted.load_access_token(issued.access_token) is None


@pytest.mark.asyncio
async def test_an_expired_stored_access_token_is_refused(tmp_path):
    provider = _provider(tmp_path)
    issued = await _issue(provider, _client())
    # Rewrite the record as already expired, the way the clock would.
    store = BindingStore(tmp_path / "bindings")
    payload = store.get_token(issued.access_token)
    payload["expires_at"] = int(time.time()) - 5
    store.put_token(issued.access_token, payload)

    restarted = _provider(tmp_path)
    assert await restarted.load_access_token(issued.access_token) is None
