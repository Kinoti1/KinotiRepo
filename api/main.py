from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DATA_DIR = LOCAL_DATA_ROOT / "Metasync" / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "metasync.sqlite3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS_PER_REPORT = 50_000
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
SESSION_COOKIE = "metasync_session"
SESSION_TTL = timedelta(days=7)
PASSWORD_ITERATIONS = 310_000
ACTIVE_USER: ContextVar[dict[str, Any] | None] = ContextVar("metasync_active_user", default=None)

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="Metasync Metadata API",
    description=(
        "Local prototype API for an academic library consortium metadata hub. "
        "It accepts tabular reports, preserves source rows, normalizes records, "
        "finds duplicate files and records, and exposes a shared catalog."
    ),
    version="0.3.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


class ResolveDuplicateRequest(BaseModel):
    action: str = Field(pattern="^(merge|ignore)$")


class CleanRules(BaseModel):
    normalize_author_names: bool = True
    standardize_subjects: bool = True
    fix_missing_fields: bool = True
    remove_exact_duplicates: bool = True
    format_publication_dates: bool = True


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    institution: str = Field(min_length=2, max_length=180)
    workspace: str | None = Field(default=None, max_length=180)
    password: str = Field(min_length=12, max_length=256)
    invitation_code: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ChangeEmailRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_email: str = Field(min_length=5, max_length=254)


class InstitutionRequest(BaseModel):
    name: str = Field(min_length=2, max_length=180)


class ResetWorkspaceRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    confirmation: str = Field(pattern="^RESET$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                institution TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS organization_invites (
                token_hash TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                created_by TEXT NOT NULL REFERENCES users(id),
                expires_at TEXT NOT NULL,
                used_at TEXT,
                used_by TEXT REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS institutions (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(organization_id, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_type TEXT NOT NULL,
                institution TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Uploaded',
                organization_id TEXT NOT NULL DEFAULT 'legacy',
                uploaded_by TEXT NOT NULL DEFAULT '',
                UNIQUE(organization_id, sha256)
            );
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                row_number INTEGER NOT NULL,
                original_json TEXT NOT NULL,
                cleaned_json TEXT,
                cleaned_at TEXT,
                merged_into TEXT REFERENCES records(id),
                auto_merged INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(report_id, row_number)
            );
            CREATE INDEX IF NOT EXISTS idx_records_report ON records(report_id);
            CREATE INDEX IF NOT EXISTS idx_records_merged ON records(merged_into);
            CREATE TABLE IF NOT EXISTS duplicate_decisions (
                pair_id TEXT PRIMARY KEY,
                record_a TEXT NOT NULL,
                record_b TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('merge', 'ignore')),
                decided_at TEXT NOT NULL
            );
            """
        )

        report_columns = {row["name"] for row in db.execute("PRAGMA table_info(reports)")}
        if "organization_id" not in report_columns:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE reports_new (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    stored_filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    institution TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    columns_json TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Uploaded',
                    organization_id TEXT NOT NULL DEFAULT 'legacy',
                    uploaded_by TEXT NOT NULL DEFAULT '',
                    UNIQUE(organization_id, sha256)
                )"""
            )
            db.execute(
                """INSERT INTO reports_new
                   (id, filename, stored_filename, sha256, file_type, institution, row_count,
                    columns_json, uploaded_at, status, organization_id, uploaded_by)
                   SELECT id, filename, stored_filename, sha256, file_type, institution, row_count,
                          columns_json, uploaded_at, status, 'legacy', '' FROM reports"""
            )
            db.execute("DROP TABLE reports")
            db.execute("ALTER TABLE reports_new RENAME TO reports")
            db.commit()
            db.execute("PRAGMA foreign_keys = ON")
        elif "uploaded_by" not in report_columns:
            db.execute("ALTER TABLE reports ADD COLUMN uploaded_by TEXT NOT NULL DEFAULT ''")

        db.execute("CREATE INDEX IF NOT EXISTS idx_reports_org_uploaded ON reports(organization_id, uploaded_at)")
        db.execute(
            "INSERT OR IGNORE INTO organizations (id, name, normalized_name, created_at) VALUES ('legacy', 'Legacy local data', 'legacy-local-data', ?)",
            (now_iso(),),
        )


