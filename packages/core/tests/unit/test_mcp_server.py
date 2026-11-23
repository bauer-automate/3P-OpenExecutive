"""Unit tests for the Open Executive MCP server (openexecutive.mcp_server).

All LLM/DB-touching service functions are mocked — these tests exercise the
wiring (registration, handler serialization, the specialist-enum contract, the
mount, and the shared-secret gate), not the underlying subsystems.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import Caller
from openexecutive.mcp_server import server as mcp_server
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"


@pytest.fixture(autouse=True)
def roster_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every handler resolves its caller against the roster: keep that read
    on a tmp file, never ./episodic_memory.db."""
    path = tmp_path / "mcp_server.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    episodic.initialize_db(path)
    people_store.initialize_db()
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def roster() -> dict[str, int]:
    ids = {
        "principal": people_store.upsert_person(
            full_name="Olivia Owner", is_principal=True, email=OWNER
        ),
        "teammate": people_store.upsert_person(full_name="Ben Teammate", email=TEAMMATE),
    }
    people_registry.invalidate()
    return ids


def _as(who: Caller) -> Any:
    """Run a handler as if the verified request named ``who``."""
    return patch.object(mcp_server, "_who", return_value=who)


class _Today:
    """Stand-in for Home's snapshot: dumps, and has no cards."""

    proposals: list[Any] = []
    caller_person_id: int | None = None

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self._payload


class _Dumpable:
    """Stand-in for a Pydantic model: ``model_dump``, and the chat and
    department a memory row is read by (none: the team's)."""

    session_id = ""
    department = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self._payload


# ---------------------------------------------------------------------------
# Contracts: specialist enum + registration.
# ---------------------------------------------------------------------------
def test_specialist_enum_matches_registry() -> None:
    """The Literal advertised to clients must equal the live roster, or callers
    get an enum that drifts from what route_to_specialist accepts."""
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert set(mcp_server.specialist_keys()) == set(SPECIALIST_REGISTRY)


@pytest.mark.asyncio
async def test_registered_tools() -> None:
    tools = await mcp_server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "consult_specialist",
        "search_knowledge",
        "list_workflows",
        "ask_executive",
        "answer_card",
        "run_workflow",
    }


@pytest.mark.asyncio
async def test_registered_resources() -> None:
    resources = await mcp_server.mcp.list_resources()
    assert {str(r.uri) for r in resources} == {
        "oe://company/profile",
        "oe://today/briefing",
        "oe://today/done-for-you",
        "oe://cards/needs-you",
        "oe://tasks",
        "oe://watches",
        "oe://approvals",
        "oe://people/roster",
        "oe://departments/state",
        "oe://memory/decisions",
        "oe://memory/initiatives",
        "oe://memory/advice",
    }


# ---------------------------------------------------------------------------
# Resource handlers.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_company_profile_resource_empty() -> None:
    fake = _Dumpable({})
    fake.is_empty = lambda: True  # type: ignore[attr-defined]
    with patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=fake,
    ):
        out = await mcp_server.company_profile()
    assert "No company profile" in out


@pytest.mark.asyncio
async def test_company_profile_resource_populated() -> None:
    fake = _Dumpable({})
    fake.is_empty = lambda: False  # type: ignore[attr-defined]
    fake.to_prompt_block = lambda: "ACME Corp — SaaS"  # type: ignore[attr-defined]
    with patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=fake,
    ):
        out = await mcp_server.company_profile()
    assert out == "ACME Corp — SaaS"


@pytest.mark.asyncio
async def test_people_roster_resource_serializes_models() -> None:
    people = [_Dumpable({"id": 1, "full_name": "Alice"})]
    with patch("openexecutive.people.store.list_people", return_value=people):
        out = await mcp_server.people_roster()
    assert json.loads(out) == [{"id": 1, "full_name": "Alice"}]


@pytest.mark.asyncio
async def test_today_briefing_resource_serializes_model() -> None:
    with patch(
        "openexecutive.api.routes.today._build_today",
        return_value=_Dumpable({"departments": [], "people": []}),
    ):
        out = await mcp_server.today_briefing()
    assert json.loads(out) == {"departments": [], "people": []}


