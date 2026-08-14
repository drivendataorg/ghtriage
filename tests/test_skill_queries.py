"""Every ```sql block the skill ships must execute against a real database.

The cookbook and SKILL.md hand agents complete queries built on derived-table and index
names; a rename in derived.py or full_text_search.py would silently invalidate all of
them with no other test failing. Same idea as `test_schema_index_example_executes_as_printed`.
"""

from pathlib import Path
import re

import duckdb
import pytest
from test_derived import _create_schema, _populate

from ghtriage.derived import create_derived
from ghtriage.full_text_search import create_search_indexes
from ghtriage.query import execute_query

SKILL_DIR = Path(__file__).resolve().parents[1] / "src" / "ghtriage" / "skills" / "ghtriage"
SKILL_FILES = (SKILL_DIR / "SKILL.md", SKILL_DIR / "references" / "query-cookbook.md")


def _sql_blocks() -> list:
    params = []
    for file in SKILL_FILES:
        blocks = re.findall(r"```sql\n(.*?)```", file.read_text(encoding="utf-8"), flags=re.DOTALL)
        assert blocks, f"no ```sql blocks found in {file.name}"
        for index, sql in enumerate(blocks, start=1):
            params.append(pytest.param(sql, id=f"{file.name}-{index}"))
    return params


@pytest.fixture(scope="module")
def skill_db_cwd(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cwd = tmp_path_factory.mktemp("skill-queries")
    db_path = cwd / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir()
    con = duckdb.connect(str(db_path))
    _create_schema(con)
    _populate(con)
    con.close()
    create_derived(db_path)
    create_search_indexes(db_path)
    return cwd


@pytest.mark.parametrize("sql", _sql_blocks())
def test_skill_sql_executes(sql: str, skill_db_cwd: Path) -> None:
    columns, _rows = execute_query(sql, cwd=skill_db_cwd)

    assert columns
