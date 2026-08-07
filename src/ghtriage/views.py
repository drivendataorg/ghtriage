"""Derived SQL views over the raw dlt tables, recreated on every pull.

The views are SQL, written as SQL: each is one template string that reads
top-to-bottom as a query, so it can be pasted straight into `ghtriage query`
when debugging. Column documentation lives in a separate dict, mirroring the
shape `annotations.py` uses for the OpenAPI descriptions.
"""

from pathlib import Path
import sys

import duckdb

ISSUE_ACTIVITY_SQL = r"""
WITH issues_padded AS (
    -- Supplies columns dlt has not created yet. UNION ALL BY NAME keeps a column
    -- the table already has and adds the rest as typed NULLs. The declared types
    -- must match what dlt produces: a mismatch coerces silently rather than
    -- erroring, and this union runs on every pull, not only when a column is absent.
    SELECT * FROM github.issues
    UNION ALL BY NAME
    SELECT
        NULL::VARCHAR AS state_reason,
        NULL::TIMESTAMP WITH TIME ZONE AS closed_at
    WHERE false
),
comments_keyed AS (
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS issue_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {issue_comments}
),
comment_agg AS (
    SELECT
        i.number AS issue_number,
        COUNT(c.issue_number) AS comment_count,
        MIN(c.created_at) AS first_comment_at,
        MAX(c.created_at) AS last_comment_at,
        MIN(c.created_at) FILTER (WHERE c.login IS DISTINCT FROM i.user__login)
            AS first_non_author_comment_at,
        MAX(c.created_at) FILTER (WHERE c.login IS DISTINCT FROM i.user__login)
            AS last_non_author_comment_at,
        COUNT(c.issue_number) FILTER (WHERE c.utype IS DISTINCT FROM 'Bot')
            AS non_bot_comment_count
    FROM issues_padded i
    LEFT JOIN comments_keyed c ON c.issue_number = i.number
    GROUP BY i.number
),
participants AS (
    SELECT i.number AS issue_number, i.user__login AS login, i.user__type AS utype
    FROM issues_padded i
    UNION
    SELECT c.issue_number, c.login, c.utype FROM comments_keyed c
),
participant_agg AS (
    SELECT
        issue_number,
        COUNT(DISTINCT login) AS participant_count,
        COUNT(DISTINCT login) FILTER (WHERE utype IS DISTINCT FROM 'Bot')
            AS non_bot_participant_count
    FROM participants
    GROUP BY issue_number
),
label_agg AS (
    SELECT _dlt_parent_id, list(name ORDER BY name) AS labels
    FROM {issues__labels}
    GROUP BY _dlt_parent_id
),
assignee_agg AS (
    SELECT _dlt_parent_id, list(login ORDER BY login) AS assignees
    FROM {issues__assignees}
    GROUP BY _dlt_parent_id
)
SELECT
    i.number,
    i.title,
    i.state,
    i.state_reason,
    i.user__login AS author,
    i.user__type AS author_type,
    COALESCE(lb.labels, CAST([] AS VARCHAR[])) AS labels,
    COALESCE(asg.assignees, CAST([] AS VARCHAR[])) AS assignees,
    i.created_at,
    i.updated_at,
    i.closed_at,
    ca.comment_count,
    ca.non_bot_comment_count,
    ca.first_comment_at,
    ca.last_comment_at,
    ca.first_non_author_comment_at,
    ca.last_non_author_comment_at,
    pa.participant_count,
    pa.non_bot_participant_count
FROM issues_padded i
LEFT JOIN comment_agg ca ON ca.issue_number = i.number
LEFT JOIN participant_agg pa ON pa.issue_number = i.number
LEFT JOIN label_agg lb ON lb._dlt_parent_id = i._dlt_id
LEFT JOIN assignee_agg asg ON asg._dlt_parent_id = i._dlt_id
"""

