# Argo Orchestrator

Argo Orchestrator consumes Auth API v1. `src/token_client.py` calls `POST /v1/auth` before starting a workflow.

ARGO-201 introduced the dependency and ARGO-202 documented failure behavior. Argo has not migrated to Auth API v2.
