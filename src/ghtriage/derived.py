"""Derived objects over the raw dlt tables, rebuilt on every pull.

Two views of pre-joined activity facts, and two tables holding one full-text
document per issue and per pull request. They are one kind of thing -- fully
recomputable from the raw tables, never a source of truth -- and differ only in
that DuckDB cannot index a view, so the search corpora have to be materialized.

Each is SQL, written as SQL: one template string that reads top-to-bottom as a
query, so it can be pasted straight into `ghtriage query` when debugging. Column
documentation lives in a separate dict, mirroring the shape `annotations.py`
uses for the OpenAPI descriptions.

Materialize before indexing: `full_text_search` indexes what this module builds.
"""

from dataclasses import dataclass, field
from pathlib import Path
import sys

import duckdb

ISSUE_ACTIVITY_SQL = r"""
WITH issues_padded AS (
    -- Supplies columns dlt has not created yet. UNION ALL BY NAME keeps a column
    -- the table already has and adds the rest as typed NULLs. The declared types
    -- must match what dlt produces: a mismatch coerces silently rather than
    -- erroring, and this union runs on every pull, not only when a column is absent.
    -- Pinned by test_padding_does_not_coerce_existing_column_values (values) and
    -- test_view_types_match_spec_on_sparse_databases (types).
    SELECT * FROM github.issues
    UNION ALL BY NAME
    SELECT
        NULL::BIGINT AS id,
        NULL::VARCHAR AS state_reason,
        NULL::TIMESTAMP WITH TIME ZONE AS closed_at
    WHERE false
),
comments_keyed AS (
    SELECT
        -- TRY_CAST, not CAST: a URL with no trailing number yields '', and a hard
        -- cast would raise at SELECT time -- after the view was created, where the
        -- creation guard cannot help.
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS issue_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {conversation_comments}
),
comment_agg AS (
    SELECT
        i.number AS issue_number,
        -- Counts joined rows: c.issue_number is NULL exactly when the LEFT JOIN
        -- missed, so COUNT(*) would report 1 for an issue with no comments.
        COUNT(c.issue_number) AS comment_count,
        MIN(c.created_at) AS first_comment_at,
        MAX(c.created_at) AS last_comment_at,
        MIN(c.created_at) FILTER (WHERE c.login IS DISTINCT FROM i.user__login)
            AS first_non_author_comment_at,
        MAX(c.created_at) FILTER (WHERE c.login IS DISTINCT FROM i.user__login)
            AS last_non_author_comment_at,
        -- IS DISTINCT FROM, not <>: under <> a NULL user__type falls into neither
        -- bucket, breaking the documented `bot count = total - non_bot`.
        COUNT(c.issue_number) FILTER (WHERE c.utype IS DISTINCT FROM 'Bot')
            AS non_bot_comment_count
    FROM issues_padded i
    LEFT JOIN comments_keyed c ON c.issue_number = i.number
    GROUP BY i.number
),
participants AS (
    -- A set, not "distinct non-author commenters + 1": the non-bot count has to be
    -- drawn from the same set as the total for the subtraction to hold, including
    -- when the item was opened by a bot.
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
    -- From the child table: the deprecated assignee__login does not populate
    -- reliably when there is more than one assignee.
    SELECT _dlt_parent_id, list(login ORDER BY login) AS assignees
    FROM {issues__assignees}
    GROUP BY _dlt_parent_id
)
SELECT
    i.id,
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

PULL_REQUEST_ACTIVITY_SQL = r"""
WITH pulls_padded AS (
    -- See the note on issues_padded: declared types must match what dlt produces.
    SELECT * FROM github.pull_requests
    UNION ALL BY NAME
    SELECT
        NULL::BIGINT AS id,
        NULL::BOOLEAN AS draft,
        NULL::TIMESTAMP WITH TIME ZONE AS closed_at,
        NULL::TIMESTAMP WITH TIME ZONE AS merged_at
    WHERE false
),
conversation_keyed AS (
    -- PR conversation comments live in conversation_comments, keyed by the PR number.
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS pull_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {conversation_comments}
),
review_keyed AS (
    SELECT
        TRY_CAST(regexp_extract(pull_request_url, '/(\d+)$', 1) AS BIGINT) AS pull_number,
        user__login AS login,
        user__type AS utype,
        created_at
    FROM {review_comments}
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
    -- A set, not "distinct non-author commenters + 1": the non-bot count has to be
    -- drawn from the same set as the total for the subtraction to hold, including
    -- when the item was opened by a bot.
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
    FROM {pull_requests__labels}
    GROUP BY _dlt_parent_id
),
assignee_agg AS (
    -- From the child table: the deprecated assignee__login does not populate
    -- reliably when there is more than one assignee.
    SELECT _dlt_parent_id, list(login ORDER BY login) AS assignees
    FROM {pull_requests__assignees}
    GROUP BY _dlt_parent_id
),
reviewer_agg AS (
    SELECT _dlt_parent_id, list(login ORDER BY login) AS pending_reviewers
    FROM {pull_requests__requested_reviewers}
    GROUP BY _dlt_parent_id
)
SELECT
    p.id,
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


