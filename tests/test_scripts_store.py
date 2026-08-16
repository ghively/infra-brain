"""Tests for GeneratedScript model and scripts_store (Task F1)."""

import hashlib
import uuid
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from infra_brain.db.models import GeneratedScript

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


# ---------------------------------------------------------------------------
# Model / schema tests
# ---------------------------------------------------------------------------


def test_generated_script_table_exists(engine):
    inspector = sa_inspect(engine)
    assert "generated_scripts" in inspector.get_table_names()


def test_generated_script_indexes_exist(engine):
    inspector = sa_inspect(engine)
    index_names = {idx["name"] for idx in inspector.get_indexes("generated_scripts")}
    assert "ix_generated_scripts_content_sha256" in index_names
    assert "ix_generated_scripts_language" in index_names
    assert "ix_generated_scripts_created_by_agent" in index_names


def test_generated_script_create(db):
    content = "Get-Date"
    sha = hashlib.sha256(content.encode()).hexdigest()
    gs = GeneratedScript(
        name="test-script",
        language="powershell",
        purpose="check date",
        content=content,
        content_sha256=sha,
        created_by_agent="test_agent",
        domain="windows",
    )
    db.add(gs)
    db.flush()
    row = db.get(GeneratedScript, gs.id)
    assert row.name == "test-script"
    assert row.run_count == 0
    assert row.reusable is True
    assert row.last_run_at is None
    assert row.last_stdout == ""
    assert row.last_returncode is None
    assert row.git_repo == ""
    assert row.git_path == ""
    assert row.git_commit_sha == ""


# ---------------------------------------------------------------------------
# save_script — dedup by hash tests
# ---------------------------------------------------------------------------


def _make_session_factory(engine):
    """Return a session factory that uses the test engine."""
    from contextlib import contextmanager

    @contextmanager
    def get_session():
        with Session(engine) as session:
            yield session

    return get_session


def test_save_script_inserts_new_row(engine):
    """First save of a script inserts a new DB row."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
    ):
        gs.return_value.script_library_project_id = 0
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = ""
        gs.return_value.gitlab_token = ""
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30

        content = "Get-Process | Select-Object -First 10  # unique-" + str(uuid.uuid4())
        row = scripts_store.save_script(
            name="proc-list",
            language="powershell",
            purpose="list processes",
            content=content,
            created_by_agent="test_agent",
            domain="windows",
            stdout="proc output",
            returncode=0,
        )

    assert row.name == "proc-list"
    assert row.run_count == 1
    assert row.last_stdout == "proc output"
    assert row.last_returncode == 0


def test_save_script_dedup_bumps_run_count(engine):
    """Second save of same content (same hash) increments run_count."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "hostname  # dedup-test-" + str(uuid.uuid4())

    settings_mock = MagicMock()
    settings_mock.script_library_project_id = 0
    settings_mock.script_library_branch = "main"
    settings_mock.gitlab_url = ""
    settings_mock.gitlab_token = ""
    settings_mock.gitlab_ssl_verify = True
    settings_mock.api_timeout_seconds = 30

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings", return_value=settings_mock),
    ):
        row1 = scripts_store.save_script(
            name="hn",
            language="bash",
            purpose="get hostname",
            content=content,
            created_by_agent="agent1",
            domain="linux",
        )
        assert row1.run_count == 1

        row2 = scripts_store.save_script(
            name="hn",
            language="bash",
            purpose="get hostname",
            content=content,
            created_by_agent="agent1",
            domain="linux",
        )
        assert row2.run_count == 2
        # Same DB row (same id)
        assert row1.id == row2.id


def test_db_row_persists_when_git_unconfigured(engine):
    """DB row is saved even when script_library_project_id=0 (GitLab not configured)."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "ls -la  # no-git-" + str(uuid.uuid4())

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
    ):
        gs.return_value.script_library_project_id = 0
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = ""
        gs.return_value.gitlab_token = ""
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30

        row = scripts_store.save_script(
            name="dir-list",
            language="bash",
            purpose="list dir",
            content=content,
            created_by_agent="agent1",
            domain="linux",
        )

    # git fields must be empty strings (not None)
    assert row.git_repo == ""
    assert row.git_path == ""
    assert row.git_commit_sha == ""
    # But the row itself exists and has data
    assert row.id is not None
    assert row.content == content


def test_db_row_persists_when_git_call_fails(engine):
    """DB row is saved even when GitLab commit raises an exception."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "uptime  # git-fail-" + str(uuid.uuid4())

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
        patch("infra_brain.scripts_store.httpx") as mock_httpx,
    ):
        gs.return_value.script_library_project_id = 42
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = "https://gitlab.example.com"
        gs.return_value.gitlab_token = "token123"
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30
        # Make GitLab call fail
        mock_httpx.get.side_effect = Exception("network error")
        mock_httpx.post.side_effect = Exception("network error")
        mock_httpx.put.side_effect = Exception("network error")

        row = scripts_store.save_script(
            name="uptime-check",
            language="bash",
            purpose="check uptime",
            content=content,
            created_by_agent="agent1",
            domain="linux",
        )

    assert row.id is not None
    assert row.content == content


