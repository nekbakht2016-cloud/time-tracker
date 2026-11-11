import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import create_app  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture()
def client(db):
    return TestClient(create_app(db))


def add(client, name="Иван"):
    r = client.post("/api/employees", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def test_add_and_list_employees(client):
    add(client, "Анна")
    add(client, "Борис")
    names = [e["name"] for e in client.get("/api/employees").json()]
    assert names == ["Анна", "Борис"]


def test_employee_validation(client):
    assert client.post("/api/employees", json={"name": "   "}).status_code == 422
    add(client, "Анна")
    assert client.post("/api/employees", json={"name": "Анна"}).status_code == 409


def test_start_and_stop_timer(client):
    eid = add(client)
    assert client.post("/api/timer/start", json={"employee_id": eid, "task": "Отчёт"}).status_code == 201
    assert client.get("/api/employees").json()[0]["running_task"] == "Отчёт"
    r = client.post("/api/timer/stop", json={"employee_id": eid})
    assert r.status_code == 200 and r.json()["seconds"] >= 0
    assert client.get("/api/employees").json()[0]["running_task"] is None


def test_repeated_start_is_rejected(client):
    eid = add(client)
    client.post("/api/timer/start", json={"employee_id": eid, "task": "A"})
    r = client.post("/api/timer/start", json={"employee_id": eid, "task": "B"})
    assert r.status_code == 409
    report = client.get("/api/report", params={"employee_id": eid}).json()
    assert len(report["entries"]) == 1


def test_stop_without_start_is_rejected(client):
    eid = add(client)
    assert client.post("/api/timer/stop", json={"employee_id": eid}).status_code == 409


def test_empty_task_is_rejected(client):
    eid = add(client)
    assert client.post("/api/timer/start", json={"employee_id": eid, "task": "  "}).status_code == 422


def test_unknown_employee(client):
    assert client.post("/api/timer/start", json={"employee_id": 999, "task": "x"}).status_code == 404
    assert client.get("/api/report", params={"employee_id": 999}).status_code == 404


def test_empty_report(client):
    eid = add(client)
    r = client.get("/api/report", params={"employee_id": eid, "date_from": "2020-01-01", "date_to": "2020-01-31"})
    assert r.status_code == 200
    assert r.json()["entries"] == [] and r.json()["total_seconds"] == 0


def test_report_period_filter(client, db):
    import sqlite3
    eid = add(client)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO entries(employee_id, task, started_at, stopped_at) VALUES (?, 'old', '2026-01-10T09:00:00+00:00', '2026-01-10T10:30:00+00:00')", (eid,))
    con.execute("INSERT INTO entries(employee_id, task, started_at, stopped_at) VALUES (?, 'new', '2026-02-05T09:00:00+00:00', '2026-02-05T09:45:00+00:00')", (eid,))
    con.commit(); con.close()
    r = client.get("/api/report", params={"employee_id": eid, "date_from": "2026-01-01", "date_to": "2026-01-31"}).json()
    assert [e["task"] for e in r["entries"]] == ["old"] and r["total_seconds"] == 5400
    r = client.get("/api/report", params={"employee_id": eid}).json()
    assert r["total_seconds"] == 5400 + 2700


def test_bad_dates(client):
    eid = add(client)
    assert client.get("/api/report", params={"employee_id": eid, "date_from": "вчера"}).status_code == 422
    assert client.get("/api/report", params={"employee_id": eid, "date_from": "2026-02-01", "date_to": "2026-01-01"}).status_code == 422


def test_data_survives_restart(db):
    c1 = TestClient(create_app(db))
    eid = add(c1)
    c1.post("/api/timer/start", json={"employee_id": eid, "task": "Задача"})
    c2 = TestClient(create_app(db))  # «перезапуск»: новое приложение, тот же файл
    emp = c2.get("/api/employees").json()
    assert emp[0]["name"] == "Иван" and emp[0]["running_task"] == "Задача"
    assert c2.post("/api/timer/stop", json={"employee_id": eid}).status_code == 200
