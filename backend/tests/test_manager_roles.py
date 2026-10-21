"""Шлюз ролей менеджера (WP-F): разбор edge-заголовков и матрица 403.

Оператор (нет группы engineers/admins) не проходит изменяющие эндпоинты —
ни на личной полосе (с Remote-User), ни на дефолтной (без него).
Неизвестный пользователь — оператор. При заданном IMITATOR_EDGE_TOKEN
заголовки Remote-* принимаются только вместе с X-Edge-Token.
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from httpx import MockTransport, Response

from app.manager import roles
from app.manager.instances import InstanceRegistry, InstanceRecord
from app.manager.proxy import InstanceProxy
from app.manager.routes import ManagerDispatchMiddleware

RECORD = InstanceRecord(
    user="ivan",
    slot=0,
    http_port=8101,
    opc_port=4841,
    username="imit_ivan",
    password="pw",
    created_at="2026-10-21T00:00:00+00:00",
    updated_at="2026-10-21T00:00:00+00:00",
)


# -- разбор заголовков -------------------------------------------------------


def test_normalize_user_strips_and_casefolds():
    assert roles.normalize_user("  Ivan Petrov ") == "ivan petrov"
    assert roles.normalize_user("\"<>Ivan[]'") == "ivan"
    assert roles.normalize_user("A" * 100) == "a" * 64
    assert roles.normalize_user("") == ""


def test_parse_groups_separators_and_case():
    assert roles.parse_groups("Engineers; Admins") == {"engineers", "admins"}
    assert roles.parse_groups("") == set()


def test_role_resolution_defaults_to_operator():
    assert roles.role_of({}) is roles.Role.OPERATOR
    assert roles.role_of({"remote-groups": "operators,viewers"}) is roles.Role.OPERATOR
    assert roles.role_of({"remote-groups": "engineers"}) is roles.Role.ENGINEER
    assert roles.role_of({"remote-groups": "engineers,admins"}) is roles.Role.ADMIN
    # Без пользователя роль всё равно определена (оператор).
    assert roles.identity_of({}) is None
    assert roles.resolve({}) == (None, roles.Role.OPERATOR)


def test_mutating_matrix():
    mutating = [
        ("POST", "/api/signals/write"),
        ("POST", "/api/signals/write_batch"),
        ("POST", "/api/signals/reset"),
        ("POST", "/api/states/save"),
        ("DELETE", "/api/states/delete/x.json"),
        ("POST", "/api/scenarios/start"),
        ("POST", "/api/scenarios/pause"),
        ("POST", "/api/machine/start"),
        ("POST", "/api/playback/load_path"),
        ("PUT", "/api/heartbeat"),
    ]
    for method, path in mutating:
        assert roles.is_mutating(method, path), (method, path)
    reading = [
        ("GET", "/api/signals"),
        ("GET", "/api/states/list"),
        ("GET", "/api/scenarios/status"),
        ("GET", "/api/machine"),
        ("GET", "/api/playback/status"),
        ("GET", "/api/heartbeat"),
        ("GET", "/api/diagnostics/events"),
        ("POST", "/api/station/snapshots"),  # вне матрицы WP-F: не изменяющий имитатора
    ]
    for method, path in reading:
        assert not roles.is_mutating(method, path), (method, path)


# -- шлюз на ASGI-уровне -------------------------------------------------------


class Delegate:
    """Дефолтная полоса: отвечает, помечая себя, — видно, кто обслужил."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        body = json.dumps({"lane": "default", "path": scope["path"]}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _make_client(monkeypatch, tmp_path, *, edge_token: str = ""):
    registry = InstanceRegistry(users_root=tmp_path / "users", spawn_enabled=False)
    registry._records["ivan"] = RECORD
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return Response(200, json={"lane": "child"})

    proxy = InstanceProxy(client=MockTransport(handler))
    app = ManagerDispatchMiddleware(Delegate(), registry, proxy)
    if edge_token:
        monkeypatch.setenv("IMITATOR_EDGE_TOKEN", edge_token)
    return TestClient(app), captured


def _operator():
    return {"Remote-User": "op", "Remote-Groups": "operators"}


def _engineer():
    return {"Remote-User": "ivan", "Remote-Groups": "engineers"}


OPERATOR_FORBIDDEN = [
    ("post", "/api/signals/write"),
    ("post", "/api/signals/write_batch"),
    ("post", "/api/signals/reset"),
    ("post", "/api/states/save"),
    ("delete", "/api/states/delete/state.json"),
    ("post", "/api/scenarios/start"),
    ("post", "/api/scenarios/stop"),
    ("post", "/api/machine/start"),
    ("post", "/api/playback/control"),
    ("put", "/api/heartbeat"),
]

EVERYONE_ALLOWED = [
    ("get", "/api/signals"),
    ("get", "/api/states/list"),
    ("get", "/api/scenarios/status"),
    ("get", "/api/machine"),
    ("get", "/api/playback/status"),
    ("get", "/api/heartbeat"),
    ("get", "/"),
]


