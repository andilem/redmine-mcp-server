#!/usr/bin/env bash
export REDMINE_AUTH_MODE=redmine-login
export REDMINE_MCP_BASE_URL=http://localhost:52484
export REDMINE_OAUTH_SCOPE_ENFORCEMENT=off
export REDMINE_URL=https://psc.mmlab.de
export SERVER_HOST=127.0.0.1
export SERVER_PORT=52484
export REDMINE_MCP_READ_ONLY=true
exec ./.venv/Scripts/python.exe -m redmine_mcp_server.main
