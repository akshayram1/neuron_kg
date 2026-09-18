# Auth Service

Auth Service provides two authentication contracts:

- `POST /v1/auth` — current production contract introduced by AUTH-301.
- `POST /v2/auth` — migration contract introduced by AUTH-302.

MCP Gateway and Argo Orchestrator currently consume Auth API v1. Adding v2 does not deprecate or remove v1. Both routes remain active until a separate removal decision is approved.
