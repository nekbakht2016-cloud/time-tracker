"""Учёт рабочего времени сотрудников: FastAPI + SQLite."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

BASE = Path(__file__).parent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(db_path: str) -> None:
    with connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                task TEXT NOT NULL,
                started_at TEXT NOT NULL,
                stopped_at TEXT
            );
            -- у одного сотрудника не может быть двух запущенных таймеров
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_employee
                ON entries(employee_id) WHERE stopped_at IS NULL;
            """
        )


class EmployeeIn(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Имя не может быть пустым")
        return v


class StartIn(BaseModel):
    employee_id: int
    task: str

    @field_validator("task")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Описание задачи не может быть пустым")
        return v


class StopIn(BaseModel):
    employee_id: int


def seconds_between(start: str, stop: str | None) -> int:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(stop) if stop else datetime.now(timezone.utc)
    return max(0, int((b - a).total_seconds()))


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("TT_DB", str(BASE / "timetracker.db"))
    init_db(path)
    app = FastAPI(title="Учёт рабочего времени")

    def employee_or_404(con, employee_id: int):
        row = con.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Сотрудник не найден")
        return row

    @app.post("/api/employees", status_code=201)
    def add_employee(body: EmployeeIn):
        with connect(path) as con:
            try:
                cur = con.execute("INSERT INTO employees(name) VALUES (?)", (body.name,))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Сотрудник с таким именем уже есть")
            return {"id": cur.lastrowid, "name": body.name}

    @app.get("/api/employees")
    def list_employees():
        with connect(path) as con:
            rows = con.execute(
                """SELECT e.id, e.name,
                          (SELECT task FROM entries WHERE employee_id = e.id AND stopped_at IS NULL) AS running_task
                   FROM employees e ORDER BY e.name"""
            ).fetchall()
            return [dict(r) for r in rows]

    @app.post("/api/timer/start", status_code=201)
    def start_timer(body: StartIn):
        with connect(path) as con:
            employee_or_404(con, body.employee_id)
            try:
                cur = con.execute(
                    "INSERT INTO entries(employee_id, task, started_at) VALUES (?, ?, ?)",
                    (body.employee_id, body.task, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Таймер уже запущен. Сначала остановите его")
            return {"id": cur.lastrowid, "task": body.task}

    @app.post("/api/timer/stop")
    def stop_timer(body: StopIn):
        with connect(path) as con:
            employee_or_404(con, body.employee_id)
            cur = con.execute(
                "UPDATE entries SET stopped_at = ? WHERE employee_id = ? AND stopped_at IS NULL",
                (now_iso(), body.employee_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(409, "Нет запущенного таймера")
            row = con.execute(
                "SELECT * FROM entries WHERE employee_id = ? ORDER BY id DESC LIMIT 1",
                (body.employee_id,),
            ).fetchone()
            return {"id": row["id"], "seconds": seconds_between(row["started_at"], row["stopped_at"])}

    @app.get("/api/report")
    def report(employee_id: int, date_from: str | None = None, date_to: str | None = None):
        """Записи сотрудника, начатые в период [date_from, date_to] (даты ГГГГ-ММ-ДД, включительно)."""
        for d in (date_from, date_to):
            if d:
                try:
                    datetime.strptime(d, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(422, "Дата должна быть в формате ГГГГ-ММ-ДД")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "Дата «с» позже даты «по»")
        with connect(path) as con:
            emp = employee_or_404(con, employee_id)
            sql = "SELECT * FROM entries WHERE employee_id = ?"
            args: list = [employee_id]
            if date_from:
                sql += " AND substr(started_at, 1, 10) >= ?"
                args.append(date_from)
            if date_to:
                sql += " AND substr(started_at, 1, 10) <= ?"
                args.append(date_to)
            rows = con.execute(sql + " ORDER BY started_at", args).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "task": r["task"],
                    "started_at": r["started_at"],
                    "stopped_at": r["stopped_at"],
                    "seconds": seconds_between(r["started_at"], r["stopped_at"]),
                    "running": r["stopped_at"] is None,
                }
                for r in rows
            ]
            return {
                "employee": emp["name"],
                "entries": entries,
                "total_seconds": sum(e["seconds"] for e in entries),
            }

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(BASE / "static" / "index.html")

    return app


app = create_app()