def test_find_reusable_returns_saved_rows(engine):
    """find_reusable returns previously saved scripts matching purpose+language."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "Get-Service  # reusable-" + str(uuid.uuid4())

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
    ):
        gs.return_value.script_library_project_id = 0
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = ""
        gs.return_value.gitlab_token = ""
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30

        scripts_store.save_script(
            name="svc-check",
            language="powershell",
            purpose="list services unique-purpose-xyz",
            content=content,
            created_by_agent="agent1",
            domain="windows",
        )

    with patch("infra_brain.scripts_store.get_session", get_session):
        results = scripts_store.find_reusable(
            purpose="list services unique-purpose-xyz",
            language="powershell",
        )

    assert len(results) >= 1
    assert any(r.purpose == "list services unique-purpose-xyz" for r in results)


def test_find_reusable_no_cross_language_match(engine):
    """find_reusable does NOT return rows from a different language."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "Get-NetAdapter  # cross-lang-" + str(uuid.uuid4())

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
    ):
        gs.return_value.script_library_project_id = 0
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = ""
        gs.return_value.gitlab_token = ""
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30

        scripts_store.save_script(
            name="net-adapter",
            language="powershell",
            purpose="check network unique-cross-lang",
            content=content,
            created_by_agent="agent1",
            domain="windows",
        )

    with patch("infra_brain.scripts_store.get_session", get_session):
        results = scripts_store.find_reusable(
            purpose="check network unique-cross-lang",
            language="bash",  # wrong language
        )

    assert len(results) == 0


def test_save_script_gitlab_commit_attempted_when_configured(engine):
    """When project_id > 0, httpx calls are attempted for GitLab commit, and
    git_commit_sha is populated from the branch's latest commit -- NOT from
    the file create/update response, which never carries a commit id on any
    real GitLab version (verified live against gitlab.example.internal)."""
    from infra_brain import scripts_store

    get_session = _make_session_factory(engine)
    content = "Get-ComputerInfo  # gitlab-configured-" + str(uuid.uuid4())

    # Real GitLab shape: file create/update returns only {file_path, branch}.
    create_response = MagicMock()
    create_response.status_code = 201
    create_response.json.return_value = {"file_path": "x", "branch": "main"}
    create_response.raise_for_status = MagicMock()

    # Real GitLab shape: GET .../repository/commits/{branch} returns the commit.
    commit_lookup_response = MagicMock()
    commit_lookup_response.status_code = 200
    commit_lookup_response.json.return_value = {"id": "realcommitsha123"}

    file_exists_check = MagicMock(status_code=404)

    with (
        patch("infra_brain.scripts_store.get_session", get_session),
        patch("infra_brain.scripts_store.get_settings") as gs,
        patch("infra_brain.scripts_store.httpx") as mock_httpx,
    ):
        gs.return_value.script_library_project_id = 99
        gs.return_value.script_library_branch = "main"
        gs.return_value.gitlab_url = "https://gitlab.example.com"
        gs.return_value.gitlab_token = "token123"
        gs.return_value.gitlab_ssl_verify = True
        gs.return_value.api_timeout_seconds = 30
        # First GET = file-exists check (404 -> create via POST), second GET
        # = the post-commit sha lookup this test exists to verify.
        mock_httpx.get.side_effect = [file_exists_check, commit_lookup_response]
        mock_httpx.post.return_value = create_response

        row = scripts_store.save_script(
            name="computer-info",
            language="powershell",
            purpose="get computer info",
            content=content,
            created_by_agent="agent1",
            domain="windows",
        )

    assert mock_httpx.post.called
    assert mock_httpx.get.call_count == 2
    assert row.git_commit_sha == "realcommitsha123"