ISSUE_THREADS_SQL = r"""
WITH issues_padded AS (
    -- See issues_padded above: the declared types must match what dlt produces.
    SELECT * FROM github.issues
    UNION ALL BY NAME
    SELECT NULL::VARCHAR AS title, NULL::VARCHAR AS body WHERE false
),
comments AS (
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS number,
        string_agg(body, E'\n' ORDER BY created_at) AS comment_text
    FROM {conversation_comments}
    GROUP BY 1
)
SELECT i.id, i.number, concat_ws(E'\n', i.title, i.body, c.comment_text) AS thread_text
FROM issues_padded i
LEFT JOIN comments c ON c.number = i.number
"""

PULL_REQUEST_THREADS_SQL = r"""
WITH pulls_padded AS (
    SELECT * FROM github.pull_requests
    UNION ALL BY NAME
    SELECT NULL::VARCHAR AS title, NULL::VARCHAR AS body WHERE false
),
comments AS (
    -- Both channels, interleaved by time. The split that pull_request_activity keeps for
    -- counts is a fact about engagement; a search corpus just wants all the words.
    SELECT number, string_agg(body, E'\n' ORDER BY created_at) AS comment_text
    FROM (
        SELECT
            TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS number,
            body,
            created_at
        FROM {conversation_comments}
        UNION ALL
        SELECT
            TRY_CAST(regexp_extract(pull_request_url, '/(\d+)$', 1) AS BIGINT) AS number,
            body,
            created_at
        FROM {review_comments}
    )
    GROUP BY number
)
SELECT p.id, p.number, concat_ws(E'\n', p.title, p.body, c.comment_text) AS thread_text
FROM pulls_padded p
LEFT JOIN comments c ON c.number = p.number
"""


# Stand-ins for source tables dlt has not created yet. Each must match the shape
# the CTE around it selects, so the substitution is invisible downstream.
EMPTY: dict[str, str] = {
    "conversation_comments": (
        "(SELECT NULL::VARCHAR AS issue_url, NULL::VARCHAR AS user__login, "
        "NULL::VARCHAR AS user__type, NULL::VARCHAR AS body, "
        "NULL::TIMESTAMP WITH TIME ZONE AS created_at WHERE false)"
    ),
    "issues__labels": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS name WHERE false)"
    ),
    "issues__assignees": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
    "review_comments": (
        "(SELECT NULL::VARCHAR AS pull_request_url, NULL::VARCHAR AS user__login, "
        "NULL::VARCHAR AS user__type, NULL::VARCHAR AS body, "
        "NULL::TIMESTAMP WITH TIME ZONE AS created_at WHERE false)"
    ),
    "pull_requests__labels": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS name WHERE false)"
    ),
    "pull_requests__assignees": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
    "pull_requests__requested_reviewers": (
        "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)"
    ),
}


@dataclass(frozen=True)
class Derived:
    """One derived object, and what has to exist before it can be built."""

    kind: str  # VIEW or TABLE -- a table only because DuckDB cannot index a view
    base: str  # base table that must exist for the object to mean anything
    sql: str
    requires: tuple[str, ...] = ()  # columns the base table must carry
    # slot -> columns the real table must carry to be used instead of the stand-in.
    # A comment table present but text-less is not the same as an absent one.
    sources: dict[str, tuple[str, ...]] = field(default_factory=dict)


# What each slot must carry for the real table to be used instead of the stand-in:
# exactly the columns the SQL reads from it.
_COMMENT_FACTS = ("user__login", "user__type", "created_at")
_CONVERSATION = ("issue_url", *_COMMENT_FACTS)
_REVIEW = ("pull_request_url", *_COMMENT_FACTS)
_LABELS = ("_dlt_parent_id", "name")
_LOGINS = ("_dlt_parent_id", "login")

