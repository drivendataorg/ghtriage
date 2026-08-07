from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

import ghtriage.views as views_module
from ghtriage.views import EMPTY, VIEW_COLUMN_DOCS, VIEW_DOCS, VIEWS, create_views

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# One shared dataset, built to exercise every case the suite asserts on:
#
#   issues
#     1  alice (User)   open    replies from bob, alice (author), codecov[bot]
#     2  alice (User)   open    no comments at all
#     3  ci[bot] (Bot)  closed  replies from ci[bot] and carol
#     4  dave (User)    open    author-only thread
#     5  ci[bot] (Bot)  open    only a bot commenter -> no non-bot participants
#     6  NULL author    open    deleted-account login
#   128  alice (User)   open    multi-digit number, guards the regexp join key
#   pull_requests
#     10 erin (User)    open    conversation comments only, no review comments
#     11 erin (User)    open    review comments from a human and from a bot
#     12 frank (User)   open    labels + assignees + pending reviewers all populated
#     13 frank (User)   open    nothing at all
#
# `issues` deliberately omits `state_reason`/`closed_at` in the degraded fixture
# only; the full fixture has them so padding can be shown not to blank real data.


def _api(kind: str, number: int) -> str:
    return f"https://api.github.com/repos/o/r/{kind}/{number}"


def _create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA github")
    con.execute("""
        CREATE TABLE github.issues (
            number BIGINT, title VARCHAR, state VARCHAR, state_reason VARCHAR,
            user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE,
            closed_at TIMESTAMP WITH TIME ZONE,
            comments BIGINT, assignee__login VARCHAR, _dlt_id VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE github.pull_requests (
            number BIGINT, title VARCHAR, state VARCHAR, draft BOOLEAN,
            user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE,
            closed_at TIMESTAMP WITH TIME ZONE, merged_at TIMESTAMP WITH TIME ZONE,
            assignee__login VARCHAR, _dlt_id VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE github.conversation_comments (
            id BIGINT, issue_url VARCHAR, user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE
        )
    """)
    con.execute("""
        CREATE TABLE github.review_comments (
            id BIGINT, pull_request_url VARCHAR, user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE
        )
    """)
    for child in ("issues__labels", "pull_requests__labels"):
        con.execute(f"CREATE TABLE github.{child} (name VARCHAR, _dlt_parent_id VARCHAR)")
    for child in (
        "issues__assignees",
        "pull_requests__assignees",
        "pull_requests__requested_reviewers",
    ):
        con.execute(f"CREATE TABLE github.{child} (login VARCHAR, _dlt_parent_id VARCHAR)")


