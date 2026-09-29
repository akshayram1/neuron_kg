# Synthetic fixture data schema

`connectors/synthetic/loader.py` reads these files under `synthetic/mcp-access/`
(gitignored — this is data, not code) and parses them with the **same code
paths the live connector clients use** (`connectors/jira/api.py`,
`connectors/bitbucket/api.py`), so a fixture must match each provider's real
API response shape. This is a reference for building a new story/scenario by
hand-writing these files.

No field is validated strictly — everything is read with `.get(...)` and
sensible defaults, so a missing optional field never crashes ingestion. What's
below marks what's actually read (and therefore worth setting to test
something) vs. ignored.

## Jira — `jira_real/issues.json`

One file: a JSON **list** of issue objects (real Jira `/search/jql` result
items, minus the numeric `id` — the loader synthesizes `id = key` since a
fixture has no real internal id to capture).

```json
[
  {
    "key": "DATAOS-1507",
    "fields": {
      "summary": "Tenant-based access filtering for search results",
      "description": "What\nImplemented tenant-based access control...\nWhy\n...",
      "status": { "name": "Done" },
      "issuetype": { "name": "Sub-task" },
      "project": { "key": "DATAOS", "name": "DataOS 2.0", "projectTypeKey": "software" },
      "assignee": { "accountId": "712020:...", "displayName": "Akshay Chame", "emailAddress": "a@x.com" },
      "reporter": { "accountId": "712020:...", "displayName": "Sahil Sasane" },
      "labels": ["security", "auth"],
      "created": "2026-05-14T14:17:47.124+0530",
      "updated": "2026-07-21T14:17:57.219+0530",
      "parent": { "id": "DATAOS-1500", "key": "DATAOS-1500" },
      "comment": { "comments": [
        { "author": { "displayName": "Sahil Sasane" }, "body": "LGTM" }
      ]},
      "issuelinks": [
        { "type": { "outward": "blocks" }, "outwardIssue": { "id": "DATAOS-1508" } }
      ]
    }
  }
]
```


| Field                                                    | Used for                                                                           | Notes                                                                                                                                                                                                                   |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `key`                                                    | issue key, and its synthesized `id`                                                | required                                                                                                                                                                                                                |
| `fields.summary`                                         | title                                                                              | falls back to `key`                                                                                                                                                                                                     |
| `fields.description`                                     | body text                                                                          | plain string **or** an ADF rich-text node (`{"type":"doc","content":[...]}`) — both parse                                                                                                                               |
| `fields.status.name`                                     | "Status: X" line in embedded/searchable text                                       |                                                                                                                                                                                                                         |
| `fields.issuetype.name`                                  | "Type: X" line                                                                     |                                                                                                                                                                                                                         |
| `fields.project.key`/`.name`                             | the one synthetic Jira `Project` node (taken from the **first** issue in the file) | every issue should share the same project for one story                                                                                                                                                                 |
| `fields.assignee`/`.reporter`                            | `{accountId, displayName, emailAddress?}` → `Person` nodes                         | `assignee: null` → unassigned (tested: works)                                                                                                                                                                           |
| `fields.labels`                                          | list of strings                                                                    |                                                                                                                                                                                                                         |
| `fields.created`/`.updated`                              | timestamps                                                                         | any ISO-ish string works                                                                                                                                                                                                |
| `fields.parent.id`/`.key`                                | Epic/Story parent link                                                             |                                                                                                                                                                                                                         |
| `fields.comment.comments[].author.displayName` / `.body` | embedded as "Comment by X: ..."                                                    | `body` is plain string or ADF                                                                                                                                                                                           |
| `fields.issuelinks[]`                                    | "blocks" relation                                                                  | `outwardIssue.id` must equal **another issue's** `key` in this same file (since synthesized id = key) and `type.outward` must contain the substring `"block"`                                                           |
| `changelog` (sibling of `fields`, **not inside** it)     | assignee/status history over time                                                  | `{"histories":[{"created":"...","author":{"displayName":"..."},"items":[{"field":"status","from":null,"fromString":null,"to":"3","toString":"In Progress"}]}]}` — only `field: "assignee"` or `"status"` items are kept |




## Bitbucket — `bitbucket_real/pull_requests/*.json` + `bitbucket_real/pr_commits/{pr_id}.json`

Any number of files under `pull_requests/`, each shaped like one page of the
real `GET /repositories/{workspace}/{slug}/pullrequests` response:

```json
{ "values": [
  {
    "id": 40,
    "title": "[DATAOS-3572] feat(auth): integrate PEP enforcement across MCP service",
    "description": "## Summary\n...",
    "state": "MERGED",
    "author": { "display_name": "Sahil Sasane", "uuid": "{447bb2a0-...}" },
    "source": { "branch": { "name": "feat/pep-auth-2.0" } },
    "destination": {
      "branch": { "name": "main" },
      "repository": { "full_name": "rubik_/mcp", "uuid": "{eff7a048-...}", "name": "mcp" }
    },
    "created_on": "2026-07-14T08:54:20+00:00",
    "updated_on": "2026-07-14T14:16:23+00:00"
  }
]}
```

One file per PR under `pr_commits/`, **named** `{pr_id}.json` (e.g. `40.json`
for PR id 40 — the loader keys commits to their PR by parsing the filename
stem as an int; a non-numeric filename silently groups under PR id `0`):

```json
{ "values": [
  {
    "hash": "e96ea2d1fc8202c6e8ac2d474e339bff8258b4c8",
    "message": "Merged in feat/pep-auth-2.0 (pull request #40)\n\n[DATAOS-3572] feat(auth): ...",
    "author": { "user": { "display_name": "Sahil Sasane" } },
    "date": "2026-07-14T14:16:21+00:00"
  }
]}
```


| Field                                                                         | Used for                                                                                                                                                                                                                       | Notes                                                                                                    |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `id`                                                                          | PR id, and how its commits file is matched                                                                                                                                                                                     |                                                                                                          |
| `title`/`description`                                                         | embedded text (description drives the searchable/embedded content)                                                                                                                                                             |                                                                                                          |
| `state`                                                                       | `OPEN`|`MERGED`|`DECLINED`|`SUPERSEDED` — stored as a **structured property only**, not embedded into the searchable text (see the PR #35 finding earlier this session — the LLM can't see a PR's merge state from text alone) |                                                                                                          |
| `author.display_name`/`.uuid`                                                 | `Person` node                                                                                                                                                                                                                  |                                                                                                          |
| `source.branch.name` / `destination.branch.name`                              |                                                                                                                                                                                                                                |                                                                                                          |
| `destination.repository` (or `source.repository`, or a commit's `repository`) | the **one** synthetic `Repository` node — taken from whichever PR/commit is encountered first                                                                                                                                  | only needs to appear once across all your PR/commit files; `full_name` splits on `/` into workspace/slug |
| `created_on`/`updated_on`                                                     | timestamps                                                                                                                                                                                                                     |                                                                                                          |
| commit `hash`                                                                 | commit id                                                                                                                                                                                                                      |                                                                                                          |
| commit `message`                                                              | **empty message = commit is silently dropped** (mirrors the real client)                                                                                                                                                       |                                                                                                          |
| commit `author.user.display_name` (or `author.raw: "Name <email>"`)           | `Person` node + email extraction                                                                                                                                                                                               |                                                                                                          |
| commit `date`                                                                 | timestamp                                                                                                                                                                                                                      |                                                                                                          |




## Notion — `notion_real/search_mcp.json` + `notion_real/pages/{page_id}.json`

One file, shaped like the real `/search` API response:

```json
{ "results": [
  {
    "id": "35fc5c1d-4876-801b-94dd-e6823b98e445",
    "url": "https://app.notion.com/p/Some-Page-35fc5c1d...",
    "last_edited_time": "2026-07-28T07:33:00.000Z",
    "parent": { "type": "page_id", "page_id": "<parent-page-id>" },
    "properties": {
      "title": { "type": "title", "title": [
        { "plain_text": "Authentication Flow Implementation of DCR" }
      ]}
    }
  }
]}
```

Then one file per page id under `pages/`, **named** `{id}.json` matching the
`id` above:

```json
{ "id": "35fc5c1d-4876-801b-94dd-e6823b98e445", "markdown": "## Overview\nThe MCP server supports..." }
```


| Field                                                                    | Used for                                                                                  | Notes                                                                                                                                           |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `results[].id`                                                           | page id, and the sibling `pages/{id}.json` filename                                       | required                                                                                                                                        |
| `results[].url`                                                          |                                                                                           |                                                                                                                                                 |
| `results[].last_edited_time`                                             |                                                                                           |                                                                                                                                                 |
| `results[].parent.type`/`.page_id`                                       | nested-page hierarchy (`PARENT_OF` edge)                                                  | omit `parent`, or set `type` to anything other than `"page_id"`, for a top-level page                                                           |
| `results[].properties.<any-key>.type == "title"` → `.title[].plain_text` | page title                                                                                | the loader scans every property for the one with `type: "title"`; falls back to "Untitled"                                                      |
| `pages/{id}.json`'s `markdown`                                           | the page's **entire embedded/searchable content, and what the LLM extraction pass reads** | this is the only provider whose ingestion runs the LLM semantic pass (facts, entities, and now `Finding` nodes — see `graph/finding_bridge.py`) |