PULL_ACTIVITY_SQL = r"""
WITH pulls_padded AS (
    -- See the note on issues_padded: declared types must match what dlt produces.
    SELECT * FROM github.pulls
    UNION ALL BY NAME
    SELECT
        NULL::BOOLEAN AS draft,
        NULL::TIMESTAMP WITH TIME ZONE AS closed_at,
        NULL::TIMESTAMP WITH TIME ZONE AS merged_at
    WHERE false
),
conversation_keyed AS (
    -- PR conversation comments live in issue_comments, keyed by the PR number.
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS pull_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {issue_comments}
),
review_keyed AS (
    SELECT
        TRY_CAST(regexp_extract(pull_request_url, '/(\d+)$', 1) AS BIGINT) AS pull_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {pull_comments}
),
conversation_agg AS (
    SELECT
        pull_number,
        COUNT(*) AS comment_count,
        COUNT(*) FILTER (WHERE utype IS DISTINCT FROM 'Bot') AS non_bot_comment_count,
        MIN(created_at) AS first_comment_at,
        MAX(created_at) AS last_comment_at
    FROM conversation_keyed
    GROUP BY pull_number
),
review_agg AS (
    SELECT
        pull_number,
        COUNT(*) AS review_comment_count,
        COUNT(*) FILTER (WHERE utype IS DISTINCT FROM 'Bot') AS non_bot_review_comment_count,
        MIN(created_at) AS first_review_comment_at,
        MAX(created_at) AS last_review_comment_at
    FROM review_keyed
    GROUP BY pull_number
),
participants AS (
    SELECT p.number AS pull_number, p.user__login AS login, p.user__type AS utype
    FROM pulls_padded p
    UNION
    SELECT c.pull_number, c.login, c.utype FROM conversation_keyed c
    UNION
    SELECT r.pull_number, r.login, r.utype FROM review_keyed r
),
participant_agg AS (
    SELECT
        pull_number,
        COUNT(DISTINCT login) AS participant_count,
        COUNT(DISTINCT login) FILTER (WHERE utype IS DISTINCT FROM 'Bot')
            AS non_bot_participant_count
    FROM participants
    GROUP BY pull_number
),
label_agg AS (
    SELECT _dlt_parent_id, list(name ORDER BY name) AS labels
    FROM {pulls__labels}
    GROUP BY _dlt_parent_id
),
assignee_agg AS (
    SELECT _dlt_parent_id, list(login ORDER BY login) AS assignees
    FROM {pulls__assignees}
    GROUP BY _dlt_parent_id
),
reviewer_agg AS (
    SELECT _dlt_parent_id, list(login ORDER BY login) AS pending_reviewers
    FROM {pulls__requested_reviewers}
    GROUP BY _dlt_parent_id
)
SELECT
    p.number,
    p.title,
    p.state,
    p.draft,
    p.user__login AS author,
    p.user__type AS author_type,
    COALESCE(lb.labels, CAST([] AS VARCHAR[])) AS labels,
    COALESCE(asg.assignees, CAST([] AS VARCHAR[])) AS assignees,
    COALESCE(rv.pending_reviewers, CAST([] AS VARCHAR[])) AS pending_reviewers,
    p.created_at,
    p.updated_at,
    p.closed_at,
    p.merged_at,
    COALESCE(c.comment_count, 0) AS comment_count,
    COALESCE(c.non_bot_comment_count, 0) AS non_bot_comment_count,
    c.first_comment_at,
    c.last_comment_at,
    COALESCE(r.review_comment_count, 0) AS review_comment_count,
    COALESCE(r.non_bot_review_comment_count, 0) AS non_bot_review_comment_count,
    r.first_review_comment_at,
    r.last_review_comment_at,
    pa.participant_count,
    pa.non_bot_participant_count
FROM pulls_padded p
LEFT JOIN conversation_agg c ON c.pull_number = p.number
LEFT JOIN review_agg r ON r.pull_number = p.number
LEFT JOIN participant_agg pa ON pa.pull_number = p.number
LEFT JOIN label_agg lb ON lb._dlt_parent_id = p._dlt_id
LEFT JOIN assignee_agg asg ON asg._dlt_parent_id = p._dlt_id
LEFT JOIN reviewer_agg rv ON rv._dlt_parent_id = p._dlt_id
"""


# Stand-ins for source tables dlt has not created yet. Each must match the shape
# the CTE around it selects, so the substitution is invisible downstream.
EMPTY: dict[str, str] = {
    "issue_comments": (
        "(SELECT NULL::VARCHAR AS issue_url, NULL::VARCHAR AS user__login, "
        "NULL::VARCHAR AS user__type, NULL::TIMESTAMP WITH TIME ZONE AS created_at WHERE false)"
    ),
    "issues__labels": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS name WHERE false)"
    ),
    "issues__assignees": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
    "pull_comments": (
        "(SELECT NULL::VARCHAR AS pull_request_url, NULL::VARCHAR AS user__login, "
        "NULL::VARCHAR AS user__type, NULL::TIMESTAMP WITH TIME ZONE AS created_at WHERE false)"
    ),
    "pulls__labels": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS name WHERE false)"
    ),
    "pulls__assignees": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
    "pulls__requested_reviewers": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
}

