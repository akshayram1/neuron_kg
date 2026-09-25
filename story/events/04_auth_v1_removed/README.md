# Event 04 — Auth v1 removal exposes Argo

Auth Service removes `POST /v1/auth` after a readiness page claims every
consumer migrated. Argo's current production client still calls the removed
route, and an incident records blocked workflows. The previously predicted
blast-radius risk becomes a materialized failure.
