from connectors.jira.api import JiraChange, field_intervals


def test_assignee_changelog_becomes_closed_intervals():
    changes = (
        JiraChange("2026-03-20T10:00:00.000+0000", "assignee", None, None, "acct-a", "Akshay", "Animesh"),
        JiraChange("2026-04-01T09:00:00.000+0000", "assignee", "acct-a", "Akshay", "acct-b", "Riya", "Animesh"),
    )
    intervals = field_intervals("2026-03-12T00:00:00.000+0000", "acct-b", "Riya", changes, "assignee")
    assert intervals[0][2] == "acct-a"
    assert intervals[0][1].startswith("2026-04-01")
    assert intervals[-1][2] == "acct-b"
    assert intervals[-1][1] is None


def test_no_changelog_is_one_open_interval():
    assert field_intervals("2026-01-01T00:00:00Z", "acct", "Akshay", (), "assignee") == [
        ("2026-01-01T00:00:00Z", None, "acct", "Akshay"),
    ]


def test_status_changes_are_independent_of_assignee():
    changes = (
        JiraChange("2026-03-15T00:00:00Z", "status", None, "To Do", None, "In Progress", "Animesh"),
        JiraChange("2026-03-20T00:00:00Z", "assignee", None, None, "acct-a", "Akshay", "Animesh"),
        JiraChange("2026-04-01T00:00:00Z", "status", None, "In Progress", None, "Done", "Akshay"),
    )
    statuses = field_intervals("2026-03-12T00:00:00Z", "Done", "Done", changes, "status")
    assert [item[3] for item in statuses] == ["To Do", "In Progress", "Done"]
    assert statuses[0][1] == "2026-03-15T00:00:00Z"
    assert statuses[1][1] == "2026-04-01T00:00:00Z"
    assert statuses[2][1] is None
