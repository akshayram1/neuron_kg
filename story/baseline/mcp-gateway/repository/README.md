# MCP Gateway

MCP Gateway consumes Auth API v1 from Auth Service. `src/auth_client.py` sends `POST /v1/auth` before opening a tool session.

This dependency was introduced by MCP-101. MCP-102 added two retries for transport failures. The repository has not migrated to Auth API v2.
