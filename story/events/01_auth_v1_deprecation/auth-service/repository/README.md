# Auth Service

AUTH-900 approves removal of `POST /v1/auth` on 2026-10-01. `POST /v2/auth` replaces it.

Known v1 consumers are MCP Gateway and Argo Orchestrator. Both must migrate before the sunset date. The v1 route remains live until the scheduled removal; this commit announces the change but does not remove the route yet.
