"""SPIKE stage 2: the login page that turns Redmine credentials into a token.

Two routes, registered on the FastMCP instance so they sit next to
``/authorize`` and ``/token``:

- ``GET  /redmine-login?login_id=…`` renders the form
- ``POST /redmine-login`` verifies the credentials and redirects back to the
  MCP client with an authorization code

The ``login_id`` is minted by
:meth:`RedmineLoginProvider.authorize` and is single-use, so a form that
is submitted twice, replayed, or forged finds nothing pending and is refused.
"""

import html
import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ._offload import in_thread
from ._redmine_login import LOGIN_PATH, fetch_api_key

logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redmine-Anmeldung</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 26rem;
        margin: 4rem auto; padding: 0 1rem; }}
 h1 {{ font-size: 1.25rem; margin-bottom: .25rem; }}
 p.sub {{ color: #666; margin-top: 0; }}
 label {{ display: block; margin-top: 1rem; font-weight: 600; }}
 input {{ width: 100%; padding: .5rem; margin-top: .25rem; font: inherit;
         border: 1px solid #999; border-radius: .25rem; box-sizing: border-box; }}
 button {{ margin-top: 1.5rem; padding: .55rem 1.1rem; font: inherit;
          border-radius: .25rem; border: 1px solid transparent;
          background: #2563eb; color: #fff; cursor: pointer; }}
 .err {{ margin-top: 1rem; padding: .6rem .75rem; border-radius: .25rem;
        background: #fee2e2; color: #991b1b; }}
 .opt {{ font-weight: 400; color: #777; }}
 .note {{ margin-top: 2rem; font-size: .85rem; color: #666;
         border-top: 1px solid #ddd; padding-top: 1rem; }}
</style></head><body>
<h1>Redmine-Anmeldung</h1>
<p class="sub">{redmine}</p>
{error}
<form method="post" action="{action}">
  <input type="hidden" name="login_id" value="{login_id}">
  <label>Benutzername oder API-Key
    <input name="login" autocomplete="username" autofocus required></label>
  <label>Passwort <span class="opt">(bei API-Key beliebig)</span>
    <input name="password" type="password" autocomplete="current-password"
           value="x" required></label>
  <button type="submit">Anmelden</button>
</form>
<p class="note">Empfohlen: trag deinen <b>API-Key</b> als Benutzernamen ein
(Redmine → Mein Konto) und lass das Passwortfeld wie es ist. Dann verlässt
dein Passwort den Browser nie. Alternativ Benutzername und Passwort — beides
wird einmal benutzt, um deinen persönlichen API-Key abzurufen, und weder
gespeichert noch protokolliert. Danach handelt Claude mit deinen Rechten.</p>
</body></html>
"""


def _render(login_id: str, redmine_url: str, error: str | None = None) -> HTMLResponse:
    block = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = _PAGE.format(
        redmine=html.escape(redmine_url),
        error=block,
        action=html.escape(LOGIN_PATH),
        login_id=html.escape(login_id),
    )
    return HTMLResponse(body, headers=_NO_STORE)


def _refused(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><p>{html.escape(message)}</p>",
        status_code=400,
        headers=_NO_STORE,
    )


def register_login_routes(mcp: Any, provider: Any, redmine_url: str) -> None:
    """Attach the login routes to ``mcp`` for the Redmine-login auth mode."""

    verify = os.getenv("REDMINE_SSL_VERIFY", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    @mcp.custom_route(LOGIN_PATH, methods=["GET"])
    async def login_form(request: Request):  # pragma: no cover - browser flow
        login_id = request.query_params.get("login_id", "")
        if not login_id or not provider.has_pending(login_id):
            return _refused(
                "Diese Anmeldeseite ist abgelaufen. Starte die Verbindung im "
                "Client neu."
            )
        return _render(login_id, redmine_url)

    @mcp.custom_route(LOGIN_PATH, methods=["POST"])
    async def login_submit(request: Request):  # pragma: no cover - browser flow
        form = await request.form()
        login_id = str(form.get("login_id") or "")
        login = str(form.get("login") or "").strip()
        password = str(form.get("password") or "")

        if not login_id or not provider.has_pending(login_id):
            return _refused(
                "Diese Anmeldeseite ist abgelaufen oder wurde doppelt "
                "abgeschickt. Starte die Verbindung im Client neu."
            )
        if not login or not password:
            return _render(
                login_id, redmine_url, "Benutzername oder API-Key ist nötig."
            )

        binding, error = await in_thread(
            fetch_api_key, redmine_url, login, password, verify=verify
        )
        if binding is None:
            # login_id stays pending so the user can correct a typo
            return _render(login_id, redmine_url, error or "Anmeldung fehlgeschlagen.")

        resolved_login, api_key = binding
        redirect = await provider.complete_login(login_id, resolved_login, api_key)
        if redirect is None:
            return _refused(
                "Die Anmeldung konnte nicht abgeschlossen werden. Starte die "
                "Verbindung im Client neu."
            )
        logger.info("Redmine login succeeded for %s", resolved_login)
        return RedirectResponse(redirect, status_code=302, headers=_NO_STORE)