initialize_database()


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"{PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        iterations, salt_hex, digest_hex = stored.split("$", 2)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: sqlite3.Connection, user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + SESSION_TTL).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token_digest(token), user_id, expires_at, now_iso()),
    )
    return token, expires_at


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with connect() as db:
        row = db.execute(
        """SELECT u.id, u.name, u.email, u.institution, u.organization_id, u.is_admin, o.name AS organization_name, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               JOIN organizations o ON o.id = u.organization_id WHERE s.token_hash = ?""",
            (token_digest(token),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now_iso():
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
            return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "institution": row["institution"],
        "organization_id": row["organization_id"],
        "organization_name": row["organization_name"],
        "is_admin": bool(row["is_admin"]),
    }


def active_user() -> dict[str, Any]:
    user = ACTIVE_USER.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to access this workspace.")
    return user


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def authenticate_api(request: Request, call_next):
    path = request.url.path
    public_paths = {
        "/api/health",
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    }
    if not path.startswith("/api/") or path in public_paths:
        return await call_next(request)
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"detail": "Sign in to access this workspace."}, status_code=401)
    context = ACTIVE_USER.set(user)
    try:
        return await call_next(request)
    finally:
        ACTIVE_USER.reset(context)


@app.post("/api/auth/register", status_code=201)
def register_user(payload: RegisterRequest, request: Request, response: Response) -> dict[str, Any]:
    name = re.sub(r"\s+", " ", payload.name).strip()
    email = payload.email.strip().casefold()
    institution = re.sub(r"\s+", " ", payload.institution).strip()
    workspace_name = re.sub(r"\s+", " ", payload.workspace or "").strip() or institution
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    if len(name) < 2 or len(institution) < 2:
        raise HTTPException(status_code=422, detail="Enter your name and institution.")

    user_id = str(uuid.uuid4())
    normalized_workspace = normalize_key(workspace_name)
    invite_token = payload.invitation_code.strip() if payload.invitation_code else ""
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in instead.")

        if invite_token:
            invite = db.execute(
                "SELECT * FROM organization_invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (token_digest(invite_token), now_iso()),
            ).fetchone()
            if invite is None:
                raise HTTPException(status_code=400, detail="This invitation code is invalid, expired, or already used.")
            organization_id = invite["organization_id"]
            organization = db.execute("SELECT name FROM organizations WHERE id = ?", (organization_id,)).fetchone()
            is_admin = 0
        else:
            if db.execute("SELECT 1 FROM organizations WHERE normalized_name = ?", (normalized_workspace,)).fetchone():
                raise HTTPException(status_code=409, detail="This institution already has a workspace. Ask its workspace admin for an invitation code.")
            organization_id = str(uuid.uuid4())
            organization = {"name": workspace_name}
            is_admin = 1

        if not invite_token:
            db.execute(
                "INSERT INTO organizations (id, name, normalized_name, created_at) VALUES (?, ?, ?, ?)",
                (organization_id, workspace_name, normalized_workspace, now_iso()),
            )
        db.execute(
            "INSERT INTO users (id, organization_id, name, email, institution, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, organization_id, name, email, institution, password_hash(payload.password), is_admin, now_iso()),
        )
        if invite_token:
            claimed = db.execute(
                "UPDATE organization_invites SET used_at = ?, used_by = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now_iso(), user_id, token_digest(invite_token), now_iso()),
            )
            if claimed.rowcount != 1:
                raise HTTPException(status_code=400, detail="This invitation code has already been used or has expired.")
        elif db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            db.execute("UPDATE reports SET organization_id = ?, uploaded_by = ? WHERE organization_id = 'legacy'", (organization_id, user_id))
        token, _ = create_session(db, user_id)

    set_session_cookie(response, token, request)
    return {"user": user_for_token(token)}


