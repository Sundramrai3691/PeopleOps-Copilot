"""Synthetic PeopleOps data, policy retrieval, authorization, and MCP operations."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY, name TEXT UNIQUE, cost_center TEXT, manager_id INTEGER);
CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY, employee_code TEXT UNIQUE, name TEXT, email TEXT, department_id INTEGER REFERENCES departments(id), manager_id INTEGER REFERENCES employees(id), job_title TEXT, location TEXT, joining_date TEXT, employment_status TEXT);
CREATE TABLE IF NOT EXISTS leave_balances(employee_id INTEGER REFERENCES employees(id), leave_type TEXT, available_days REAL, used_days REAL, year INTEGER, PRIMARY KEY(employee_id,leave_type,year));
CREATE TABLE IF NOT EXISTS leave_requests(id TEXT PRIMARY KEY, employee_id INTEGER REFERENCES employees(id), leave_type TEXT, start_date TEXT, end_date TEXT, reason TEXT, status TEXT, approver_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS attendance_daily(employee_id INTEGER REFERENCES employees(id), work_date TEXT, status TEXT, hours_worked REAL, PRIMARY KEY(employee_id,work_date));
CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY, actor_id TEXT, action TEXT, tool_name TEXT, decision TEXT, input_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS action_proposals(id TEXT PRIMARY KEY, actor TEXT, payload TEXT, status TEXT, created_at TEXT);
"""

DEPARTMENTS = ["Engineering", "People", "Finance", "Operations", "Sales", "Customer Support"]


class _PostgresConnection:
    """Small adapter for qmark SQL shared by the SQLite and PostgreSQL paths."""
    def __init__(self, connection):
        self.connection = connection
    def execute(self, statement, parameters=()):
        return self.connection.execute(statement.replace("?", "%s"), parameters)
    def executemany(self, statement, parameters):
        with self.connection.cursor() as cursor:
            return cursor.executemany(statement.replace("?", "%s"), parameters)
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, *args):
        return self.connection.__exit__(*args)
    def close(self):
        self.connection.close()
    def commit(self):
        self.connection.commit()


class _SQLiteConnection:
    """Match PostgreSQL's close-on-context-exit behavior on every platform."""
    def __init__(self, connection):
        self.connection = connection
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, *args):
        try:
            return self.connection.__exit__(*args)
        finally:
            self.connection.close()
    def __getattr__(self, name):
        return getattr(self.connection, name)


