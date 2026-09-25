# Expected mismatch

This phase intentionally contains no MCP Bitbucket update.

Jira and Notion state that MCP now uses `POST /v2/auth`, while the latest
accepted Bitbucket snapshot from `baseline/mcp-gateway/bitbucket.json` still
contains `AUTH_API_VERSION = "v1"` and `/v1/auth`.

The semantic claim should be preserved, but it must not silently close the
stronger current-code dependency. The expected output is a contested claim or
documentation/code mismatch signal.