@app.post("/api/auth/login")
def login_user(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    email = payload.email.strip().casefold()
    with connect() as db:
        row = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or not password_matches(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        token, _ = create_session(db, row["id"])
    set_session_cookie(response, token, request)
    return {"user": user_for_token(token)}


@app.post("/api/auth/logout")
def logout_user(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"status": "signed_out"}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to access this workspace.")
    return {"user": user}


@app.post("/api/auth/change-email")
def change_email(payload: ChangeEmailRequest, request: Request) -> dict[str, Any]:
    user = active_user()
    email = payload.new_email.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    with connect() as db:
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row is None or not password_matches(payload.current_password, row["password_hash"]):
            raise HTTPException(status_code=403, detail="The current password is incorrect.")
        existing = db.execute("SELECT 1 FROM users WHERE email = ? AND id <> ?", (email, user["id"])).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        db.execute("UPDATE users SET email = ? WHERE id = ?", (email, user["id"]))
    return {"user": user_for_token(request.cookies.get(SESSION_COOKIE))}


def sync_institution_registry(db: sqlite3.Connection, organization_id: str) -> None:
    existing = db.execute(
        "SELECT name FROM institutions WHERE organization_id = ?", (organization_id,)
    ).fetchall()
    known = {normalize_key(row["name"]) for row in existing}
    sources = db.execute(
        "SELECT institution AS name, id AS user_id FROM users WHERE organization_id = ? "
        "UNION SELECT institution AS name, uploaded_by AS user_id FROM reports WHERE organization_id = ?",
        (organization_id, organization_id),
    ).fetchall()
    for source in sources:
        name = re.sub(r"\s+", " ", str(source["name"] or "")).strip()
        normalized = normalize_key(name)
        if len(name) >= 2 and normalized and normalized not in known:
            db.execute(
                "INSERT OR IGNORE INTO institutions (id, organization_id, name, normalized_name, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), organization_id, name, normalized, source["user_id"] or "", now_iso()),
            )
            known.add(normalized)


@app.get("/api/institutions")
def list_institutions() -> dict[str, Any]:
    user = active_user()
    with connect() as db:
        sync_institution_registry(db, user["organization_id"])
        rows = db.execute(
            "SELECT id, name, created_at FROM institutions WHERE organization_id = ? ORDER BY name COLLATE NOCASE",
            (user["organization_id"],),
        ).fetchall()
    return {"institutions": [dict(row) for row in rows]}


@app.post("/api/institutions", status_code=201)
def register_institution(payload: InstitutionRequest) -> dict[str, Any]:
    user = active_user()
    name = re.sub(r"\s+", " ", payload.name).strip()
    normalized = normalize_key(name)
    if len(name) < 2 or not normalized:
        raise HTTPException(status_code=422, detail="Enter a valid institution name.")
    with connect() as db:
        sync_institution_registry(db, user["organization_id"])
        row = db.execute(
            "SELECT id, name, created_at FROM institutions WHERE organization_id = ? AND normalized_name = ?",
            (user["organization_id"], normalized),
        ).fetchone()
        if row:
            raise HTTPException(status_code=409, detail="This institution is already registered in the workspace.")
        institution_id = str(uuid.uuid4())
        created_at = now_iso()
        db.execute(
            "INSERT INTO institutions (id, organization_id, name, normalized_name, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (institution_id, user["organization_id"], name, normalized, user["id"], created_at),
        )
    return {"institution": {"id": institution_id, "name": name, "created_at": created_at}}


@app.post("/api/workspace/reset")
def reset_workspace(payload: ResetWorkspaceRequest) -> dict[str, int]:
    user = active_user()
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Only the workspace administrator can reset workspace data.")
    with connect() as db:
        owner = db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if owner is None or not password_matches(payload.current_password, owner["password_hash"]):
            raise HTTPException(status_code=403, detail="The current password is incorrect.")
        report_rows = db.execute(
            "SELECT id, stored_filename FROM reports WHERE organization_id = ?", (user["organization_id"],)
        ).fetchall()
        record_count = db.execute(
            "SELECT COUNT(*) FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ?",
            (user["organization_id"],),
        ).fetchone()[0]
        db.execute(
            """DELETE FROM duplicate_decisions
               WHERE record_a IN (SELECT r.id FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ?)
                  OR record_b IN (SELECT r.id FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ?)""",
            (user["organization_id"], user["organization_id"]),
        )
        db.execute("DELETE FROM reports WHERE organization_id = ?", (user["organization_id"],))
        db.execute("DELETE FROM institutions WHERE organization_id = ?", (user["organization_id"],))
        db.execute("DELETE FROM organization_invites WHERE organization_id = ?", (user["organization_id"],))
        report_count = len(report_rows)
    for row in report_rows:
        stored_path = (UPLOAD_DIR / Path(row["stored_filename"]).name).resolve()
        if stored_path.parent == UPLOAD_DIR.resolve() and stored_path.is_file():
            stored_path.unlink()
    return {"reports_cleared": report_count, "records_cleared": record_count}


@app.post("/api/auth/invites")
def create_invitation() -> dict[str, str]:
    user = active_user()
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Only a workspace admin can create invitation codes.")
    code = secrets.token_urlsafe(12)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO organization_invites (token_hash, organization_id, created_by, expires_at) VALUES (?, ?, ?, ?)",
            (token_digest(code), user["organization_id"], user["id"], expires_at),
        )
    return {"invitation_code": code, "expires_at": expires_at}


def clean_filename(filename: str | None) -> str:
    # Keep only a basename and a conservative set of visible characters.
    basename = Path(filename or "report.csv").name.replace("\x00", "")
    return re.sub(r"[^A-Za-z0-9._ -]", "_", basename)[:180] or "report.csv"


def read_table(content: bytes, suffix: str) -> pd.DataFrame:
    try:
        if suffix == ".csv":
            frame = pd.read_csv(io.BytesIO(content), dtype=object, encoding="utf-8-sig")
        elif suffix == ".tsv":
            frame = pd.read_csv(io.BytesIO(content), sep="\t", dtype=object, encoding="utf-8-sig")
        else:
            frame = pd.read_excel(io.BytesIO(content), dtype=object, engine="openpyxl")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read this table: {exc}") from exc

    frame = frame.dropna(how="all").reset_index(drop=True)
    if frame.empty:
        raise HTTPException(status_code=422, detail="The uploaded table has no data rows.")
    if len(frame) > MAX_ROWS_PER_REPORT:
        raise HTTPException(status_code=413, detail=f"A report can contain up to {MAX_ROWS_PER_REPORT:,} rows.")

    columns: list[str] = []
    seen: dict[str, int] = {}
    for index, column in enumerate(frame.columns):
        name = re.sub(r"\s+", " ", str(column)).strip() or f"column_{index + 1}"
        count = seen.get(name.casefold(), 0) + 1
        seen[name.casefold()] = count
        columns.append(name if count == 1 else f"{name}_{count}")
    frame.columns = columns
    return frame


def json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def safe_frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def report_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "file_type": row["file_type"],
        "institution": row["institution"],
        "row_count": row["row_count"],
        "columns": json.loads(row["columns_json"]),
        "uploaded_at": row["uploaded_at"],
        "status": row["status"],
    }