VIEWS: dict[str, str] = {
    "issue_activity": ISSUE_ACTIVITY_SQL,
    "pull_activity": PULL_ACTIVITY_SQL,
}

BASE_TABLES: dict[str, str] = {
    "issue_activity": "issues",
    "pull_activity": "pulls",
}

VIEW_DOCS: dict[str, str] = {
    "issue_activity": (
        "Derived view: one row per issue with pre-joined comment activity, labels, and assignees."
    ),
    "pull_activity": (
        "Derived view: one row per pull request with pre-joined conversation-comment, "
        "review-comment, label, assignee, and review-request facts."
    ),
}

VIEW_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "issue_activity": {
        "number": "Pass-through of issues.number.",
        "title": "Pass-through of issues.title.",
        "state": "Pass-through of issues.state.",
        "state_reason": "Pass-through of issues.state_reason.",
        "author": "Login of the issue opener. Pass-through of issues.user__login.",
        "author_type": (
            "GitHub account type of the issue opener: User, Bot, or Organization. Pass-through "
            "of issues.user__type. Machine accounts that are not GitHub Apps are typed User."
        ),
        "labels": (
            "Sorted list of label names from issues__labels. Empty list when the issue has no "
            "labels."
        ),
        "assignees": (
            "Sorted list of assignee logins from issues__assignees. Empty list when unassigned. "
            "Prefer this over the deprecated issues.assignee__login, which is unreliable when "
            "there is more than one assignee."
        ),
        "created_at": "Pass-through of issues.created_at.",
        "updated_at": (
            "Pass-through of issues.updated_at. GitHub bumps this for many edit types, not only "
            "comments."
        ),
        "closed_at": "Pass-through of issues.closed_at.",
        "comment_count": (
            "Count of issue_comments rows matching this issue number, including bot comments. "
            "May differ from issues.comments if comments were deleted on GitHub after being "
            "pulled."
        ),
        "non_bot_comment_count": (
            "Of comment_count, how many were posted by an account GitHub does not type as Bot. "
            "Subtract from comment_count for the bot count. Measures account type, not "
            "automation: machine accounts that are not GitHub Apps are typed User and counted "
            "here."
        ),
        "first_comment_at": (
            "Earliest created_at among matching issue_comments rows, including bot comments. "
            "NULL when there are none."
        ),
        "last_comment_at": (
            "Latest created_at among matching issue_comments rows, including bot comments. "
            "NULL when there are none."
        ),
        "first_non_author_comment_at": (
            "Earliest comment created_at whose author differs from the issue author, including "
            "bot comments. NULL when there is none."
        ),
        "last_non_author_comment_at": (
            "Latest comment created_at whose author differs from the issue author, including "
            "bot comments. NULL when there is none."
        ),
        "participant_count": (
            "Number of distinct logins in the set formed by the issue author together with all "
            "comment authors, including bots. NULL logins are not counted."
        ),
        "non_bot_participant_count": (
            "Of participant_count, how many are accounts GitHub does not type as Bot. Subtract "
            "from participant_count for the bot count. Excludes the issue author when the issue "
            "was opened by a bot."
        ),
    },
    "pull_activity": {
        "number": "Pass-through of pulls.number.",
        "title": "Pass-through of pulls.title.",
        "state": (
            "Pass-through of pulls.state. Merged pull requests have state 'closed'; use "
            "merged_at to distinguish."
        ),
        "draft": "Pass-through of pulls.draft.",
        "author": "Login of the pull request opener. Pass-through of pulls.user__login.",
        "author_type": (
            "GitHub account type of the pull request opener: User, Bot, or Organization. "
            "Pass-through of pulls.user__type. Machine accounts that are not GitHub Apps are "
            "typed User."
        ),
        "labels": (
            "Sorted list of label names from pulls__labels. Empty list when the pull request "
            "has no labels."
        ),
        "assignees": (
            "Sorted list of assignee logins from pulls__assignees. Empty list when unassigned. "
            "Prefer this over the deprecated pulls.assignee__login, which is unreliable when "
            "there is more than one assignee."
        ),
        "pending_reviewers": (
            "Sorted list of logins with an outstanding review request, from "
            "pulls__requested_reviewers. GitHub drops a reviewer from this list once they "
            "submit a review, so it reflects unfulfilled requests only, including on closed "
            "pull requests. Empty list when there are none."
        ),
        "created_at": "Pass-through of pulls.created_at.",
        "updated_at": "Pass-through of pulls.updated_at.",
        "closed_at": "Pass-through of pulls.closed_at.",
        "merged_at": (
            "Pass-through of pulls.merged_at. NULL when the pull request was not merged."
        ),
        "comment_count": (
            "Count of conversation comments: issue_comments rows whose issue_url number matches "
            "this pull request, including bot comments. Excludes inline review comments."
        ),
        "non_bot_comment_count": (
            "Of comment_count, how many were posted by an account GitHub does not type as Bot. "
            "Subtract from comment_count for the bot count. Measures account type, not "
            "automation."
        ),
        "first_comment_at": (
            "Earliest created_at among matching conversation comments, including bot comments. "
            "NULL when there are none."
        ),
        "last_comment_at": (
            "Latest created_at among matching conversation comments, including bot comments. "
            "NULL when there are none."
        ),
        "review_comment_count": (
            "Count of inline review comments: pull_comments rows matching this pull request, "
            "including bot comments. Excludes conversation comments."
        ),
        "non_bot_review_comment_count": (
            "Of review_comment_count, how many were posted by an account GitHub does not type "
            "as Bot. Subtract from review_comment_count for the bot count. A bot review is "
            "meaningful activity but is not a human review; this column keeps the two "
            "distinguishable."
        ),
        "first_review_comment_at": (
            "Earliest created_at among matching review comments, including bot comments. "
            "NULL when there are none."
        ),
        "last_review_comment_at": (
            "Latest created_at among matching review comments, including bot comments. "
            "NULL when there are none."
        ),
        "participant_count": (
            "Number of distinct logins in the set formed by the pull request author together "
            "with all conversation- and review-comment authors, including bots. NULL logins are "
            "not counted."
        ),
        "non_bot_participant_count": (
            "Of participant_count, how many are accounts GitHub does not type as Bot. Subtract "
            "from participant_count for the bot count. Excludes the pull request author when it "
            "was opened by a bot."
        ),
    },
}