@pytest.mark.parametrize("method,path", OPERATOR_FORBIDDEN)
def test_operator_write_rejected_on_personal_lane(monkeypatch, tmp_path, method, path):
    client, captured = _make_client(monkeypatch, tmp_path)
    response = client.request(method.upper(), path, json={"a": 1}, headers=_operator())
    assert response.status_code == 403
    assert response.json() == {"code": "role_forbidden"}
    assert "lane" not in captured  # до дочернего экземпляра не дошло


@pytest.mark.parametrize("method,path", OPERATOR_FORBIDDEN)
def test_operator_write_rejected_on_default_lane(monkeypatch, tmp_path, method, path):
    """Без Remote-User запрос обслуживает сам менеджер — но записать оператор
    не может и в дефолтный экземпляр (шлюз закрывает обе полосы)."""
    client, _ = _make_client(monkeypatch, tmp_path)
    response = client.request(method.upper(), path, json={"a": 1})
    assert response.status_code == 403
    assert response.json() == {"code": "role_forbidden"}
    body = response.json()
    assert "lane" not in body  # делегат не отвечал — отказал шлюз


@pytest.mark.parametrize("method,path", EVERYONE_ALLOWED)
def test_read_endpoints_open_to_operator_on_both_lanes(monkeypatch, tmp_path, method, path):
    client, captured = _make_client(monkeypatch, tmp_path)
    personal = client.request(method.upper(), path, headers=_operator())
    assert personal.status_code == 200
    assert personal.json() == {"lane": "child"}  # личная полоса → дочерний
    default = client.request(method.upper(), path)
    assert default.status_code == 200
    if path != "/":  # корень на дефолтной полосе не имеет значения для матрицы
        assert default.json()["lane"] == "default"


@pytest.mark.parametrize("method,path", OPERATOR_FORBIDDEN)
def test_engineer_passes_gate_on_personal_lane(monkeypatch, tmp_path, method, path):
    client, captured = _make_client(monkeypatch, tmp_path)
    response = client.request(method.upper(), path, json={"a": 1}, headers=_engineer())
    assert response.status_code == 200
    assert captured["method"] == method.upper()
    assert captured["url"].startswith("http://127.0.0.1:8101/")
    assert captured["url"].endswith(path)


def test_unknown_user_is_operator(monkeypatch, tmp_path):
    client, _ = _make_client(monkeypatch, tmp_path)
    response = client.post(
        "/api/signals/write",
        json={"id": "x"},
        headers={"Remote-User": "mallory", "Remote-Groups": "bosses,guests"},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "role_forbidden"}
    # Читать mallory может — у него свой экземпляр.
    reading = client.get("/api/signals", headers={"Remote-User": "mallory"})
    assert reading.status_code == 200
    assert reading.json() == {"lane": "child"}


def test_attestation_gates_elevated_writer(monkeypatch, tmp_path):
    """Без аттестации «инженерные» заголовки — оператор (дефолтная полоса, 403);
    с пройденной аттестацией инженер пишет — в свой личный экземпляр."""
    client, _ = _make_client(monkeypatch, tmp_path, edge_token="edge-secret")
    denied = client.post(
        "/api/heartbeat",
        json={"enabled": True},
        headers={"Remote-User": "anna", "Remote-Groups": "admins"},  # нет X-Edge-Token
    )
    assert denied.status_code == 403
    assert denied.json() == {"code": "role_forbidden"}

    wrong = client.post(
        "/api/heartbeat",
        json={"enabled": True},
        headers={
            "Remote-User": "anna",
            "Remote-Groups": "admins",
            "X-Edge-Token": "nope",
        },
    )
    assert wrong.status_code == 403

    allowed = client.post(
        "/api/heartbeat",
        json={"enabled": True},
        headers={
            "Remote-User": "anna",
            "Remote-Groups": "admins",
            "X-Edge-Token": "edge-secret",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["lane"] == "child"


def test_capacity_overflow_is_503_instance_limit(monkeypatch, tmp_path):
    """Ёмкость исчерпана → 503 {"code": "instance_limit"} на личной полосе."""
    registry = InstanceRegistry(
        users_root=tmp_path / "users",
        max_instances=1,
        spawn_enabled=False,
    )
    registry._records["busy"] = replace(RECORD, user="busy", slot=0)
    captured = {}

    def handler(request):
        captured["hit"] = True
        return Response(200, json={"lane": "child"})

    proxy = InstanceProxy(client=MockTransport(handler))
    app = ManagerDispatchMiddleware(Delegate(), registry, proxy)
    with TestClient(app) as client:
        response = client.get("/api/signals", headers=_engineer())
    assert response.status_code == 503
    assert response.json() == {"code": "instance_limit"}
    assert "hit" not in captured  # до дочернего не дошло