DERIVED: dict[str, Derived] = {
    "issue_activity": Derived(
        "VIEW",
        "issues",
        ISSUE_ACTIVITY_SQL,
        sources={
            "conversation_comments": _CONVERSATION,
            "issues__labels": _LABELS,
            "issues__assignees": _LOGINS,
        },
    ),
    "pull_request_activity": Derived(
        "VIEW",
        "pull_requests",
        PULL_REQUEST_ACTIVITY_SQL,
        sources={
            "conversation_comments": _CONVERSATION,
            "review_comments": _REVIEW,
            "pull_requests__labels": _LABELS,
            "pull_requests__assignees": _LOGINS,
            "pull_requests__requested_reviewers": _LOGINS,
        },
    ),
    "issue_threads": Derived(
        "TABLE",
        "issues",
        ISSUE_THREADS_SQL,
        requires=("id",),
        sources={"conversation_comments": ("issue_url", "body", "created_at")},
    ),
    "pull_request_threads": Derived(
        "TABLE",
        "pull_requests",
        PULL_REQUEST_THREADS_SQL,
        requires=("id",),
        sources={
            "conversation_comments": ("issue_url", "body", "created_at"),
            "review_comments": ("pull_request_url", "body", "created_at"),
        },
    ),
}

DERIVED_DOCS: dict[str, str] = {
    "issue_threads": (
        "Derived table: one full-text document per issue, holding its title, body, and every "
        "conversation comment on it. Rebuilt and re-indexed on every pull. Search it to find "
        "whether a topic has been discussed before; join back on number for the facts."
    ),
    "pull_request_threads": (
        "Derived table: one full-text document per pull request, holding its title, body, and "
        "every conversation and review comment on it. Rebuilt and re-indexed on every pull."
    ),
    "issue_activity": (
        "Derived view: one row per issue with pre-joined comment activity, labels, and assignees."
    ),
    "pull_request_activity": (
        "Derived view: one row per pull request with pre-joined conversation-comment, "
        "review-comment, label, assignee, and review-request facts."
    ),
}

