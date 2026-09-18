# Expected resolution

The MCP Bitbucket snapshot now corroborates the Jira and Notion migration
claim from phase 02.

Expected behavior:

- close or invalidate MCP's `CALLS_ENDPOINT POST /v1/auth` fact;
- add `CALLS_ENDPOINT POST /v2/auth`;
- resolve the documentation/code mismatch;
- remove MCP from the live Auth API v1 impact signal;
- retain Argo in the blast radius because its repository is unchanged.