def _quote(text: str) -> str:
    """Escape a string for a DDL literal. COMMENT ON does not take bound parameters."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def _present_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'github'"
        ).fetchall()
    }


def _render(sql: str, present: set[str]) -> str:
    """Fill each optional-source slot with the real table or an empty stand-in."""
    return sql.format(
        **{slot: (f"github.{slot}" if slot in present else empty) for slot, empty in EMPTY.items()}
    )


def create_views(db_path: Path) -> None:
    """Create or replace every derived view in the `github` schema.

    Best-effort: a view that fails to build warns on stderr and does not stop the
    others, so a view problem never fails a pull.
    """
    try:
        with duckdb.connect(str(db_path)) as con:
            present = _present_tables(con)
            for name, sql in VIEWS.items():
                _create_one(con, name, sql, present)
    except Exception as exc:
        print(f"Warning: view creation failed: {exc}", file=sys.stderr)


def _create_one(con: duckdb.DuckDBPyConnection, name: str, sql: str, present: set[str]) -> None:
    base = BASE_TABLES[name]
    if base not in present:
        print(
            f"Note: skipping view {name}; source table {base} is not present yet.",
            file=sys.stderr,
        )
        return
    try:
        con.execute(f"CREATE OR REPLACE VIEW github.{name} AS {_render(sql, present)}")

        # CREATE OR REPLACE drops comments, so they are reapplied every time.
        con.execute(f"COMMENT ON VIEW github.{name} IS {_quote(VIEW_DOCS[name])}")
        for column, doc in VIEW_COLUMN_DOCS[name].items():
            con.execute(f"COMMENT ON COLUMN github.{name}.{column} IS {_quote(doc)}")
    except Exception as exc:
        print(f"Warning: could not create view {name}: {exc}", file=sys.stderr)