def _populate(con: duckdb.DuckDBPyConnection) -> None:
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "has replies", "open", None, "alice", "User", _d(1), _d(9), None, 3, None, "i1"),
            (2, "silent", "open", None, "alice", "User", _d(2), _d(2), None, 0, None, "i2"),
            (
                3,
                "bot opened",
                "closed",
                "completed",
                "ci[bot]",
                "Bot",
                _d(3),
                _d(7),
                _d(7),
                2,
                None,
                "i3",
            ),
            (4, "author only", "open", None, "dave", "User", _d(4), _d(5), None, 1, None, "i4"),
            (5, "bots only", "open", None, "ci[bot]", "Bot", _d(5), _d(6), None, 1, None, "i5"),
            (6, "ghost", "open", None, None, None, _d(6), _d(6), None, 0, None, "i6"),
            (
                128,
                "multi digit",
                "open",
                None,
                "alice",
                "User",
                _d(8),
                _d(10),
                None,
                1,
                None,
                "i128",
            ),
        ],
    )
    con.executemany(
        "INSERT INTO github.pull_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                10,
                "conversation only",
                "open",
                False,
                "erin",
                "User",
                _d(1, 2),
                _d(3, 2),
                None,
                None,
                None,
                "p10",
            ),
            (
                11,
                "reviewed",
                "open",
                False,
                "erin",
                "User",
                _d(2, 2),
                _d(6, 2),
                None,
                None,
                None,
                "p11",
            ),
            (
                12,
                "fully tagged",
                "open",
                True,
                "frank",
                "User",
                _d(3, 2),
                _d(4, 2),
                None,
                None,
                None,
                "p12",
            ),
            (
                13,
                "bare",
                "open",
                False,
                "frank",
                "User",
                _d(4, 2),
                _d(4, 2),
                None,
                None,
                None,
                "p13",
            ),
        ],
    )
    con.executemany(
        "INSERT INTO github.conversation_comments VALUES (?,?,?,?,?,?)",
        [
            (101, _api("issues", 1), "bob", "User", _d(5), _d(5)),
            (102, _api("issues", 1), "alice", "User", _d(7), _d(7)),
            (103, _api("issues", 1), "codecov[bot]", "Bot", _d(9), _d(9)),
            (104, _api("issues", 3), "ci[bot]", "Bot", _d(6), _d(6)),
            (105, _api("issues", 3), "carol", "User", _d(7), _d(7)),
            (106, _api("issues", 4), "dave", "User", _d(5), _d(5)),
            (107, _api("issues", 5), "codecov[bot]", "Bot", _d(6), _d(6)),
            (110, _api("issues", 10), "gil", "User", _d(3, 2), _d(3, 2)),
            (111, _api("issues", 11), "codecov[bot]", "Bot", _d(5, 2), _d(5, 2)),
            (128, _api("issues", 128), "bob", "User", _d(10), _d(10)),
        ],
    )
    con.executemany(
        "INSERT INTO github.review_comments VALUES (?,?,?,?,?,?)",
        [
            (201, _api("pulls", 11), "hank", "User", _d(5, 2), _d(5, 2)),
            (202, _api("pulls", 11), "Copilot", "Bot", _d(6, 2), _d(6, 2)),
        ],
    )
    # Inserted unsorted on purpose: the views must sort these.
    con.execute("INSERT INTO github.issues__labels VALUES ('ui','i1'), ('bug','i1'), ('bug','i3')")
    con.execute("INSERT INTO github.issues__assignees VALUES ('zoe','i1'), ('adam','i1')")
    con.execute("INSERT INTO github.pull_requests__labels VALUES ('ci','p12'), ('area: db','p12')")
    con.execute("INSERT INTO github.pull_requests__assignees VALUES ('yara','p12'), ('bob','p12')")
    con.execute(
        "INSERT INTO github.pull_requests__requested_reviewers VALUES ('wes','p12'), ('ann','p12')"
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A fully populated database, as a repo with rich history would look."""
    path = tmp_path / "full.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    _populate(con)
    con.close()
    return path


def _d(day: int, month: int = 1) -> datetime:
    """A UTC-pinned fixture timestamp. Naive literals would inherit the local zone."""
    return datetime(2026, month, day, tzinfo=timezone.utc)


def _ts(date: str) -> datetime:
    return datetime.fromisoformat(f"{date}T00:00:00+00:00")


def rows(db_path: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute(sql).fetchall()


def typed_columns(db_path: Path, view: str) -> list[tuple[str, str]]:
    return rows(
        db_path,
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='github' AND table_name='{view}' ORDER BY ordinal_position",
    )


def columns(db_path: Path, view: str) -> list[str]:
    return [
        r[0]
        for r in rows(
            db_path,
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='github' AND table_name='{view}' ORDER BY ordinal_position",
        )
    ]


# ---------------------------------------------------------------------------
# Step 1 — the view exists with its pass-through columns
# ---------------------------------------------------------------------------


def test_create_views_creates_issue_activity(db: Path) -> None:
    create_views(db)

    # Identity columns lead the projection; full column order is pinned by
    # test_view_columns_match_spec.
    assert columns(db, "issue_activity")[:6] == [
        "number",
        "title",
        "state",
        "state_reason",
        "author",
        "author_type",
    ]
    assert {"created_at", "updated_at", "closed_at"} <= set(columns(db, "issue_activity"))
    assert rows(db, "SELECT number, title, state FROM github.issue_activity ORDER BY number") == [
        (1, "has replies", "open"),
        (2, "silent", "open"),
        (3, "bot opened", "closed"),
        (4, "author only", "open"),
        (5, "bots only", "open"),
        (6, "ghost", "open"),
        (128, "multi digit", "open"),
    ]


def test_create_views_author_type_passes_through(db: Path) -> None:
    create_views(db)

    assert rows(
        db, "SELECT number, author, author_type FROM github.issue_activity ORDER BY number"
    ) == [
        (1, "alice", "User"),
        (2, "alice", "User"),
        (3, "ci[bot]", "Bot"),
        (4, "dave", "User"),
        (5, "ci[bot]", "Bot"),
        (6, None, None),
        (128, "alice", "User"),
    ]


# ---------------------------------------------------------------------------
# Step 2 — documentation machinery and the drift guard
# ---------------------------------------------------------------------------


def test_create_views_applies_view_and_column_comments(db: Path) -> None:
    create_views(db)

    view_comments = dict(
        rows(db, "SELECT view_name, comment FROM duckdb_views() WHERE schema_name='github'")
    )
    assert view_comments["issue_activity"] == VIEW_DOCS["issue_activity"]
    assert view_comments["issue_activity"]

    column_comments = dict(
        rows(
            db,
            "SELECT column_name, comment FROM duckdb_columns() "
            "WHERE schema_name='github' AND table_name='issue_activity'",
        )
    )
    assert column_comments == VIEW_COLUMN_DOCS["issue_activity"]
    assert all(c for c in column_comments.values())


@pytest.mark.parametrize("view", sorted(VIEWS))
def test_view_docs_match_view_columns(db: Path, view: str) -> None:
    """Every view column has exactly one doc, and no doc outlives its column.

    This is the drift guard that lets the SQL and the docs live apart.
    """
    create_views(db)

    assert set(VIEW_COLUMN_DOCS[view]) == set(columns(db, view))
    assert view in VIEW_DOCS


# ---------------------------------------------------------------------------
# Step 3 — comment aggregates
# ---------------------------------------------------------------------------


def test_create_views_issue_activity_row_count_matches_issues(db: Path) -> None:
    create_views(db)

    assert rows(db, "SELECT count(*) FROM github.issue_activity") == rows(
        db, "SELECT count(*) FROM github.issues"
    )


def test_create_views_issue_comment_aggregates(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, comment_count, first_comment_at, last_comment_at "
        "FROM github.issue_activity ORDER BY number",
    ) == [
        (1, 3, _ts("2026-01-05"), _ts("2026-01-09")),
        (2, 0, None, None),
        (3, 2, _ts("2026-01-06"), _ts("2026-01-07")),
        (4, 1, _ts("2026-01-05"), _ts("2026-01-05")),
        (5, 1, _ts("2026-01-06"), _ts("2026-01-06")),
        (6, 0, None, None),
        (128, 1, _ts("2026-01-10"), _ts("2026-01-10")),
    ]


def test_create_views_join_key_handles_multi_digit_numbers(db: Path) -> None:
    """A regex capturing a single digit would silently mis-key every number over 9."""
    create_views(db)

    assert rows(db, "SELECT comment_count FROM github.issue_activity WHERE number = 128") == [(1,)]


# ---------------------------------------------------------------------------
# Step 4 — non-author timestamps
# ---------------------------------------------------------------------------


def test_create_views_non_author_columns_exclude_author_comments(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, first_non_author_comment_at, last_non_author_comment_at "
        "FROM github.issue_activity ORDER BY number",
    ) == [
        # 1: bob and codecov[bot] are non-authors; alice's own reply is excluded
        (1, _ts("2026-01-05"), _ts("2026-01-09")),
        (2, None, None),
        (3, _ts("2026-01-07"), _ts("2026-01-07")),
        # 4: dave is the only commenter and is the author -> nobody replied
        (4, None, None),
        (5, _ts("2026-01-06"), _ts("2026-01-06")),
        (6, None, None),
        (128, _ts("2026-01-10"), _ts("2026-01-10")),
    ]


# ---------------------------------------------------------------------------
# Step 5 — participants and the non-bot splits
# ---------------------------------------------------------------------------


def test_create_views_participant_count_counts_author_once(db: Path) -> None:
    create_views(db)

    # 1: alice (author, who also commented), bob, codecov[bot] -> 3, not 4
    # 4: dave is author and sole commenter -> 1
    assert rows(
        db, "SELECT number, participant_count FROM github.issue_activity ORDER BY number"
    ) == [(1, 3), (2, 1), (3, 2), (4, 1), (5, 2), (6, 0), (128, 2)]


def test_create_views_participant_count_ignores_null_author(db: Path) -> None:
    """Issue 6 has a deleted-account author and no comments: nobody to count."""
    create_views(db)

    assert rows(db, "SELECT participant_count FROM github.issue_activity WHERE number = 6") == [
        (0,)
    ]


def test_create_views_non_bot_comment_count_splits_by_user_type(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, comment_count, non_bot_comment_count "
        "FROM github.issue_activity ORDER BY number",
    ) == [
        (1, 3, 2),  # bob, alice human; codecov[bot] not
        (2, 0, 0),
        (3, 2, 1),  # ci[bot] excluded, carol counted
        (4, 1, 1),
        (5, 1, 0),  # only a bot commented
        (6, 0, 0),
        (128, 1, 1),
    ]


def test_create_views_non_bot_participant_count_subtracts_exactly(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, participant_count, non_bot_participant_count "
        "FROM github.issue_activity ORDER BY number",
    ) == [
        (1, 3, 2),
        (2, 1, 1),
        (3, 2, 1),  # bot author not counted as non-bot
        (4, 1, 1),
        (5, 2, 0),  # bot author + bot commenter
        (6, 0, 0),
        (128, 2, 2),
    ]
    assert rows(
        db,
        "SELECT count(*) FROM github.issue_activity "
        "WHERE non_bot_participant_count > participant_count "
        "   OR non_bot_comment_count > comment_count",
    ) == [(0,)]


def test_create_views_non_bot_counts_treat_null_user_type_as_non_bot(
    tmp_path: Path,
) -> None:
    """A NULL user__type must land in the non-bot bucket, not in neither.

    Under `<> 'Bot'` the comparison yields NULL and the row is silently dropped
    from both sides, breaking `bot = total - non_bot`.
    """
    path = tmp_path / "nulltype.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.execute(
        "INSERT INTO github.issues VALUES "
        "(1,'t','open',NULL,'alice','User','2026-01-01 00:00:00+00','2026-01-01 00:00:00+00',"
        "NULL,1,NULL,'i1')"
    )
    con.execute(
        f"INSERT INTO github.conversation_comments VALUES "
        f"(1,'{_api('issues', 1)}','ghost',NULL,"
        f"'2026-01-02 00:00:00+00','2026-01-02 00:00:00+00')"
    )
    con.close()

    create_views(path)

    assert rows(
        path,
        "SELECT comment_count, non_bot_comment_count, participant_count, "
        "non_bot_participant_count FROM github.issue_activity",
    ) == [(1, 1, 2, 2)]


def test_create_views_non_bot_comment_count_counts_non_app_machine_account(
    tmp_path: Path,
) -> None:
    """Accepted limitation: a User-typed machine account counts as non-bot."""
    path = tmp_path / "machine.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.execute(
        "INSERT INTO github.issues VALUES "
        "(1,'t','open',NULL,'alice','User','2026-01-01 00:00:00+00','2026-01-01 00:00:00+00',"
        "NULL,1,NULL,'i1')"
    )
    con.execute(
        f"INSERT INTO github.conversation_comments VALUES "
        f"(1,'{_api('issues', 1)}','codecov-commenter','User',"
        f"'2026-01-02 00:00:00+00','2026-01-02 00:00:00+00')"
    )
    con.close()

    create_views(path)

    assert rows(path, "SELECT non_bot_comment_count FROM github.issue_activity") == [(1,)]


def test_create_views_non_bot_counts_equal_totals_when_no_bots_present(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nobots.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.execute(
        "INSERT INTO github.issues VALUES "
        "(1,'t','open',NULL,'alice','User','2026-01-01 00:00:00+00','2026-01-01 00:00:00+00',"
        "NULL,1,NULL,'i1')"
    )
    con.execute(
        f"INSERT INTO github.conversation_comments VALUES "
        f"(1,'{_api('issues', 1)}','bob','User',"
        f"'2026-01-02 00:00:00+00','2026-01-02 00:00:00+00')"
    )
    con.close()

    create_views(path)

    assert rows(
        path,
        "SELECT comment_count = non_bot_comment_count, "
        "participant_count = non_bot_participant_count FROM github.issue_activity",
    ) == [(True, True)]


# ---------------------------------------------------------------------------
# Step 6 — list columns
# ---------------------------------------------------------------------------


def test_create_views_labels_sorted_and_empty_list_when_none(db: Path) -> None:
    create_views(db)

    assert rows(db, "SELECT number, labels FROM github.issue_activity ORDER BY number") == [
        (1, ["bug", "ui"]),  # inserted as ui, bug
        (2, []),
        (3, ["bug"]),
        (4, []),
        (5, []),
        (6, []),
        (128, []),
    ]


def test_create_views_assignees_sorted_and_empty_list_when_none(db: Path) -> None:
    create_views(db)

    assert rows(db, "SELECT number, assignees FROM github.issue_activity ORDER BY number") == [
        (1, ["adam", "zoe"]),  # inserted as zoe, adam
        (2, []),
        (3, []),
        (4, []),
        (5, []),
        (6, []),
        (128, []),
    ]


def test_create_views_assignees_includes_all_when_scalar_assignee_is_null(db: Path) -> None:
    """Issue 1 has two assignees in the child table and a NULL assignee__login.

    GitHub's deprecated scalar field does not populate reliably with more than
    one assignee, so the view must read the child table.
    """
    create_views(db)

    assert rows(db, "SELECT assignee__login FROM github.issues WHERE number = 1") == [(None,)]
    assert rows(db, "SELECT assignees FROM github.issue_activity WHERE number = 1") == [
        (["adam", "zoe"],)
    ]


def test_create_views_list_columns_do_not_fan_out(db: Path) -> None:
    """Two populated list columns on one row must not multiply it."""
    create_views(db)

    assert rows(db, "SELECT count(*) FROM github.issue_activity WHERE number = 1") == [(1,)]
    assert rows(db, "SELECT comment_count FROM github.issue_activity WHERE number = 1") == [(3,)]


# ---------------------------------------------------------------------------
# Step 7 — degradation: missing source tables
# ---------------------------------------------------------------------------


@pytest.fixture
def sparse_db(tmp_path: Path) -> Path:
    """A repo that has never produced comments, labels, assignees or reviewers.

    dlt only creates a table once a record carrying that field arrives, so these
    tables are genuinely absent, not merely empty.
    """
    path = tmp_path / "sparse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA github")
    con.execute("""
        CREATE TABLE github.issues (
            number BIGINT, title VARCHAR, state VARCHAR, state_reason VARCHAR,
            user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE,
            closed_at TIMESTAMP WITH TIME ZONE, _dlt_id VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE github.pull_requests (
            number BIGINT, title VARCHAR, state VARCHAR, draft BOOLEAN,
            user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE,
            closed_at TIMESTAMP WITH TIME ZONE, merged_at TIMESTAMP WITH TIME ZONE,
            _dlt_id VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(1, "only issue", "open", None, "alice", "User", _d(1), _d(1), None, "i1")],
    )
    con.executemany(
        "INSERT INTO github.pull_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(2, "only pull", "open", False, "bob", "User", _d(1), _d(1), None, None, "p2")],
    )
    con.close()
    return path


def test_create_views_skips_view_when_base_table_missing(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA github")
    con.close()

    create_views(path)  # must not raise

    assert rows(path, "SELECT count(*) FROM duckdb_views() WHERE schema_name='github'") == [(0,)]


def test_create_views_substitutes_empty_relation_for_missing_source_table(
    sparse_db: Path,
) -> None:
    create_views(sparse_db)

    assert rows(
        sparse_db,
        "SELECT comment_count, non_bot_comment_count, first_comment_at, last_comment_at, "
        "labels, assignees, participant_count FROM github.issue_activity",
    ) == [(0, 0, None, None, [], [], 1)]


def test_every_format_slot_has_an_empty_relation() -> None:
    """Rendering must never raise KeyError on a repo missing an optional table."""
    import re

    for sql in VIEWS.values():
        for slot in re.findall(r"\{(\w+)\}", sql):
            assert slot in EMPTY, slot


def test_create_views_swallows_errors(db: Path, capsys: pytest.CaptureFixture) -> None:
    """A broken view definition warns and does not stop the others."""
    broken = dict(VIEWS)
    broken["broken_view"] = "SELECT * FROM github.does_not_exist"

    with (
        patch.object(views_module, "VIEWS", broken),
        patch.dict(views_module.BASE_TABLES, {"broken_view": "issues"}),
        patch.dict(views_module.VIEW_DOCS, {"broken_view": "x"}),
        patch.dict(views_module.VIEW_COLUMN_DOCS, {"broken_view": {}}),
    ):
        create_views(db)

    assert "broken_view" in capsys.readouterr().err
    assert rows(db, "SELECT count(*) FROM github.issue_activity") == [(7,)]


# ---------------------------------------------------------------------------
# Step 8 — degradation: missing columns
# ---------------------------------------------------------------------------


@pytest.fixture
def unpadded_db(tmp_path: Path) -> Path:
    """A repo whose issues have never been closed and never had a state reason.

    dlt creates columns as data arrives, so `state_reason` and `closed_at` do
    not exist at all yet.
    """
    path = tmp_path / "unpadded.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA github")
    con.execute("""
        CREATE TABLE github.issues (
            number BIGINT, title VARCHAR, state VARCHAR,
            user__login VARCHAR, user__type VARCHAR,
            created_at TIMESTAMP WITH TIME ZONE, updated_at TIMESTAMP WITH TIME ZONE,
            _dlt_id VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?)",
        [(1, "never closed", "open", "alice", "User", _d(1), _d(1), "i1")],
    )
    con.close()
    return path


def test_create_views_pads_missing_column_with_typed_null(unpadded_db: Path) -> None:
    create_views(unpadded_db)

    assert rows(
        unpadded_db, "SELECT number, state_reason, closed_at FROM github.issue_activity"
    ) == [(1, None, None)]


def test_create_views_column_set_identical_with_and_without_optional_sources(
    db: Path, sparse_db: Path, unpadded_db: Path
) -> None:
    """A query written against a rich repo must work against a sparse one."""
    for path in (db, sparse_db, unpadded_db):
        create_views(path)

    # Types, not just names: a mistyped EMPTY stand-in changes a sparse repo's
    # column types while every name still matches.
    assert typed_columns(sparse_db, "issue_activity") == typed_columns(db, "issue_activity")
    assert typed_columns(unpadded_db, "issue_activity") == typed_columns(db, "issue_activity")


def test_create_views_padding_preserves_real_values(db: Path) -> None:
    """The padding union runs on every pull, not only when a column is absent."""
    create_views(db)

    assert rows(
        db, "SELECT state_reason, closed_at FROM github.issue_activity WHERE number = 3"
    ) == [("completed", _ts("2026-01-07"))]


def test_padding_does_not_coerce_existing_column_values(tmp_path: Path) -> None:
    """UNION ALL BY NAME coerces instead of erroring, and the padding runs on every
    pull, not only when a column is absent. If a padding type ever stops matching
    what dlt produces, real values get silently rewritten -- a naive TIMESTAMP
    padded as TIMESTAMP WITH TIME ZONE is reinterpreted in the machine's local zone.
    """
    path = tmp_path / "typed.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    closed = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "closed",
                "closed",
                "completed",
                "alice",
                "User",
                _d(1),
                _d(2),
                closed,
                0,
                None,
                "i1",
            )
        ],
    )
    con.close()

    create_views(path)

    # Value survives the padding union byte for byte, and keeps its declared type.
    assert rows(path, "SELECT closed_at, state_reason FROM github.issue_activity") == [
        (closed, "completed")
    ]
    assert dict(typed_columns(path, "issue_activity"))["closed_at"] == "TIMESTAMP WITH TIME ZONE"


@pytest.mark.parametrize("fixture", ["db", "sparse_db", "unpadded_db"])
def test_view_types_match_spec_on_sparse_databases(
    request: pytest.FixtureRequest, fixture: str
) -> None:
    """Type conformance must hold where padding and EMPTY stand-ins are load-bearing,
    not only on a fully populated database."""
    path = request.getfixturevalue(fixture)

    create_views(path)

    assert typed_columns(path, "issue_activity") == ISSUE_ACTIVITY_SPEC


# ---------------------------------------------------------------------------
# Step 9 — pull_request_activity
# ---------------------------------------------------------------------------


def test_create_views_pull_request_activity_separates_conversation_and_review_comments(
    db: Path,
) -> None:
    """GitHub's /issues/comments endpoint carries PR conversation comments.

    A view counting only /pulls/comments would report PR 10 as silent.
    """
    create_views(db)

    assert rows(
        db,
        "SELECT number, comment_count, review_comment_count "
        "FROM github.pull_request_activity ORDER BY number",
    ) == [(10, 1, 0), (11, 1, 2), (12, 0, 0), (13, 0, 0)]


def test_create_views_non_bot_review_comment_count_splits_by_user_type(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, review_comment_count, non_bot_review_comment_count, "
        "comment_count, non_bot_comment_count FROM github.pull_request_activity ORDER BY number",
    ) == [
        (10, 0, 0, 1, 1),
        (11, 2, 1, 1, 0),  # hank human, Copilot bot; conversation comment is codecov
        (12, 0, 0, 0, 0),
        (13, 0, 0, 0, 0),
    ]


def test_create_views_pull_participant_count_spans_both_comment_tables(db: Path) -> None:
    create_views(db)

    assert rows(
        db,
        "SELECT number, participant_count, non_bot_participant_count "
        "FROM github.pull_request_activity ORDER BY number",
    ) == [
        (10, 2, 2),  # erin, gil
        (11, 4, 2),  # erin, codecov[bot], hank, Copilot -> 2 non-bot
        (12, 1, 1),
        (13, 1, 1),
    ]


def test_create_views_pending_reviewers_sorted_and_empty_list_when_none(db: Path) -> None:
    create_views(db)

    assert rows(
        db, "SELECT number, pending_reviewers FROM github.pull_request_activity ORDER BY number"
    ) == [(10, []), (11, []), (12, ["ann", "wes"]), (13, [])]


def test_create_views_multiple_list_columns_do_not_fan_out(db: Path) -> None:
    """PR 12 has labels, assignees and pending reviewers all populated."""
    create_views(db)

    assert rows(
        db,
        "SELECT count(*), any_value(labels), any_value(assignees), any_value(pending_reviewers) "
        "FROM github.pull_request_activity WHERE number = 12",
    ) == [(1, ["area: db", "ci"], ["bob", "yara"], ["ann", "wes"])]


def test_create_views_pull_request_activity_passes_through_draft_and_merged_at(db: Path) -> None:
    create_views(db)

    assert rows(
        db, "SELECT number, draft, merged_at FROM github.pull_request_activity ORDER BY number"
    ) == [(10, False, None), (11, False, None), (12, True, None), (13, False, None)]


def test_create_views_pull_request_activity_degrades(sparse_db: Path, db: Path) -> None:
    """The degradation paths must be re-checked: pull_request_activity unions three
    participant sources and pads three columns, not two."""
    create_views(sparse_db)
    create_views(db)

    assert typed_columns(sparse_db, "pull_request_activity") == typed_columns(
        db, "pull_request_activity"
    )
    assert rows(
        sparse_db,
        "SELECT comment_count, non_bot_comment_count, review_comment_count, "
        "non_bot_review_comment_count, labels, assignees, pending_reviewers, "
        "participant_count FROM github.pull_request_activity",
    ) == [(0, 0, 0, 0, [], [], [], 1)]


# ---------------------------------------------------------------------------
# Specification conformance
# ---------------------------------------------------------------------------

TS = "TIMESTAMP WITH TIME ZONE"

ISSUE_ACTIVITY_SPEC = [
    ("number", "BIGINT"),
    ("title", "VARCHAR"),
    ("state", "VARCHAR"),
    ("state_reason", "VARCHAR"),
    ("author", "VARCHAR"),
    ("author_type", "VARCHAR"),
    ("labels", "VARCHAR[]"),
    ("assignees", "VARCHAR[]"),
    ("created_at", TS),
    ("updated_at", TS),
    ("closed_at", TS),
    ("comment_count", "BIGINT"),
    ("non_bot_comment_count", "BIGINT"),
    ("first_comment_at", TS),
    ("last_comment_at", TS),
    ("first_non_author_comment_at", TS),
    ("last_non_author_comment_at", TS),
    ("participant_count", "BIGINT"),
    ("non_bot_participant_count", "BIGINT"),
]

PULL_ACTIVITY_SPEC = [
    ("number", "BIGINT"),
    ("title", "VARCHAR"),
    ("state", "VARCHAR"),
    ("draft", "BOOLEAN"),
    ("author", "VARCHAR"),
    ("author_type", "VARCHAR"),
    ("labels", "VARCHAR[]"),
    ("assignees", "VARCHAR[]"),
    ("pending_reviewers", "VARCHAR[]"),
    ("created_at", TS),
    ("updated_at", TS),
    ("closed_at", TS),
    ("merged_at", TS),
    ("comment_count", "BIGINT"),
    ("non_bot_comment_count", "BIGINT"),
    ("first_comment_at", TS),
    ("last_comment_at", TS),
    ("review_comment_count", "BIGINT"),
    ("non_bot_review_comment_count", "BIGINT"),
    ("first_review_comment_at", TS),
    ("last_review_comment_at", TS),
    ("participant_count", "BIGINT"),
    ("non_bot_participant_count", "BIGINT"),
]

SPEC = {"issue_activity": ISSUE_ACTIVITY_SPEC, "pull_request_activity": PULL_ACTIVITY_SPEC}


@pytest.mark.parametrize("view", sorted(SPEC))
def test_view_column_types_match_spec(db: Path, view: str) -> None:
    """Column names, order and types, pinned against the plan's output spec.

    Types matter beyond documentation: a mistyped padding entry coerces real
    values instead of raising.
    """
    create_views(db)

    actual = rows(
        db,
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='github' AND table_name='{view}' ORDER BY ordinal_position",
    )
    assert actual == SPEC[view]


@pytest.mark.parametrize("view", sorted(SPEC))
def test_view_row_count_matches_base_table(db: Path, view: str) -> None:
    base = {"issue_activity": "issues", "pull_request_activity": "pull_requests"}[view]

    create_views(db)

    assert rows(db, f"SELECT count(*) FROM github.{view}") == rows(
        db, f"SELECT count(*) FROM github.{base}"
    )


@pytest.mark.parametrize("view", sorted(SPEC))
def test_non_bot_counts_never_exceed_totals(db: Path, view: str) -> None:
    create_views(db)

    pairs = [
        ("non_bot_comment_count", "comment_count"),
        ("non_bot_participant_count", "participant_count"),
    ]
    if view == "pull_request_activity":
        pairs.append(("non_bot_review_comment_count", "review_comment_count"))

    where = " OR ".join(f"{part} > {total}" for part, total in pairs)
    assert rows(db, f"SELECT count(*) FROM github.{view} WHERE {where}") == [(0,)]


# ---------------------------------------------------------------------------
# Step 10 — repeated creation, as every incremental pull does
# ---------------------------------------------------------------------------


def _snapshot(db_path: Path) -> dict:
    return {
        "columns": rows(
            db_path,
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='github' "
            "AND table_name IN ('issue_activity','pull_request_activity') "
            "ORDER BY table_name, ordinal_position",
        ),
        "view_comments": rows(
            db_path,
            "SELECT view_name, comment FROM duckdb_views() "
            "WHERE schema_name='github' ORDER BY view_name",
        ),
        "column_comments": rows(
            db_path,
            "SELECT table_name, column_name, comment FROM duckdb_columns() "
            "WHERE schema_name='github' "
            "AND table_name IN ('issue_activity','pull_request_activity') "
            "ORDER BY table_name, column_name",
        ),
        "counts": rows(
            db_path,
            "SELECT (SELECT count(*) FROM github.issue_activity), "
            "(SELECT count(*) FROM github.pull_request_activity)",
        ),
    }


def test_create_views_is_idempotent(db: Path) -> None:
    create_views(db)
    first = _snapshot(db)

    create_views(db)

    assert _snapshot(db) == first


def test_create_views_reapplies_comments_after_replace(db: Path) -> None:
    """CREATE OR REPLACE VIEW drops comments, so they must be reapplied each pull."""
    create_views(db)
    create_views(db)

    assert rows(
        db,
        "SELECT count(*) FROM duckdb_columns() WHERE schema_name='github' "
        "AND table_name IN ('issue_activity','pull_request_activity') AND comment IS NULL",
    ) == [(0,)]
    assert rows(
        db,
        "SELECT count(*) FROM duckdb_views() WHERE schema_name='github' AND comment IS NULL",
    ) == [(0,)]


def test_create_views_non_author_columns_handle_null_author(tmp_path: Path) -> None:
    """A deleted-account author must not swallow the non-author timestamps.

    Under `<>` the comparison against a NULL author yields NULL, so every comment
    is dropped from the filter and the columns read as "nobody replied" -- which
    contradicts what the column comment says they mean.
    """
    path = tmp_path / "ghostauthor.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "ghost author", "open", None, None, None, _d(1), _d(3), None, 1, None, "i1")],
    )
    con.executemany(
        "INSERT INTO github.conversation_comments VALUES (?,?,?,?,?,?)",
        [(1, _api("issues", 1), "bob", "User", _d(2), _d(2))],
    )
    con.close()

    create_views(path)

    assert rows(
        path,
        "SELECT first_non_author_comment_at, last_non_author_comment_at "
        "FROM github.issue_activity",
    ) == [(_d(2), _d(2))]


def test_create_views_tolerates_unparseable_comment_url(tmp_path: Path) -> None:
    """A URL without a trailing number must not break every query on the view.

    The cast happens at SELECT time, so a hard CAST would build the view fine and
    then fail on use, where the per-view try/except cannot help.
    """
    path = tmp_path / "badurl.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(1, "fine", "open", None, "alice", "User", _d(1), _d(1), None, 0, None, "i1")],
    )
    con.executemany(
        "INSERT INTO github.conversation_comments VALUES (?,?,?,?,?,?)",
        [(1, "https://api.github.com/repos/o/r/issues/not-a-number", "bob", "User", _d(2), _d(2))],
    )
    con.close()

    create_views(path)

    assert rows(path, "SELECT number, comment_count FROM github.issue_activity") == [(1, 0)]


def test_create_views_warns_and_survives_an_unusable_database(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A pull must not fail because the view step could not open the database.

    create_views runs before fetch_and_annotate, so raising here would also skip
    annotation -- a regression against the behaviour without views.
    """
    path = tmp_path / "corrupt.duckdb"
    path.write_bytes(b"this is not a duckdb file" * 100)

    create_views(path)  # must not raise

    assert "view creation failed" in capsys.readouterr().err


def test_create_views_warns_when_base_table_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Silently producing no pull_request_activity leaves a repo with no pull requests
    wondering where the view went."""
    path = tmp_path / "issuesonly.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.execute("DROP TABLE github.pull_requests")
    con.close()

    create_views(path)

    err = capsys.readouterr().err
    assert "pull_request_activity" in err and "pull_requests" in err
    assert "issue_activity" in columns_present(path)
    assert "pull_request_activity" not in columns_present(path)


def columns_present(db_path: Path) -> set[str]:
    return {r[0] for r in rows(db_path, "SELECT view_name FROM duckdb_views()")}