class PeopleOpsService:
    def __init__(self, database: str | None = None):
        self.database = database or os.getenv("PEOPLEOPS_DATABASE", "runtime/peopleops.sqlite")
        self.postgres_url = os.getenv("PEOPLEOPS_DATABASE_URL") if database is None else None
        if self.database != ":memory:" and not self.postgres_url:
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self):
        if self.postgres_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("Install the prod extra to use PostgreSQL PeopleOps storage") from exc
            return _PostgresConnection(psycopg.connect(self.postgres_url, row_factory=dict_row))
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return _SQLiteConnection(db)

    def _initialize(self):
        with self.connect() as db:
            if self.postgres_url:
                for statement in SCHEMA.strip().split(";\n"):
                    if statement.strip(): db.execute(statement)
            else:
                db.executescript(SCHEMA)
            if db.execute("SELECT count(*) AS count FROM employees").fetchone()["count"]:
                return
            departments = [(i, name, f"CC-{410+i}", (i-1)*20+1) for i,name in enumerate(DEPARTMENTS, 1)]
            today = date.today()
            employees, balances, attendance, requests = [], [], [], []
            for i in range(1, 121):
                code = f"E{i:03d}"
                dept = ((i - 1) // 20) + 1
                manager = None if (i - 1) % 20 == 0 else ((dept - 1) * 20 + 1)
                employees.append((i, code, f"Employee {i:03d}", f"{code.lower()}@example.test", dept, manager, "Department Lead" if (i-1)%20 == 0 else "Operations Specialist", ["New York", "Austin", "London", "Remote"][i % 4], (today-timedelta(days=30*(i%60+6))).isoformat(), "active"))
                balances.extend([(i,"annual",float(5+i%14),float(i%8),today.year),(i,"sick",float(4+i%7),float(i%4),today.year)])
                start = today - timedelta(days=180)
                for offset in range(180):
                    day = start + timedelta(days=offset)
                    if day.weekday() >= 5:
                        continue
                    status = "present" if (i+offset)%19 else "leave"
                    attendance.append((i, day.isoformat(), status, 8.0 if status == "present" else 0.0))
            for i in range(1, 31):
                requests.append((f"LR-{i:04d}",i,"annual",(today+timedelta(days=i+10)).isoformat(),(today+timedelta(days=i+11)).isoformat(),"Personal leave", "approved" if i%3 else "pending", max(1,((i-1)//20)*20+1), datetime.now(timezone.utc).isoformat()))
            db.executemany("INSERT INTO departments VALUES(?,?,?,?)", departments)
            db.executemany("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?)", employees)
            db.executemany("INSERT INTO leave_balances VALUES(?,?,?,?,?)", balances)
            db.executemany("INSERT INTO attendance_daily VALUES(?,?,?,?)", attendance)
            db.executemany("INSERT INTO leave_requests VALUES(?,?,?,?,?,?,?,?,?)", requests)

    @staticmethod
    def _audit(db, actor, action, tool, decision, payload):
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        db.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), actor, action, tool, decision, digest, datetime.now(timezone.utc).isoformat()))

    def _deny(self, db, actor, action, payload, message):
        self._audit(db, actor, action, action, "denied", payload)
        db.commit()
        raise PermissionError(message)

    def _employee(self, db, code):
        row = db.execute("SELECT * FROM employees WHERE employee_code=? AND employment_status='active'", (code,)).fetchone()
        if not row:
            raise ValueError("active employee not found")
        return row

    def _authorize(self, db, actor, role, target_code, *, aggregate=False):
        if role in {"hr", "admin"}:
            return
        if not actor:
            self._deny(db, actor, "authorization", {"target":target_code}, "employee identity is required")
        target = self._employee(db, target_code)
        if actor == target_code:
            return
        if role == "manager" and not aggregate:
            manager = db.execute("SELECT id FROM employees WHERE employee_code=?", (actor,)).fetchone()
            if manager and target["manager_id"] == manager["id"]:
                return
        if role == "manager" and aggregate:
            return
        self._deny(db, actor, "authorization", {"target":target_code}, "role is not authorized for this employee data")

    def get_employee_profile(self, employee_code, actor, role):
        with self.connect() as db:
            self._authorize(db, actor, role, employee_code)
            row = self._employee(db, employee_code)
            data = {k: row[k] for k in ("employee_code","name","email","department_id","manager_id","job_title","location","joining_date","employment_status")}
            self._audit(db, actor, "profile_read", "get_employee_profile", "allowed", {"employee_code":employee_code})
            return data

    def get_leave_balance(self, employee_code, actor, role):
        with self.connect() as db:
            self._authorize(db, actor, role, employee_code)
            rows = db.execute("SELECT leave_type,available_days,used_days,year FROM leave_balances WHERE employee_id=(SELECT id FROM employees WHERE employee_code=?)", (employee_code,)).fetchall()
            self._audit(db, actor, "leave_balance_read", "get_leave_balance", "allowed", {"employee_code":employee_code})
            return {"employee_code":employee_code,"balances":[dict(r) for r in rows]}

    def get_team_headcount(self, department, actor, role):
        if role not in {"manager","hr","admin"}:
            raise PermissionError("manager role required for team analytics")
        with self.connect() as db:
            if role == "manager":
                manager = db.execute("SELECT d.name FROM employees e JOIN departments d ON d.id=e.department_id WHERE e.employee_code=?", (actor,)).fetchone()
                if not manager:
                    self._deny(db, actor, "team_headcount_authorization", {"department":department}, "manager employee identity is not recognized")
                if department and department != manager["name"]:
                    self._deny(db, actor, "team_headcount_authorization", {"department":department}, "managers may view only their own department aggregate")
                department = manager["name"]
            rows = db.execute("SELECT d.name AS department,count(e.id) AS headcount FROM departments d LEFT JOIN employees e ON e.department_id=d.id AND e.employment_status='active' WHERE (CAST(? AS TEXT) IS NULL OR d.name=?) GROUP BY d.id ORDER BY d.name LIMIT 50", (department,department)).fetchall()
            self._audit(db, actor, "team_headcount", "get_team_headcount", "allowed", {"department":department})
            sql = "SELECT d.name AS department, count(e.id) AS headcount FROM departments d LEFT JOIN employees e ON e.department_id=d.id AND e.employment_status='active' WHERE (CAST(? AS TEXT) IS NULL OR d.name=?) GROUP BY d.id ORDER BY d.name LIMIT 50"
            return {"answer":{"departments":[dict(r) for r in rows]},"explainability":{"sql":sql,"tables_used":["departments","employees"],"filters":[f"department={department}" if department else "all departments","active employees only"],"validation":"read_only_allowlisted_select","result_summary":f"{len(rows)} department row(s)","authorization":"allowed"}}

    def get_attendance_summary(self, employee_code, actor, role):
        with self.connect() as db:
            self._authorize(db, actor, role, employee_code)
            since = (date.today()-timedelta(days=90)).isoformat()
            rows = db.execute("SELECT status,count(*) AS days,round(CAST(avg(hours_worked) AS NUMERIC),2) AS average_hours FROM attendance_daily WHERE employee_id=(SELECT id FROM employees WHERE employee_code=?) AND work_date>=? GROUP BY status", (employee_code,since)).fetchall()
            self._audit(db, actor, "attendance_read", "get_attendance_summary", "allowed", {"employee_code":employee_code})
            return {"employee_code":employee_code,"period_days":90,"summary":[dict(r) for r in rows]}

    def create_leave_request(self, employee_code, leave_type, start_date, end_date, reason, actor, role):
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("dates must use YYYY-MM-DD") from exc
        if end < start or start < date.today() or (end-start).days > 30 or start.year != end.year:
            raise ValueError("leave dates must be future dates within a 31-day period")
        if leave_type not in {"annual","sick"} or len(reason)>500:
            raise ValueError("invalid leave type or reason")
        payload = {"employee_code":employee_code,"leave_type":leave_type,"start_date":start.isoformat(),"end_date":end.isoformat(),"reason":reason}
        with self.connect() as db:
            self._authorize(db, actor, role, employee_code)
            employee = self._employee(db, employee_code)
            days = (end-start).days+1
            balance = db.execute("SELECT available_days FROM leave_balances WHERE employee_id=? AND leave_type=? AND year=?", (employee["id"],leave_type,start.year)).fetchone()
            if not balance or balance["available_days"] < days:
                raise ValueError("insufficient leave balance")
            approver = employee["manager_id"]
            if not approver:
                raise ValueError("no manager is assigned to approve this request")
            ident = str(uuid.uuid4())
            db.execute("INSERT INTO action_proposals VALUES(?,?,?,?,?)", (ident, actor, json.dumps(payload), "pending", datetime.now(timezone.utc).isoformat()))
            self._audit(db, actor, "leave_request_proposed", "create_leave_request", "approval_required", payload)
            return {"approval_required":True,"approval_id":ident,"status":"pending_approval","preview":payload,"approver_id":approver}

    def review_leave_request(self, approval_id, approver, role, decision):
        if role not in {"manager","hr","admin"}:
            raise PermissionError("manager role required to review leave requests")
        if decision not in {"approve","reject"}:
            raise ValueError("decision must be approve or reject")
        with self.connect() as db:
            proposal = db.execute("SELECT * FROM action_proposals WHERE id=?", (approval_id,)).fetchone()
            if not proposal or proposal["status"] != "pending":
                raise ValueError("pending approval not found")
            payload = json.loads(proposal["payload"])
            employee = self._employee(db, payload["employee_code"])
            if role == "manager":
                manager = db.execute("SELECT id FROM employees WHERE employee_code=?", (approver,)).fetchone()
                if not manager or manager["id"] != employee["manager_id"]:
                    self._deny(db, approver, "approval_authorization", {"approval_id":approval_id}, "only the employee's direct manager may approve")
            if decision == "approve":
                rid = "LR-" + uuid.uuid4().hex[:12]
                db.execute("INSERT INTO leave_requests VALUES(?,?,?,?,?,?,?,?,?)", (rid,employee["id"],payload["leave_type"],payload["start_date"],payload["end_date"],payload["reason"],"approved",employee["manager_id"],datetime.now(timezone.utc).isoformat()))
                days = (date.fromisoformat(payload["end_date"])-date.fromisoformat(payload["start_date"])).days+1
                changed = db.execute("UPDATE leave_balances SET available_days=available_days-?,used_days=used_days+? WHERE employee_id=? AND leave_type=? AND year=? AND available_days>=?", (days,days,employee["id"],payload["leave_type"],date.fromisoformat(payload["start_date"]).year,days))
                if changed.rowcount != 1:
                    raise ValueError("leave balance changed before approval; reject this proposal")
            db.execute("UPDATE action_proposals SET status=? WHERE id=?", (decision+"d",approval_id))
            self._audit(db, approver, "leave_request_"+decision, "review_leave_request", "allowed", {"approval_id":approval_id})
            return {"approval_id":approval_id,"status":decision+"d","request_id":rid if decision=="approve" else None}

    def ask_policy(self, question):
        from .retrieval import KnowledgeRetriever
        from .retrieval import tokens
        policy_path = Path(__file__).resolve().parents[2]/"data/peopleops/policies.json"
        if not policy_path.is_file():
            policy_path = Path.cwd()/"data/peopleops/policies.json"
        corpus = json.loads(policy_path.read_text(encoding="utf-8"))
        docs = [{"id":f"{item['policy_id']}:{item['section']}","database_id":"peopleops-policy","title":f"{item['title']} — {item['section']}","text":item["text"],"source":item["policy_id"],"version":corpus["effective_date"],"tables":[]} for item in corpus["policies"]]
        hits = KnowledgeRetriever(docs).retrieve("peopleops-policy",question,[],top_k=3,max_chars=2500)["hits"]
        stop = {"a","an","and","are","as","at","be","by","can","company","do","does","for","from","how","i","in","is","it","may","of","on","or","should","the","to","was","what","when","where","which","who","why","with"}
        query_terms = set(tokens(question)) - stop
        hits = [hit for hit in hits if query_terms.intersection(tokens(hit["title"] + " " + hit["text"]))]
        return {"answer":"\n".join(h["text"] for h in hits) if hits else "I could not find supporting policy evidence for that question.","sources":[{"policy_id":h["source"],"title":h["title"],"section":h["title"].split(" — ",1)[-1],"effective_date":"2026-01-01"} for h in hits],"evidence_sufficient":bool(hits)}