@pytest.mark.asyncio
async def test_done_for_you_reads_homes_own_route() -> None:
    request = object()
    route = AsyncMock(return_value=[_Dumpable({"title": "Sent the recap"})])
    with patch.object(mcp_server, "_http_request", return_value=request), patch(
        "openexecutive.api.routes.today.get_today_done", new=route
    ):
        out = await mcp_server.done_for_you()
    assert json.loads(out) == [{"title": "Sent the recap"}]
    route.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_tasks_and_approvals_read_the_web_apps_routes() -> None:
    from fastapi import HTTPException

    request = object()
    tasks = Mock(return_value=_Dumpable({"tasks": [], "total": 0}))
    with patch.object(mcp_server, "_http_request", return_value=request), patch(
        "openexecutive.api.routes.people.list_tasks_route", new=tasks
    ):
        assert json.loads(await mcp_server.tasks()) == {"tasks": [], "total": 0}
    tasks.assert_called_once_with(request, limit=200)
    refused = Mock(side_effect=HTTPException(status_code=403, detail="Sign in to see what it may do."))
    with patch.object(mcp_server, "_http_request", return_value=request), patch(
        "openexecutive.api.routes.may_do.get_what_it_may_do", new=refused
    ), pytest.raises(mcp_server.CallerRefused, match="Sign in"):
        await mcp_server.approvals_summary()


@pytest.mark.asyncio
async def test_a_route_read_needs_a_request() -> None:
    with pytest.raises(mcp_server.CallerRefused):
        await mcp_server.done_for_you()


@pytest.mark.asyncio
async def test_needs_you_is_homes_cards_for_the_caller() -> None:
    today = SimpleNamespace(proposals=[_Dumpable({"alert_id": 4, "headline": "Approve the PO"})])
    with patch.object(mcp_server, "_today_for", AsyncMock(return_value=today)), patch.object(
        mcp_server, "_require_person", AsyncMock(return_value=mcp_server.McpCaller(1, True))
    ):
        out = await mcp_server.needs_you()
    assert json.loads(out) == [{"alert_id": 4, "headline": "Approve the PO"}]


@pytest.mark.asyncio
async def test_watches_list_the_watchlist_through_its_route() -> None:
    # Through the route, so an email watch shows to its owner alone.
    request = object()
    route = Mock(return_value=[_Dumpable({"slug": "acme"})])
    with patch.object(
        mcp_server, "_require_person", AsyncMock(return_value=mcp_server.McpCaller(1, True))
    ), patch.object(mcp_server, "_http_request", return_value=request), patch(
        "openexecutive.api.routes.watchlist.list_watchlist", new=route
    ):
        assert json.loads(await mcp_server.watches()) == [{"slug": "acme"}]
    route.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_memory_decisions_resource_serializes_list() -> None:
    with patch(
        "openexecutive.memory.episodic.list_decisions",
        return_value=[_Dumpable({"id": 7, "title": "Pricing"})],
    ):
        out = await mcp_server.memory_decisions()
    assert json.loads(out) == [{"id": 7, "title": "Pricing"}]


# ---------------------------------------------------------------------------
# Tool handlers.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consult_specialist_routes_to_specialist() -> None:
    mock = AsyncMock(return_value="CFO analysis")
    with patch("openexecutive.orchestrator.router.route_to_specialist", new=mock):
        out = await mcp_server.consult_specialist("cfo", "runway?", "context here")
    assert out == "CFO analysis"
    mock.assert_awaited_once_with(
        "cfo", "runway?", context="context here", actor="specialist_mcp",
    )


@pytest.mark.asyncio
async def test_consult_specialist_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown specialist"):
        await mcp_server.consult_specialist("bogus", "q")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_knowledge_uses_retriever_and_store() -> None:
    mcp_server.set_store("STORE_SENTINEL")
    try:
        with patch(
            "openexecutive.knowledge.retriever.retrieve",
            return_value="[doc] chunk",
        ) as ret:
            out = await mcp_server.search_knowledge("unit economics")
        assert out == "[doc] chunk"
        ret.assert_called_once_with(query="unit economics", store="STORE_SENTINEL")
    finally:
        mcp_server.set_store(None)


@pytest.mark.asyncio
async def test_list_workflows_returns_catalog() -> None:
    out = await mcp_server.list_workflows()
    data = json.loads(out)
    assert isinstance(data, list) and data
    assert all("name" in w for w in data)