def report_row_or_404(report_id: str) -> sqlite3.Row:
    user = active_user()
    with connect() as db:
        row = db.execute(
            "SELECT * FROM reports WHERE id = ? AND organization_id = ?",
            (report_id, user["organization_id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found.")
    return row


def records_frame(report_id: str | None = None, include_merged: bool = False) -> pd.DataFrame:
    user = active_user()
    clauses: list[str] = []
    clauses.append("p.organization_id = ?")
    args: list[Any] = [user["organization_id"]]
    if report_id:
        clauses.append("r.report_id = ?")
        args.append(report_id)
    if not include_merged:
        clauses.append("r.merged_into IS NULL")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        rows = db.execute(
            """SELECT r.id AS record_id, r.report_id, r.row_number, r.original_json,
                      r.cleaned_json, r.cleaned_at, r.merged_into, p.filename AS source_file,
                      p.institution, p.uploaded_at
               FROM records r JOIN reports p ON p.id = r.report_id"""
            + where
            + " ORDER BY p.uploaded_at DESC, r.row_number ASC",
            args,
        ).fetchall()
    data = []
    for row in rows:
        payload = json.loads(row["cleaned_json"] or row["original_json"])
        data.append(
            {
                **payload,
                "_record_id": row["record_id"],
                "_report_id": row["report_id"],
                "_row_number": row["row_number"],
                "_cleaned": row["cleaned_json"] is not None,
                "_cleaned_at": row["cleaned_at"],
                "_source_file": row["source_file"],
                "_institution": row["institution"],
                "_uploaded_at": row["uploaded_at"],
            }
        )
    return pd.DataFrame(data)


def normalize_key(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def field_kind(column: str) -> str:
    key = normalize_key(column)
    if any(word in key.split() for word in ("author", "creator", "contributor", "person", "editor")):
        return "person"
    if any(word in key.split() for word in ("language", "lang")):
        return "language"
    if any(word in key.split() for word in ("date", "year", "publication")):
        return "date"
    if any(word in key.split() for word in ("identifier", "isbn", "issn", "doi")):
        return "identifier"
    return "text"


LANGUAGE_MAP = {
    "en": "English", "eng": "English", "english": "English",
    "sw": "Swahili", "swa": "Swahili", "kiswahili": "Swahili", "swahili": "Swahili",
    "fr": "French", "fre": "French", "fra": "French", "french": "French",
}
SUBJECT_MAP = {
    "ai": "Artificial intelligence",
    "machine intelligence": "Artificial intelligence",
}


def clean_person(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*\.\s*", ". ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\b([A-Z])\s+([A-Z])\b", r"\1. \2.", value)
    value = re.sub(r"\b([A-Z])\s+(?=[A-Z][a-z])", r"\1. ", value)
    return value.strip()


def clean_date(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    year = re.fullmatch(r"(\d{4})\??", value)
    if year:
        return year.group(1)
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    return value


def clean_value(column: str, value: Any, rules: CleanRules) -> Any:
    value = json_value(value)
    if value is None:
        return None if rules.fix_missing_fields else ""
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r"[\u00a0\t\r\n]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text:
        return None if rules.fix_missing_fields else ""
    kind = field_kind(column)
    if kind == "person" and rules.normalize_author_names:
        return clean_person(text)
    if kind == "language":
        return LANGUAGE_MAP.get(normalize_key(text), text)
    if kind == "text" and "subject" in normalize_key(column).split() and rules.standardize_subjects:
        return SUBJECT_MAP.get(normalize_key(text), text)
    if kind == "date" and rules.format_publication_dates:
        return clean_date(text)
    if kind == "identifier":
        return re.sub(r"\s+", "", text).upper()
    return text


def normalized_rows(frame: pd.DataFrame, rules: CleanRules) -> pd.DataFrame:
    cleaned = frame.copy()
    for column in cleaned.columns:
        cleaned[column] = cleaned[column].map(lambda value: clean_value(str(column), value, rules))
    return cleaned


def row_fingerprint(row: dict[str, Any]) -> str:
    compact = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def record_pair_id(a: str, b: str) -> str:
    return hashlib.sha256("|".join(sorted((a, b))).encode("utf-8")).hexdigest()[:24]


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "Metasync Metadata API", "time": now_iso()}


@app.get("/api/dashboard")
def dashboard_summary() -> dict[str, Any]:
    organization_id = active_user()["organization_id"]
    with connect() as db:
        reports = db.execute("SELECT COUNT(*) FROM reports WHERE organization_id = ?", (organization_id,)).fetchone()[0]
        files = reports
        records = db.execute("SELECT COUNT(*) FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ? AND r.merged_into IS NULL", (organization_id,)).fetchone()[0]
        cleaned = db.execute("SELECT COUNT(*) FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ? AND r.cleaned_json IS NOT NULL AND r.merged_into IS NULL", (organization_id,)).fetchone()[0]
        merged = db.execute("SELECT COUNT(*) FROM records r JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ? AND r.merged_into IS NOT NULL", (organization_id,)).fetchone()[0]
        duplicates = db.execute(
            "SELECT COUNT(*) FROM duplicate_decisions d JOIN records r ON r.id = d.record_a JOIN reports p ON p.id = r.report_id WHERE p.organization_id = ? AND d.action = 'merge'",
            (organization_id,),
        ).fetchone()[0]
        errors = db.execute("SELECT COUNT(*) FROM reports WHERE organization_id = ? AND status = 'Failed'", (organization_id,)).fetchone()[0]
        institutions = [dict(row) for row in db.execute(
            """SELECT p.institution, COUNT(DISTINCT p.id) AS reports,
                      COUNT(CASE WHEN r.merged_into IS NULL THEN r.id END) AS records,
                      COUNT(CASE WHEN r.cleaned_json IS NOT NULL AND r.merged_into IS NULL THEN 1 END) AS cleaned_records
               FROM reports p LEFT JOIN records r ON r.report_id = p.id
               WHERE p.organization_id = ? GROUP BY p.institution COLLATE NOCASE
               ORDER BY records DESC, p.institution COLLATE NOCASE""",
            (organization_id,),
        ).fetchall()]
        registry_count = db.execute(
            "SELECT COUNT(*) FROM institutions WHERE organization_id = ?", (organization_id,)
        ).fetchone()[0]
    return {"reports": reports, "files": files, "records": records, "cleaned_records": cleaned, "merged_records": merged, "resolved_duplicates": duplicates, "errors": errors, "institutions": institutions, "registered_institutions": registry_count}


@app.post("/api/reports", status_code=201)
async def upload_report(
    file: UploadFile = File(...),
    institution: str = Form(""),
) -> dict[str, Any]:
    user = active_user()
    filename = clean_filename(file.filename)
    suffix = Path(filename).suffix.casefold()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Use a CSV, TSV, or Excel .xlsx table.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than the 25 MB upload limit.")
    source_name = re.sub(r"\s+", " ", institution or user["institution"]).strip()
    with connect() as db:
        sync_institution_registry(db, user["organization_id"])
        registered = db.execute(
            "SELECT name FROM institutions WHERE organization_id = ? AND normalized_name = ?",
            (user["organization_id"], normalize_key(source_name)),
        ).fetchone()
    if registered is None:
        raise HTTPException(status_code=422, detail="Register this institution in the workspace before uploading its report.")
    source_name = registered["name"]
    checksum = hashlib.sha256(content).hexdigest()
    with connect() as db:
        duplicate = db.execute(
            "SELECT id, filename, uploaded_at FROM reports WHERE sha256 = ? AND organization_id = ?",
            (checksum, user["organization_id"]),
        ).fetchone()
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_file",
                "message": "This exact file has already been received.",
                "existing_report_id": duplicate["id"],
                "existing_filename": duplicate["filename"],
                "uploaded_at": duplicate["uploaded_at"],
            },
        )

    frame = read_table(content, suffix)
    report_id = str(uuid.uuid4())
    stored_filename = f"{report_id}{suffix}"
    (UPLOAD_DIR / stored_filename).write_bytes(content)
    records = frame_records(frame)
    uploaded_at = now_iso()
    try:
        with connect() as db:
            db.execute(
                """INSERT INTO reports
                   (id, filename, stored_filename, sha256, file_type, institution, row_count, columns_json,
                    uploaded_at, status, organization_id, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Uploaded', ?, ?)""",
                (report_id, filename, stored_filename, checksum, suffix.lstrip("."), source_name,
                 len(records), json.dumps(list(frame.columns), ensure_ascii=False), uploaded_at,
                 user["organization_id"], user["id"]),
            )
            db.executemany(
                "INSERT INTO records (id, report_id, row_number, original_json, created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), report_id, index + 1, json.dumps(row, ensure_ascii=False), uploaded_at)
                    for index, row in enumerate(records)
                ],
            )
    except Exception:
        (UPLOAD_DIR / stored_filename).unlink(missing_ok=True)
        raise

    row = report_row_or_404(report_id)
    return {"report": report_dict(row), "message": "Table received and stored in the shared metadata repository."}


@app.get("/api/reports")
def list_reports(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    organization_id = active_user()["organization_id"]
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM reports WHERE organization_id = ? ORDER BY uploaded_at DESC LIMIT ?",
            (organization_id, limit),
        ).fetchall()
    return {"reports": [report_dict(row) for row in rows], "count": len(rows)}


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    return {"report": report_dict(report_row_or_404(report_id))}


@app.get("/api/reports/{report_id}/rows")
def report_rows(
    report_id: str,
    search: str | None = None,
    column: str | None = None,
    value: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    report_row_or_404(report_id)
    frame = records_frame(report_id)
    if search:
        needle = search.casefold()
        mask = frame.drop(columns=[c for c in frame.columns if c.startswith("_")], errors="ignore").astype("string").apply(
            lambda column_values: column_values.str.contains(needle, case=False, na=False, regex=False)
        ).any(axis=1)
        frame = frame[mask]
    if column and value is not None:
        if column not in frame.columns:
            raise HTTPException(status_code=422, detail=f"Unknown column: {column}")
        frame = frame[frame[column].astype("string").str.casefold() == value.casefold()]
    total = len(frame)
    page = frame.iloc[offset:offset + limit]
    return {"report_id": report_id, "total": total, "limit": limit, "offset": offset, "rows": safe_frame_records(page)}


@app.get("/api/records")
def union_catalog(
    query: str | None = None,
    field: str | None = None,
    value: str | None = None,
    institution: str | None = None,
    include_merged: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    frame = records_frame(include_merged=include_merged)
    if institution:
        frame = frame[frame["_institution"].astype("string").str.casefold() == institution.casefold()]
    data_columns = [column for column in frame.columns if not column.startswith("_")]
    if field:
        if field not in data_columns:
            raise HTTPException(status_code=422, detail=f"Unknown field: {field}")
        if value is not None:
            frame = frame[frame[field].astype("string").str.casefold() == value.casefold()]
        elif query:
            frame = frame[frame[field].astype("string").str.contains(query, case=False, na=False, regex=False)]
    elif query:
        mask = frame[data_columns].astype("string").apply(
            lambda column_values: column_values.str.contains(query, case=False, na=False, regex=False)
        ).any(axis=1)
        frame = frame[mask]
    total = len(frame)
    page = frame.iloc[offset:offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": safe_frame_records(page),
        "fields": data_columns,
    }


@app.post("/api/reports/{report_id}/clean")
def clean_report(report_id: str, rules: CleanRules = Body(default=CleanRules())) -> dict[str, Any]:
    report = report_row_or_404(report_id)
    with connect() as db:
        rows = db.execute(
            "SELECT id, row_number, original_json, merged_into, auto_merged FROM records WHERE report_id = ? ORDER BY row_number",
            (report_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="This report has no rows to clean.")
        source = pd.DataFrame([json.loads(row["original_json"]) for row in rows])
        cleaned = normalized_rows(source, rules)
        normalized = frame_records(cleaned)
        first_by_fingerprint: dict[str, str] = {}
        exact_duplicates = 0
        # Re-running cleanup can turn the earlier automatic duplicate flags off;
        # manually merged records (auto_merged=0) remain preserved.
        db.execute(
            "UPDATE records SET merged_into = NULL, auto_merged = 0 WHERE report_id = ? AND auto_merged = 1",
            (report_id,),
        )
        for row, record in zip(rows, normalized):
            fingerprint = row_fingerprint(record)
            prior_id = first_by_fingerprint.get(fingerprint)
            if rules.remove_exact_duplicates and prior_id and (row["merged_into"] is None or row["auto_merged"]):
                exact_duplicates += 1
                db.execute(
                    "UPDATE records SET cleaned_json = ?, cleaned_at = ?, merged_into = ?, auto_merged = 1 WHERE id = ?",
                    (json.dumps(record, ensure_ascii=False), now_iso(), prior_id, row["id"]),
                )
            else:
                if row["merged_into"] is None or row["auto_merged"]:
                    first_by_fingerprint.setdefault(fingerprint, row["id"])
                if row["merged_into"] is not None and not row["auto_merged"]:
                    db.execute(
                        "UPDATE records SET cleaned_json = ?, cleaned_at = ? WHERE id = ?",
                        (json.dumps(record, ensure_ascii=False), now_iso(), row["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE records SET cleaned_json = ?, cleaned_at = ?, merged_into = NULL, auto_merged = 0 WHERE id = ?",
                        (json.dumps(record, ensure_ascii=False), now_iso(), row["id"]),
                    )
        db.execute("UPDATE reports SET status = 'Cleaned' WHERE id = ?", (report_id,))
    return {
        "report_id": report_id,
        "filename": report["filename"],
        "rows_processed": len(normalized),
        "exact_duplicate_rows": exact_duplicates,
        "rules_applied": rules.model_dump(),
        "cleaned_columns": json.loads(report["columns_json"]),
        "message": "Original values were preserved; cleaned values are stored alongside them.",
    }


def similarity(a: Any, b: Any) -> float:
    left, right = normalize_key(a), normalize_key(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def metadata_columns(columns: list[str]) -> tuple[str | None, str | None, str | None]:
    def first_match(words: tuple[str, ...]) -> str | None:
        for column in columns:
            key = normalize_key(column).split()
            if any(word in key for word in words):
                return column
        return None
    title = first_match(("title", "name"))
    creator = first_match(("author", "creator", "contributor"))
    date = first_match(("year", "date", "publication"))
    return title, creator, date


def duplicate_pairs(frame: pd.DataFrame, threshold: float, limit: int) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    data_columns = [column for column in frame.columns if not column.startswith("_")]
    title_col, creator_col, date_col = metadata_columns(data_columns)
    # For metadata without a title column, exact row equality is still useful.
    if not title_col:
        title_col = data_columns[0] if data_columns else None
    if not title_col:
        return []

    work = frame.copy().reset_index(drop=True)
    work["_title_key"] = work[title_col].map(normalize_key)
    work = work[work["_title_key"] != ""].reset_index(drop=True)
    work["_creator_key"] = work[creator_col].map(normalize_key) if creator_col else ""
    work["_date_key"] = work[date_col].astype("string").str.extract(r"(\d{4})", expand=False).fillna("") if date_col else ""
    # Block on the opening title tokens so a consortium catalog does not need
    # an all-pairs comparison across every record.
    def title_block(title: str) -> str:
        tokens = title.split()
        return f"{tokens[0][:3]} {tokens[1][:2] if len(tokens) > 1 else ''}" if tokens else ""
    work["_block"] = work["_title_key"].map(title_block)
    pairs: list[dict[str, Any]] = []
    with connect() as db:
        decisions = {row["pair_id"]: row["action"] for row in db.execute("SELECT pair_id, action FROM duplicate_decisions")}
    for _, block in work.groupby("_block", dropna=False):
        indices = list(block.index)
        if len(indices) < 2:
            continue
        for pos, i in enumerate(indices):
            for j in indices[pos + 1:]:
                left, right = work.loc[i], work.loc[j]
                if left["_report_id"] == right["_report_id"] and left["_row_number"] == right["_row_number"]:
                    continue
                title_score = similarity(left["_title_key"], right["_title_key"])
                creator_score = similarity(left["_creator_key"], right["_creator_key"]) if creator_col else title_score
                if date_col and left["_date_key"] and right["_date_key"] and left["_date_key"] != right["_date_key"]:
                    title_score *= 0.92
                score = round(100 * (0.78 * title_score + 0.22 * creator_score))
                if title_score == 1.0 and (not creator_col or creator_score == 1.0):
                    score = 100
                if score < round(threshold * 100):
                    continue
                id_a, id_b = str(left["_record_id"]), str(right["_record_id"])
                pair_id = record_pair_id(id_a, id_b)
                status = decisions.get(pair_id, "pending")
                if status != "pending":
                    continue
                pairs.append({
                    "pair_id": pair_id,
                    "score": score,
                    "status": status,
                    "record_a": {
                        "id": id_a, "report_id": left["_report_id"], "source_file": left["_source_file"],
                        "institution": left["_institution"], "row": {key: left[key] for key in data_columns},
                    },
                    "record_b": {
                        "id": id_b, "report_id": right["_report_id"], "source_file": right["_source_file"],
                        "institution": right["_institution"], "row": {key: right[key] for key in data_columns},
                    },
                })
    pairs.sort(key=lambda pair: pair["score"], reverse=True)
    return pairs[:limit]


@app.get("/api/duplicates")
def find_duplicates(
    threshold: float = Query(0.82, ge=0.5, le=1.0),
    report_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    if report_id:
        report_row_or_404(report_id)
    frame = records_frame(report_id=report_id)
    pairs = duplicate_pairs(frame, threshold, limit)
    return {"threshold": threshold, "count": len(pairs), "pairs": pairs}


@app.post("/api/duplicates/{pair_id}/resolve")
def resolve_duplicate(pair_id: str, request: ResolveDuplicateRequest) -> dict[str, Any]:
    frame = records_frame(include_merged=True)
    candidates = duplicate_pairs(frame, 0.5, 5000)
    pair = next((item for item in candidates if item["pair_id"] == pair_id), None)
    if pair is None:
        # A pair may have disappeared from the current candidate scan after a refresh.
        with connect() as db:
            existing = db.execute("SELECT pair_id FROM duplicate_decisions WHERE pair_id = ?", (pair_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="This pair has already been resolved.")
        raise HTTPException(status_code=404, detail="Duplicate pair not found.")
    id_a = pair["record_a"]["id"]
    id_b = pair["record_b"]["id"]
    if request.action == "merge":
        with connect() as db:
            rows = db.execute("SELECT id, cleaned_json, original_json FROM records WHERE id IN (?, ?)", (id_a, id_b)).fetchall()
            by_id = {row["id"]: row for row in rows}
            a = by_id[id_a]
            b = by_id[id_b]
            canonical = json.loads(a["cleaned_json"] or a["original_json"])
            other = json.loads(b["cleaned_json"] or b["original_json"])
            for key, value in other.items():
                current = canonical.get(key)
                if current in (None, "") and value not in (None, ""):
                    canonical[key] = value
            db.execute("UPDATE records SET cleaned_json = ? WHERE id = ?", (json.dumps(canonical, ensure_ascii=False), id_a))
            db.execute("UPDATE records SET merged_into = ? WHERE id = ?", (id_a, id_b))
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO duplicate_decisions (pair_id, record_a, record_b, action, decided_at) VALUES (?, ?, ?, ?, ?)",
            (pair_id, id_a, id_b, request.action, now_iso()),
        )
    return {"pair_id": pair_id, "action": request.action, "canonical_record_id": id_a if request.action == "merge" else None}


def table_export(frame: pd.DataFrame, format_name: str) -> tuple[bytes, str, str]:
    user_columns = [column for column in frame.columns if not column.startswith("_")]
    export_frame = frame[user_columns].copy()
    if format_name == "csv":
        return export_frame.to_csv(index=False).encode("utf-8-sig"), "text/csv; charset=utf-8", "metasync-union-catalog.csv"
    if format_name == "json":
        payload = json.dumps(export_frame.to_dict(orient="records"), ensure_ascii=False, indent=2)
        return payload.encode("utf-8"), "application/json; charset=utf-8", "metasync-union-catalog.json"
    if format_name == "marc":
        try:
            from pymarc import Field, Indicators, MARCWriter, Record, Subfield
        except ImportError as exc:
            raise HTTPException(status_code=501, detail="Install PyMARC to create MARC21 exports.") from exc
        stream = io.BytesIO()
        writer = MARCWriter(stream)
        title_col, creator_col, date_col = metadata_columns(user_columns)
        for index, row in export_frame.iterrows():
            record = Record(force_utf8=True)
            record.add_field(Field(tag="001", data=str(index + 1)))
            if title_col and row.get(title_col):
                record.add_field(Field(tag="245", indicators=Indicators("1", "0"), subfields=[Subfield(code="a", value=str(row[title_col]))]))
            if creator_col and row.get(creator_col):
                record.add_field(Field(tag="100", indicators=Indicators("1", " "), subfields=[Subfield(code="a", value=str(row[creator_col]))]))
            if date_col and row.get(date_col):
                record.add_field(Field(tag="264", indicators=Indicators(" ", "1"), subfields=[Subfield(code="c", value=str(row[date_col]))]))
            for column in user_columns:
                value = row.get(column)
                if value in (None, "") or column in {title_col, creator_col, date_col}:
                    continue
                tag = "650" if "subject" in normalize_key(column).split() else "500"
                record.add_field(Field(tag=tag, indicators=Indicators(" ", " "), subfields=[Subfield(code="a", value=f"{column}: {value}")]))
            writer.write(record)
        writer.close(close_fh=False)
        return stream.getvalue(), "application/marc; charset=utf-8", "metasync-union-catalog.mrc"
    raise HTTPException(status_code=422, detail="Choose csv, json, or marc as the export format.")


@app.get("/api/export")
def export_catalog(
    format: str = Query("csv", pattern="^(csv|json|marc)$"),
    report_id: str | None = None,
    record_id: str | None = None,
) -> Response:
    if report_id:
        report_row_or_404(report_id)
    frame = records_frame(report_id=report_id)
    if record_id:
        if "_record_id" not in frame.columns or record_id not in set(frame["_record_id"].astype(str)):
            raise HTTPException(status_code=404, detail="Record not found.")
        frame = frame[frame["_record_id"].astype(str) == record_id]
    if frame.empty:
        raise HTTPException(status_code=404, detail="There are no records to export yet.")
    data, media_type, filename = table_export(frame, format)
    if report_id:
        filename = f"metasync-report-{report_id[:8]}.{format if format != 'marc' else 'mrc'}"
    elif record_id:
        filename = f"metasync-record-{record_id[:8]}.{format if format != 'marc' else 'mrc'}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/styles.css", include_in_schema=False)
def stylesheet() -> FileResponse:
    return FileResponse(BASE_DIR / "styles.css", media_type="text/css")


app.mount("/assets", StaticFiles(directory=BASE_DIR / "assets"), name="assets")
