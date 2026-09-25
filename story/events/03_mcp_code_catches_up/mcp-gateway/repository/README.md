# MCP Gateway

MCP Gateway consumes Auth API v2 from Auth Service. `src/auth_client.py` sends `POST /v2/auth` before opening a tool session.

MCP-150 migrated the production client from v1 to v2. The repository no longer calls `/v1/auth`.