def _ask(who: Caller) -> tuple[str, Any]:
    profile = _Dumpable({})
    profile.is_empty = lambda: True  # type: ignore[attr-defined]
    chat = AsyncMock(return_value="Executive answer")

    async def _go() -> str:
        with (
            _as(who),
            patch(
                "openexecutive.onboarding.profile_builder.load_or_create_profile",
                return_value=profile,
            ),
            patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
            patch("openexecutive.orchestrator.executive.Executive.chat", new=chat),
        ):
            return await mcp_server.ask_executive("What's our top risk?")

    import asyncio

    return asyncio.run(_go()), chat


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (Caller("open"), "principal"),
        (Caller("operator"), "principal"),
        (Caller("open", TEAMMATE), "teammate"),
        (Caller("user", TEAMMATE), "teammate"),
        (Caller("user", OWNER), "principal"),
        (Caller("user", "stranger@else.example"), None),
        (Caller("service"), None),
    ],
)
def test_ask_executive_asks_as_the_verified_caller(
    roster: dict[str, int], who: Caller, expected: str | None
) -> None:
    out, chat = _ask(who)
    assert out == "Executive answer"
    person_id = roster[expected] if expected else None
    kwargs = chat.await_args.kwargs
    assert kwargs["person_id"] == person_id
    session = kwargs["session"]
    assert session.from_mcp_client is True
    assert session.caller_person_id == person_id
    from openexecutive.orchestrator.artifact_records import NOBODY

    assert (session.documents_viewer is NOBODY) is (person_id is None)


def test_ask_executive_takes_no_caller_argument() -> None:
    import inspect

    assert list(inspect.signature(mcp_server.ask_executive).parameters) == ["message"]


def test_with_no_request_the_caller_is_who_a_request_naming_no_one_would_be(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    assert mcp_server._who() == Caller("open")
    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", "k:" + "A" * 43)
    assert mcp_server._who() == Caller("service")


# ---------------------------------------------------------------------------
# Who may read what.
# ---------------------------------------------------------------------------
_ROSTER_ONLY = [
    "today_briefing", "needs_you", "people_roster", "memory_decisions", "memory_initiatives", "memory_advice",
]


def _read(name: str, who: Caller) -> str:
    import asyncio

    async def _go() -> str:
        with (
            _as(who),
            patch("openexecutive.memory.episodic.list_decisions", return_value=[]),
            patch("openexecutive.memory.episodic.list_initiatives", return_value=[]),
            patch("openexecutive.memory.episodic.list_advice", return_value=[]),
            patch(
                "openexecutive.api.routes.today._build_today",
                return_value=_Today({"people": []}),
            ),
            patch("openexecutive.departments.store.list_departments", return_value=[]),
        ):
            return str(await getattr(mcp_server, name)())

    return asyncio.run(_go())


@pytest.mark.parametrize("name", _ROSTER_ONLY)
@pytest.mark.parametrize(
    "who", [Caller("open"), Caller("operator"), Caller("user", OWNER), Caller("open", OWNER)]
)
def test_the_owner_reads_their_records(roster: dict[str, int], name: str, who: Caller) -> None:
    json.loads(_read(name, who))


@pytest.mark.parametrize("name", _ROSTER_ONLY)
def test_a_teammate_reads_them_but_a_service_does_not(
    roster: dict[str, int], name: str
) -> None:
    json.loads(_read(name, Caller("user", TEAMMATE)))
    for nobody in (Caller("service"), Caller("user", "stranger@else.example")):
        with pytest.raises(mcp_server.CallerRefused):
            _read(name, nobody)


def test_a_service_reads_no_watches(roster: dict[str, int]) -> None:
    rows = [_Dumpable({"slug": "acme"})]
    with patch("openexecutive.api.routes.watchlist.list_watchlist", return_value=rows), patch.object(
        mcp_server, "_http_request", return_value=object()
    ):
        assert json.loads(_read("watches", Caller("user", TEAMMATE))) == [{"slug": "acme"}]
        for nobody in (Caller("service"), Caller("user", "stranger@else.example")):
            with pytest.raises(mcp_server.CallerRefused):
                _read("watches", nobody)


def test_anyone_reads_the_departments(roster: dict[str, int]) -> None:
    assert json.loads(_read("departments_state", Caller("service"))) == []


def test_departments_say_their_approvals_level_by_name(roster: dict[str, int]) -> None:
    """The level each department's work runs at, named as the product names
    it, with its lead by name and no ids or channel settings."""
    from openexecutive.departments.models import (
        AuthorityLevel,
        DepartmentCharter,
        DepartmentConfig,
        DepartmentState,
        Goal,
    )

    state = DepartmentState(
        config=DepartmentConfig(
            slug="marketing", title="Marketing", charter=DepartmentCharter(mission="Grow"),
            authority_level=AuthorityLevel.AUTO_EXECUTE, head_person_id=roster["teammate"],
            slack_channel_id="C123", cadences={"check_in": "weekly@mon@09:00"},
        ),
        goals=[Goal(period_value="Q4 2026", key_result="Sign-ups", target="100")],
    )

    async def _go() -> str:
        with patch("openexecutive.departments.store.list_departments", return_value=[state]):
            return await mcp_server.departments_state()

    import asyncio

    [dept] = json.loads(asyncio.run(_go()))
    assert dept["approvals"] == "Hands-off"
    assert dept["lead"] == "Ben Teammate"
    assert dept["check_in"] == "weekly@mon@09:00"
    assert dept["goals"] == [
        {"goal": "Sign-ups", "target": "100", "current": "", "status": "on_track", "period": "Q4 2026"}
    ]
    assert "C123" not in json.dumps(dept) and "authority_level" not in dept


def test_the_briefing_is_narrowed_as_get_today_narrows_it(roster: dict[str, int]) -> None:
    import asyncio

    def _briefing(who: Caller) -> Any:
        build = patch(
            "openexecutive.api.routes.today._build_today",
            return_value=_Dumpable({"people": []}),
        )

        async def _go() -> Any:
            with _as(who), build as built:
                await mcp_server.today_briefing()
                return built

        return asyncio.run(_go())

    owner = _briefing(Caller("user", OWNER)).call_args.kwargs
    assert owner["include_private"] is True
    assert owner["viewer"].person_id == roster["principal"] and owner["viewer"].is_principal
    mate = _briefing(Caller("user", TEAMMATE)).call_args.kwargs
    assert mate["include_private"] is False
    assert mate["viewer"].person_id == roster["teammate"] and not mate["viewer"].is_principal


@pytest.mark.asyncio
async def test_tools_carry_titles_and_annotations() -> None:
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    for name in ("consult_specialist", "search_knowledge", "list_workflows"):
        ann = tools[name].annotations
        assert tools[name].title
        assert ann is not None and ann.readOnlyHint is True and ann.openWorldHint is False
    for name in ("ask_executive", "answer_card", "run_workflow"):
        acts = tools[name]
        assert acts.title and acts.annotations is not None
        assert acts.annotations.readOnlyHint is False
        assert acts.annotations.destructiveHint is True
        assert acts.annotations.idempotentHint is False
        # It can reach people outside, under the person's Approvals.
        assert acts.annotations.openWorldHint is True
    # Factual descriptions: no steering between tools.
    for tool in tools.values():
        assert "fallback" not in (tool.description or "").lower()
        assert "prefer" not in (tool.description or "").lower()


# ---------------------------------------------------------------------------
# Mount + auth.
# ---------------------------------------------------------------------------
def test_mount_adds_mcp_route() -> None:
    app = FastAPI()
    mcp_server.mount(app)
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)


