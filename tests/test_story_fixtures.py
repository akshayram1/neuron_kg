import json
from pathlib import Path

from connectors.bitbucket.api import BitbucketCommit, BitbucketFile, BitbucketRepository
from connectors.jira.api import JiraIssue, JiraProject, JiraSite
from connectors.notion.api import NotionPage
from connectors.story.loader import PHASES, StoryIngestor


STORY_ROOT = Path(__file__).resolve().parents[1] / "story"


def test_every_story_phase_has_connector_shaped_payloads():
    seen = {"jira": 0, "notion": 0, "bitbucket": 0}
    for relative in PHASES.values():
        root = STORY_ROOT / relative
        for path in root.rglob("jira.json"):
            payload = json.loads(path.read_text())
            JiraSite(**payload["site"])
            JiraProject(**payload["project"])
            for item in payload["issues"]:
                # Nested people/change objects are validated by the story
                # adapter; this verifies the connector's required top-level contract.
                JiraIssue(**{
                    **item, "assignee": None, "reporter": None,
                    "labels": tuple(item.get("labels") or ()),
                    "blocks": tuple(item.get("blocks") or ()), "changes": (),
                })
            seen["jira"] += 1
        for path in root.rglob("notion.json"):
            payload = json.loads(path.read_text())
            for item in payload["pages"]:
                NotionPage(**item)
            seen["notion"] += 1
        for path in root.rglob("bitbucket.json"):
            payload = json.loads(path.read_text())
            BitbucketRepository(**payload["repository"])
            for item in payload["files"]:
                BitbucketFile(**item)
            for item in payload["commits"]:
                BitbucketCommit(**{**item, "files": ()})
            seen["bitbucket"] += 1

    assert seen == {"jira": 9, "notion": 6, "bitbucket": 6}


def test_migration_completion_claim_requires_completion_language():
    complete = {"metadata": {"content": "Migration to Auth API v2 is completed."}}
    planned = {"metadata": {"content": "Migration to Auth API v2 is planned."}}
    negated = {"metadata": {"content": "No migration to Auth API v2 has been completed."}}
    conditional = {"metadata": {"content": "Migration to Auth API v2 is complete only after code changes."}}
    assert StoryIngestor._claims_completed_v2(complete)
    assert not StoryIngestor._claims_completed_v2(planned)
    assert not StoryIngestor._claims_completed_v2(negated)
    assert not StoryIngestor._claims_completed_v2(conditional)