DERIVED_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "issue_threads": {
        "id": (
            "Pass-through of issues.id. The full-text document key: pass it to "
            "fts_github_issue_threads.match_bm25 to score this document."
        ),
        "number": "Pass-through of issues.number. Join key to issues and issue_activity.",
        "thread_text": (
            "The issue's title, body, and every conversation comment on it, oldest first, "
            "newline-joined. Comment authors and timestamps are not included; join the raw "
            "tables for those."
        ),
    },
    "pull_request_threads": {
        "id": (
            "Pass-through of pull_requests.id. The full-text document key: pass it to "
            "fts_github_pull_request_threads.match_bm25 to score this document."
        ),
        "number": (
            "Pass-through of pull_requests.number. Join key to pull_requests and "
            "pull_request_activity."
        ),
        "thread_text": (
            "The pull request's title, body, and every comment on it -- conversation and inline "
            "review alike, interleaved oldest first and newline-joined. The two channels are "
            "kept separate in pull_request_activity, where the distinction is a fact about "
            "engagement rather than an input to a tokenizer."
        ),
    },
    "issue_activity": {
        "id": (
            "Pass-through of issues.id. The full-text document key: pass it to "
            "fts_github_issues.match_bm25 to search titles and bodies without joining, or to "
            "fts_github_issue_threads.match_bm25 to search whole threads. Prefer number as the "
            "human-facing identifier."
        ),
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
            "Count of conversation_comments rows matching this issue number, including bot "
            "comments. May differ from issues.comments if comments were deleted on GitHub "
            "after being pulled."
        ),
        "non_bot_comment_count": (
            "Of comment_count, how many were posted by an account GitHub does not type as Bot. "
            "Subtract from comment_count for the bot count. Measures account type, not "
            "automation: machine accounts that are not GitHub Apps are typed User and counted "
            "here."
        ),
        "first_comment_at": (
            "Earliest created_at among matching conversation_comments rows, including bot "
            "comments. NULL when there are none."
        ),
        "last_comment_at": (
            "Latest created_at among matching conversation_comments rows, including bot "
            "comments. NULL when there are none."
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
    "pull_request_activity": {
        "id": (
            "Pass-through of pull_requests.id. The full-text document key: pass it to "
            "fts_github_pull_requests.match_bm25 to search titles and bodies without joining, or "
            "to fts_github_pull_request_threads.match_bm25 to search whole threads. Prefer number "
            "as the human-facing identifier."
        ),
        "number": "Pass-through of pull_requests.number.",
        "title": "Pass-through of pull_requests.title.",
        "state": (
            "Pass-through of pull_requests.state. Merged pull requests have state 'closed'; use "
            "merged_at to distinguish."
        ),
        "draft": "Pass-through of pull_requests.draft.",
        "author": "Login of the pull request opener. Pass-through of pull_requests.user__login.",
        "author_type": (
            "GitHub account type of the pull request opener: User, Bot, or Organization. "
            "Pass-through of pull_requests.user__type. Machine accounts that are not GitHub "
            "Apps are typed User."
        ),
        "labels": (
            "Sorted list of label names from pull_requests__labels. Empty list when the pull "
            "request has no labels."
        ),
        "assignees": (
            "Sorted list of assignee logins from pull_requests__assignees. Empty list when "
            "unassigned. Prefer this over the deprecated pull_requests.assignee__login, which "
            "is unreliable when there is more than one assignee."
        ),
        "pending_reviewers": (
            "Sorted list of logins with an outstanding review request, from "
            "pull_requests__requested_reviewers. GitHub drops a reviewer from this list once they "
            "submit a review, so it reflects unfulfilled requests only, including on closed "
            "pull requests. Empty list when there are none."
        ),
        "created_at": "Pass-through of pull_requests.created_at.",
        "updated_at": "Pass-through of pull_requests.updated_at.",
        "closed_at": "Pass-through of pull_requests.closed_at.",
        "merged_at": (
            "Pass-through of pull_requests.merged_at. NULL when the pull request was not merged."
        ),
        "comment_count": (
            "Count of conversation comments: conversation_comments rows whose issue_url number "
            "matches this pull request, including bot comments. Excludes inline review "
            "comments."
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
            "Count of inline review comments: review_comments rows matching this pull request, "
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


def quote_literal(text: str) -> str:
    """Escape a string for a DDL literal. COMMENT ON does not take bound parameters."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def present_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'github'"
        ).fetchall()
    }


def present_columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'github' AND table_name = ?",
            [table],
        ).fetchall()
    }


def render_slots(sql: str, usable: set[str]) -> str:
    """Fill each optional-source slot with the real table or an empty stand-in."""
    return sql.format(
        **{slot: (f"github.{slot}" if slot in usable else empty) for slot, empty in EMPTY.items()}
    )


def create_derived(db_path: Path) -> list[str]:
    """Create or replace every derived view and table in the `github` schema.

    Raises if the database cannot be opened or probed. An individual object that cannot
    be built is dropped and returned as a message, leaving the rest intact. An object
    with no source table yet is skipped quietly -- that is not a failure.
    """
    failures = []
    with duckdb.connect(str(db_path)) as con:
        present = present_tables(con)
        for name in DERIVED:
            if (failure := _create_one(con, name, present)) is not None:
                failures.append(failure)
    return failures


def drop_derived_objects(db_path: Path) -> None:
    """Drop every derived view and table."""
    with duckdb.connect(str(db_path)) as con:
        for name in DERIVED:
            _drop_one(con, name)


def _drop_one(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """Drop `name`, whichever kind it is."""
    con.execute(f"DROP {DERIVED[name].kind} IF EXISTS github.{name}")


def _create_one(con: duckdb.DuckDBPyConnection, name: str, present: set[str]) -> str | None:
    spec = DERIVED[name]
    kind = spec.kind.lower()
    # Everything is inside the guard, catalog probes included: an object that cannot be
    # built must cost only itself. What escapes here escapes `create_derived` too, and
    # the caller can then only assume every derived table is of unknown age.
    try:
        if spec.base not in present:
            print(
                f"Note: skipping {kind} {name}; source table {spec.base} is not present yet.",
                file=sys.stderr,
            )
            _drop_one(con, name)
            return None
        if missing := sorted(set(spec.requires) - present_columns(con, spec.base)):
            print(
                f"Note: skipping {kind} {name}; "
                f"{spec.base} has no {', '.join(missing)} column yet.",
                file=sys.stderr,
            )
            _drop_one(con, name)
            return None
        usable = {
            slot
            for slot in EMPTY
            if slot in present and set(spec.sources.get(slot, ())) <= present_columns(con, slot)
        }
        con.execute(
            f"CREATE OR REPLACE {spec.kind} github.{name} AS {render_slots(spec.sql, usable)}"
        )

        # CREATE OR REPLACE drops comments, so they are reapplied every time.
        con.execute(f"COMMENT ON {spec.kind} github.{name} IS {quote_literal(DERIVED_DOCS[name])}")
        for column, doc in DERIVED_COLUMN_DOCS[name].items():
            con.execute(f"COMMENT ON COLUMN github.{name}.{column} IS {quote_literal(doc)}")
    except Exception as exc:
        _drop_one(con, name)
        return f"could not create {kind} {name}: {exc}"
    return None
