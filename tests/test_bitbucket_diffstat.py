from connectors.bitbucket.api import (
    BitbucketCommit, BitbucketFileChange, BitbucketRepository,
    parse_diffstat, src_listing_url,
)
from graph.bitbucket_pipeline import commit_record, modifies_paths


def test_src_root_listing_keeps_trailing_slash():
    root = "https://api.bitbucket.org/2.0/repositories/rubik_/argus/src/abc"
    assert src_listing_url(root, "") == f"{root}/"
    assert src_listing_url(root, "src/argus") == f"{root}/src/argus"


def test_parse_diffstat_official_modified_shape():
    changes = parse_diffstat([{
        "type": "diffstat",
        "status": "modified",
        "lines_removed": 1,
        "lines_added": 2,
        "old": {"path": "setup.py", "type": "commit_file"},
        "new": {"path": "setup.py", "type": "commit_file"},
    }])
    assert changes == (
        BitbucketFileChange("setup.py", "setup.py", "modified", 2, 1),
    )


def test_parse_diffstat_removed_uses_old_path():
    changes = parse_diffstat([{
        "status": "removed",
        "lines_added": 0,
        "lines_removed": 5,
        "old": {"path": "gone.py"},
        "new": None,
    }])
    assert changes[0].path == "gone.py"
    assert changes[0].old_path == "gone.py"
    assert changes[0].status == "removed"


def test_parse_diffstat_rename_keeps_both_paths():
    changes = parse_diffstat([{
        "status": "renamed",
        "lines_added": 0,
        "lines_removed": 0,
        "old": {"path": "old/name.py"},
        "new": {"path": "new/name.py"},
    }])
    assert changes[0].path == "new/name.py"
    assert changes[0].old_path == "old/name.py"


def test_modifies_paths_only_links_ingested_head_files():
    commit = BitbucketCommit(
        "abc", "DATAOS-4346 Added fields", "Aashish", "a@tmdc.io",
        "2026-09-09T00:00:00+00:00", "https://example",
        files=(
            BitbucketFileChange("src/argus/search/query/plan.py", "src/argus/search/query/plan.py",
                                "modified", 12, 3),
            BitbucketFileChange("gone.py", "gone.py", "removed", 0, 5),
            BitbucketFileChange("README.go", "README.go", "modified", 1, 0),
        ),
    )
    linked = modifies_paths(commit, {"src/argus/search/query/plan.py", "other.py"})
    assert linked == [("src/argus/search/query/plan.py", "modified +12/-3")]


def test_modifies_paths_rename_falls_back_to_old_path_if_new_missing():
    commit = BitbucketCommit(
        "abc", "rename", "A", "", "2026-01-01T00:00:00+00:00", "",
        files=(BitbucketFileChange("new.py", "old.py", "renamed", 0, 0),),
    )
    assert modifies_paths(commit, {"old.py"}) == [("old.py", "renamed +0/-0")]


def test_commit_record_lists_all_changed_paths_for_search():
    commit = BitbucketCommit(
        "be023b9deadbeef",
        "DATAOS-4346 Added ai_instructions",
        "Aashish Verma", "aashish.verma@tmdc.io",
        "2026-09-09T00:00:00+00:00", "https://bitbucket.org/x",
        files=(
            BitbucketFileChange("src/argus/search/query/plan.py", "src/argus/search/query/plan.py",
                                "modified", 12, 3),
            BitbucketFileChange("gone.py", "gone.py", "removed", 0, 5),
        ),
    )
    record = commit_record(
        BitbucketRepository(
            "u", "rubik_", "argus", "argus", "rubik_/argus", "typesense",
            "", True, "",
        ),
        commit, "conn",
    )
    assert "src/argus/search/query/plan.py" in record.content
    assert "gone.py" in record.content
    assert "DATAOS-4346" in record.content