def test_mcp_path_is_not_unauthenticated() -> None:
    """The /mcp endpoint must stay behind the shared-secret gate."""
    from openexecutive.api import main

    assert "/mcp" not in main._UNAUTHENTICATED_PATHS


def test_mcp_endpoint_is_gated_by_shared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BACKEND_SHARED_SECRET set, /mcp returns 401 without the header and
    passes the gate (no 401) with it. Routing only — lifespan is not run."""
    from openexecutive.api.main import create_app

    monkeypatch.setenv("BACKEND_SHARED_SECRET", "testsecret")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    app = create_app()
    client = TestClient(app)  # not a context manager → lifespan does not run

    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream"}

    no_key = client.post("/mcp", json=body, headers=headers, follow_redirects=False)
    assert no_key.status_code == 401

    with_key = client.post(
        "/mcp",
        json=body,
        headers={**headers, "x-api-key": "testsecret"},
        follow_redirects=False,
    )
    assert with_key.status_code != 401


def test_an_mcp_call_with_no_http_request_names_no_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    context = SimpleNamespace(request_context=SimpleNamespace(request=None))
    with patch.object(mcp_server.mcp, "get_context", return_value=context):
        assert mcp_server._who() == Caller("service")


def test_a_caller_who_is_no_one_sees_no_names_in_the_departments(roster: dict[str, int]) -> None:
    """Who leads a department is the team's to know, as the People list is."""
    import asyncio

    from openexecutive.departments.models import (
        DepartmentCharter,
        DepartmentConfig,
        DepartmentState,
    )

    state = DepartmentState(
        config=DepartmentConfig(
            slug="ops", title="Ops", charter=DepartmentCharter(mission="Run"),
            head_person_id=roster["teammate"],
        ),
    )

    async def _go() -> str:
        with (
            patch("openexecutive.departments.store.list_departments", return_value=[state]),
            patch.object(mcp_server, "_who", return_value=Caller("service")),
        ):
            return await mcp_server.departments_state()

    [dept] = json.loads(asyncio.run(_go()))
    assert dept["lead"] is None and dept["people"] == []
