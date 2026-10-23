#!/usr/bin/env python3
"""
EWAA — Every Word's An Acronym
Created by TayTanium Software.

API server only:
    http://127.0.0.1:7863

Production frontend:
    https://ewaa.taytanium.com

Production API:
    https://ewaa-api.taytanium.com

Dependencies:
    pip install flask waitress

Run:
    export EWAA_ADMIN_PASSWORD='a-long-random-password'
    python server.py

Import or update dictionary entries:
    python server.py import words.json

Export dictionary:
    python server.py export words.json

Generate a password hash:
    python server.py hash-password

Environment variables:
    EWAA_ADMIN_PASSWORD
        Admin password. Required unless EWAA_ADMIN_PASSWORD_HASH is set.

    EWAA_ADMIN_PASSWORD_HASH
        Werkzeug password hash. Preferred over a plaintext environment
        variable. Generate using the hash-password command.

    EWAA_DB_PATH
        Database location. Default: ./ewaa.sqlite3 next to this script.

    EWAA_ALLOWED_ORIGINS
        Comma-separated frontend origins. Defaults include production
        and local development origins.

    EWAA_TRUST_CLOUDFLARE_HEADERS
        Set to "1" only when requests arrive through a trusted local
        Cloudflare Tunnel. Enables CF-Connecting-IP for rate limits.

        Do not enable this if untrusted clients can reach the API
        directly or through another proxy that preserves spoofed headers.
"""

import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request
from waitress import serve
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATABASE_PATH = Path(
    os.environ.get("EWAA_DB_PATH", str(BASE_DIR / "ewaa.sqlite3"))
).expanduser().resolve()

BAD_WORDS_PATH = BASE_DIR.parent / "bad_words.txt"

BAD_WORDS = frozenset(
    line.strip().lower()
    for line in BAD_WORDS_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
)

HOST = "127.0.0.1"
PORT = 7863

SESSION_SECONDS = 7 * 24 * 60 * 60
MAX_WORD_LENGTH = 40
MAX_REPORT_LENGTH = 2000
MAX_IMPORT_BYTES = 50 * 1024 * 1024

WORD_RE = re.compile(r"^[a-z]{1,40}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

DEFAULT_ORIGINS = ",".join(
    [
        "https://ewaa.taytanium.com",
        "http://127.0.0.1:7862",
        "http://localhost:7862",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
)

LOGGER = logging.getLogger("ewaa")


# ============================================================
# STARTER DICTIONARY
# ============================================================

# These are illustrative, editable backronyms—not historical
# etymologies or a complete English dictionary.
#
# Expansion words do NOT need to exist in the dictionary yet.
# Clicking an absent expansion word opens the missing-word page.

STARTER_WORDS = []


# ============================================================
# VALIDATION
# ============================================================

class ValidationError(ValueError):
    pass


def normalize_word(value):
    if not isinstance(value, str):
        raise ValidationError("Word must be a string.")

    word = value.strip().lower()

    if not WORD_RE.fullmatch(word):
        raise ValidationError(
            "Enter one word using 1–40 English letters."
        )

    return word


def validate_entry(entry):
    if not isinstance(entry, dict):
        raise ValidationError("Each dictionary entry must be an object.")

    word = normalize_word(entry.get("word"))

    expansion = entry.get("expansion")

    if not isinstance(expansion, list):
        raise ValidationError(
            f"{word}: expansion must be an array of words."
        )

    if len(expansion) != len(word):
        raise ValidationError(
            f"{word}: expected {len(word)} expansion words, "
            f"received {len(expansion)}."
        )

    cleaned_expansion = []

    for index, part in enumerate(expansion):
        try:
            part = normalize_word(part)
        except ValidationError as exc:
            raise ValidationError(
                f"{word}: invalid expansion word at position "
                f"{index + 1}: {exc}"
            ) from exc

        if part[0] != word[index]:
            raise ValidationError(
                f"{word}: expansion word {index + 1} must start "
                f"with '{word[index]}', not '{part[0]}'."
            )

        cleaned_expansion.append(part)

    return {
        "word": word,
        "expansion": cleaned_expansion,
    }


# ============================================================
# DATABASE
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL UNIQUE,
    expansion TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('missing', 'acronym')),
    word TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS reports_filter_idx
ON reports (status, kind, created_at DESC);

CREATE INDEX IF NOT EXISTS reports_word_idx
ON reports (word);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_missing_report
ON reports (word)
WHERE kind = 'missing' AND status = 'open';

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    credential_fingerprint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS session_expiry_idx
ON admin_sessions (expires_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT NOT NULL,
    subject TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bucket, subject, window_start)
);

CREATE INDEX IF NOT EXISTS rate_limit_expiry_idx
ON rate_limits (expires_at);
"""


def open_database():
    connection = sqlite3.connect(
        str(DATABASE_PATH),
        timeout=15,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def upsert_word(connection, entry, now):
    connection.execute(
        """
        INSERT INTO words (
            word, expansion, created_at, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(word) DO UPDATE SET
            expansion = excluded.expansion,
            updated_at = excluded.updated_at
        """,
        (
            entry["word"],
            json.dumps(entry["expansion"], ensure_ascii=False),
            now,
            now,
        ),
    )




def initialize_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = open_database()

    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")

        connection.execute(
            """
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('rate_limit_secret', ?)
            """,
            (secrets.token_hex(32),),
        )

        seeded = connection.execute(
            "SELECT value FROM settings WHERE key = 'starter_seeded'"
        ).fetchone()

        if seeded is None:
            now = int(time.time())

            count = connection.execute(
                "SELECT COUNT(*) AS count FROM words"
            ).fetchone()["count"]

            if count == 0:
                for raw_entry in STARTER_WORDS:
                    entry = validate_entry(raw_entry)
                    upsert_word(connection, entry, now)

            connection.execute(
                """
                INSERT INTO settings (key, value)
                VALUES ('starter_seeded', '1')
                """
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    # The database contains reports and active session metadata.
    # Restrict filesystem access on POSIX systems.
    if os.name == "posix":
        os.chmod(DATABASE_PATH, 0o600)


def get_db():
    if "db" not in g:
        g.db = open_database()

    return g.db


def iso_timestamp(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def serialize_word(row):
    return {
        "word": row["word"],
        "expansion": json.loads(row["expansion"]),
    }


def serialize_report(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "word": row["word"],
        "details": row["details"],
        "status": row["status"],
        "created_at": iso_timestamp(row["created_at"]),
        "updated_at": iso_timestamp(row["updated_at"]),
    }


# ============================================================
# API ERRORS
# ============================================================

class APIError(Exception):
    def __init__(self, message, status=400, headers=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.headers = headers or {}


def json_body():
    if not request.is_json:
        raise APIError(
            "Send a JSON request body with Content-Type: application/json.",
            415,
        )

    data = request.get_json()

    if not isinstance(data, dict):
        raise APIError("The JSON body must be an object.")

    return data


def query_integer(name, default, minimum, maximum):
    raw = request.args.get(name)

    if raw is None:
        return default

    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise APIError(f"{name} must be an integer.")

    if not minimum <= value <= maximum:
        raise APIError(
            f"{name} must be between {minimum} and {maximum}."
        )

    return value


# ============================================================
# APPLICATION FACTORY
# ============================================================

def create_app():
    initialize_database()

    configured_hash = os.environ.get(
        "EWAA_ADMIN_PASSWORD_HASH", ""
    ).strip()

    configured_password = os.environ.get(
        "EWAA_ADMIN_PASSWORD", ""
    )

    if configured_hash:
        password_hash = configured_hash
        credential_source = "hash:" + configured_hash

        # Check the hash format at startup rather than failing at login.
        try:
            check_password_hash(password_hash, "startup-format-check")
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "EWAA_ADMIN_PASSWORD_HASH is invalid. Generate one "
                "using: python server.py hash-password"
            ) from exc

        if not password_hash.startswith(("scrypt:", "pbkdf2:")):
            raise RuntimeError(
                "Use a modern Werkzeug scrypt or PBKDF2 password hash."
            )
    else:
        if len(configured_password) < 16:
            raise RuntimeError(
                "Set EWAA_ADMIN_PASSWORD to at least 16 characters, "
                "or set EWAA_ADMIN_PASSWORD_HASH."
            )

        password_hash = generate_password_hash(configured_password)
        credential_source = "password:" + configured_password

    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in os.environ.get(
            "EWAA_ALLOWED_ORIGINS", DEFAULT_ORIGINS
        ).split(",")
        if origin.strip()
    }

    if "*" in allowed_origins:
        raise RuntimeError(
            "EWAA_ALLOWED_ORIGINS must list explicit origins, not '*'."
        )

    trust_cloudflare = (
        os.environ.get("EWAA_TRUST_CLOUDFLARE_HEADERS", "0") == "1"
    )

    connection = open_database()

    try:
        rate_secret = connection.execute(
            """
            SELECT value FROM settings
            WHERE key = 'rate_limit_secret'
            """
        ).fetchone()["value"].encode("utf-8")
    finally:
        connection.close()

    # Stable across restarts when the configured credential stays the
    # same. Changing the password/hash invalidates previous sessions.
    credential_fingerprint = hmac.new(
        rate_secret,
        credential_source.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    @app.teardown_appcontext
    def close_database(_exception):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    # --------------------------------------------------------
    # CORS + RESPONSE HEADERS
    # --------------------------------------------------------

    @app.before_request
    def check_origin():
        origin = request.headers.get("Origin")

        if origin and origin not in allowed_origins:
            raise APIError("This origin is not allowed.", 403)

        if request.method == "OPTIONS":
            return "", 204

    @app.after_request
    def response_headers(response):
        origin = request.headers.get("Origin")

        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization"
            )
            response.headers["Access-Control-Expose-Headers"] = (
                "Retry-After"
            )
            response.headers["Access-Control-Max-Age"] = "600"

        response.vary.add("Origin")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"

        return response

    # --------------------------------------------------------
    # ERROR HANDLERS
    # --------------------------------------------------------

    @app.errorhandler(APIError)
    def api_error(error):
        response = jsonify({"error": error.message})
        response.status_code = error.status

        for key, value in error.headers.items():
            response.headers[key] = value

        return response

    @app.errorhandler(ValidationError)
    def validation_error(error):
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {
            400: "Invalid request or malformed JSON.",
            404: "API endpoint not found.",
            405: "This HTTP method is not allowed.",
            413: "Request body is too large.",
            415: "Unsupported content type.",
        }

        # Preserve framework headers such as Allow for a 405 response.
        response = error.get_response()
        response.data = app.json.dumps(
            {"error": messages.get(error.code, error.description)}
        )
        response.content_type = "application/json"

        return response

    @app.errorhandler(Exception)
    def unexpected_error(error):
        LOGGER.exception("Unhandled API error")
        return jsonify(
            {"error": "An internal server error occurred."}
        ), 500

    # --------------------------------------------------------
    # RATE LIMITS
    # --------------------------------------------------------

    def client_address():
        remote = request.remote_addr or "unknown"

        if trust_cloudflare:
            try:
                local_proxy = ipaddress.ip_address(remote).is_loopback
            except ValueError:
                local_proxy = False

            forwarded = request.headers.get("CF-Connecting-IP")

            if local_proxy and forwarded:
                try:
                    return str(ipaddress.ip_address(forwarded.strip()))
                except ValueError:
                    pass

        return remote

    def rate_limit(bucket, limit, period, global_limit=False):
        now = int(time.time())
        window_start = now - (now % period)
        expires_at = window_start + period

        identity = "global" if global_limit else client_address()

        subject = hmac.new(
            rate_secret,
            identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        connection = get_db()

        # SQLite serializes this write transaction, so concurrent
        # requests cannot bypass a read-then-increment check.
        with connection:
            connection.execute(
                "DELETE FROM rate_limits WHERE expires_at <= ?",
                (now,),
            )

            connection.execute(
                """
                INSERT INTO rate_limits (
                    bucket, subject, window_start, count, expires_at
                )
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(bucket, subject, window_start)
                DO UPDATE SET count = count + 1
                """,
                (bucket, subject, window_start, expires_at),
            )

            count = connection.execute(
                """
                SELECT count FROM rate_limits
                WHERE bucket = ? AND subject = ? AND window_start = ?
                """,
                (bucket, subject, window_start),
            ).fetchone()["count"]

        if count > limit:
            retry_after = max(1, expires_at - now)
            raise APIError(
                "Too many requests. Please try again later.",
                429,
                {"Retry-After": str(retry_after)},
            )

    # --------------------------------------------------------
    # ADMIN AUTHENTICATION
    # --------------------------------------------------------

    def require_admin(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            authorization = request.headers.get("Authorization", "")
            parts = authorization.split()

            if (
                len(parts) != 2
                or parts[0].lower() != "bearer"
                or not TOKEN_RE.fullmatch(parts[1])
            ):
                raise APIError("Please sign in to continue.", 401)

            token_hash = hashlib.sha256(
                parts[1].encode("ascii")
            ).hexdigest()

            session = get_db().execute(
                """
                SELECT token_hash, credential_fingerprint, expires_at
                FROM admin_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()

            if (
                session is None
                or session["expires_at"] <= int(time.time())
                or not hmac.compare_digest(
                    session["credential_fingerprint"],
                    credential_fingerprint,
                )
            ):
                raise APIError(
                    "Your session has expired. Please sign in again.",
                    401,
                )

            g.admin_token_hash = token_hash

            return view(*args, **kwargs)

        return wrapped

    # --------------------------------------------------------
    # PUBLIC ENDPOINTS
    # --------------------------------------------------------

    @app.get("/api/health")
    def health():
        get_db().execute("SELECT 1").fetchone()
        return jsonify(
            {
                "ok": True,
                "project": "EWAA",
            }
        )

    @app.get("/api/stats")
    def stats():
        count = get_db().execute(
            "SELECT COUNT(*) AS count FROM words"
        ).fetchone()["count"]

        return jsonify({"words": count})

    @app.get("/api/words/<word>")
    def get_word(word):
        word = normalize_word(word)

        row = get_db().execute(
            """
            SELECT word, expansion
            FROM words
            WHERE word = ?
            """,
            (word,),
        ).fetchone()

        if row is None:
            raise APIError(
                "This word is not in the dictionary yet.",
                404,
            )

        data = serialize_word(row)
        parts = sorted(set(data["expansion"]))
        expansions = {}

        if parts:
            placeholders = ", ".join("?" for _ in parts)
            rows = get_db().execute(
                f"""
                SELECT word, expansion
                FROM words
                WHERE word IN ({placeholders})
                """,
                parts,
            ).fetchall()

            expansions = {
                part_row["word"]: json.loads(part_row["expansion"])
                for part_row in rows
                if part_row["word"] not in BAD_WORDS
            }

        data["expansions"] = expansions

        return jsonify(data)

    @app.get("/api/random")
    def random_word():
        connection = get_db()

        # A random offset gives every stored word equal probability
        # without sorting the whole dictionary by RANDOM().
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM words"
        ).fetchone()["count"]

        if count == 0:
            raise APIError("The dictionary is currently empty.", 404)

        offset = secrets.randbelow(count)

        row = connection.execute(
            """
            SELECT word FROM words
            ORDER BY id
            LIMIT 1 OFFSET ?
            """,
            (offset,),
        ).fetchone()

        if row is None:
            raise APIError(
                "The dictionary changed. Please try again.",
                503,
            )

        return jsonify({"word": row["word"]})

    @app.post("/api/reports")
    def create_report():
        rate_limit("reports-minute", limit=5, period=60)
        rate_limit("reports-hour", limit=30, period=3600)

        data = json_body()

        kind = data.get("kind")
        if kind not in ("missing", "acronym"):
            raise APIError(
                "Report kind must be 'missing' or 'acronym'."
            )

        word = normalize_word(data.get("word"))

        details = data.get("details", "")
        if not isinstance(details, str):
            raise APIError("Report details must be text.")

        details = details.strip()

        if len(details) > MAX_REPORT_LENGTH:
            raise APIError(
                "Report details must be 2,000 characters or fewer."
            )

        if kind == "acronym" and len(details) < 10:
            raise APIError(
                "Please describe the issue using at least 10 characters."
            )

        if kind == "missing":
            details = ""

        connection = get_db()
        now = int(time.time())

        connection.execute("BEGIN IMMEDIATE")

        try:
            exists = connection.execute(
                "SELECT 1 FROM words WHERE word = ?",
                (word,),
            ).fetchone() is not None

            if kind == "missing" and exists:
                raise APIError(
                    "This word already has an acronym. "
                    "Reload its page to view it.",
                    409,
                )

            if kind == "acronym" and not exists:
                raise APIError(
                    "This word has no acronym yet. "
                    "Please report it as a missing word instead.",
                    404,
                )

            if kind == "missing":
                duplicate = connection.execute(
                    """
                    SELECT id FROM reports
                    WHERE kind = 'missing'
                      AND word = ?
                      AND status = 'open'
                    LIMIT 1
                    """,
                    (word,),
                ).fetchone()
            else:
                duplicate = connection.execute(
                    """
                    SELECT id FROM reports
                    WHERE kind = 'acronym'
                      AND word = ?
                      AND details = ?
                      AND status = 'open'
                    LIMIT 1
                    """,
                    (word, details),
                ).fetchone()

            if duplicate is not None:
                connection.commit()

                message = (
                    "This missing word has already been reported. Thanks!"
                    if kind == "missing"
                    else "This issue has already been reported. Thanks!"
                )

                return jsonify(
                    {
                        "ok": True,
                        "duplicate": True,
                        "message": message,
                    }
                )

            cursor = connection.execute(
                """
                INSERT INTO reports (
                    kind, word, details, status, created_at, updated_at
                )
                VALUES (?, ?, ?, 'open', ?, ?)
                """,
                (kind, word, details, now, now),
            )

            report_id = cursor.lastrowid
            connection.commit()

        except Exception:
            connection.rollback()
            raise

        message = (
            "Thanks! The missing word has been reported."
            if kind == "missing"
            else "Thanks! Your acronym issue has been reported."
        )

        return jsonify(
            {
                "ok": True,
                "id": report_id,
                "duplicate": False,
                "message": message,
            }
        ), 201

    # --------------------------------------------------------
    # ADMIN LOGIN / LOGOUT
    # --------------------------------------------------------

    @app.post("/api/admin/login")
    def admin_login():
        rate_limit("login-ip", limit=10, period=15 * 60)

        # Secondary cap limits expensive password hashing if someone
        # attempts to bypass per-IP limits using many addresses.
        rate_limit(
            "login-global",
            limit=100,
            period=15 * 60,
            global_limit=True,
        )

        data = json_body()
        password = data.get("password")

        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise APIError("Invalid password.", 401)

        if not check_password_hash(password_hash, password):
            raise APIError("Invalid password.", 401)

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(
            token.encode("ascii")
        ).hexdigest()

        now = int(time.time())
        expires_at = now + SESSION_SECONDS

        connection = get_db()

        with connection:
            connection.execute(
                """
                DELETE FROM admin_sessions
                WHERE expires_at <= ?
                   OR credential_fingerprint != ?
                """,
                (now, credential_fingerprint),
            )

            connection.execute(
                """
                INSERT INTO admin_sessions (
                    token_hash,
                    credential_fingerprint,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    token_hash,
                    credential_fingerprint,
                    now,
                    expires_at,
                ),
            )

        return jsonify(
            {
                "token": token,
                "expires_at": iso_timestamp(expires_at),
            }
        )

    @app.post("/api/admin/logout")
    @require_admin
    def admin_logout():
        connection = get_db()

        with connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?",
                (g.admin_token_hash,),
            )

        return jsonify({"ok": True})

    # --------------------------------------------------------
    # ADMIN REPORT MANAGEMENT
    # --------------------------------------------------------

    @app.get("/api/admin/issues")
    @require_admin
    def list_issues():
        status = request.args.get("status", "open")
        kind = request.args.get("kind", "all")

        if status not in ("open", "resolved", "all"):
            raise APIError("Invalid status filter.")

        if kind not in ("missing", "acronym", "all"):
            raise APIError("Invalid report type filter.")

        limit = query_integer("limit", 30, 1, 100)
        offset = query_integer("offset", 0, 0, 10_000_000)

        conditions = []
        parameters = []

        if status != "all":
            conditions.append("status = ?")
            parameters.append(status)

        if kind != "all":
            conditions.append("kind = ?")
            parameters.append(kind)

        where = (
            " WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )

        connection = get_db()

        # Only fixed SQL fragments are composed here.
        # All user-provided values remain bound parameters.
        total = connection.execute(
            "SELECT COUNT(*) AS count FROM reports" + where,
            parameters,
        ).fetchone()["count"]

        rows = connection.execute(
            """
            SELECT id, kind, word, details, status, created_at, updated_at
            FROM reports
            """
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            parameters + [limit, offset],
        ).fetchall()

        return jsonify(
            {
                "items": [serialize_report(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.patch("/api/admin/issues/<int:issue_id>")
    @require_admin
    def update_issue(issue_id):
        data = json_body()
        status = data.get("status")

        if status not in ("open", "resolved"):
            raise APIError("Status must be 'open' or 'resolved'.")

        connection = get_db()
        connection.execute("BEGIN IMMEDIATE")

        try:
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?",
                (issue_id,),
            ).fetchone()

            if row is None:
                raise APIError("Report not found.", 404)

            if row["kind"] == "missing" and status == "open":
                other = connection.execute(
                    """
                    SELECT id FROM reports
                    WHERE kind = 'missing'
                      AND word = ?
                      AND status = 'open'
                      AND id != ?
                    LIMIT 1
                    """,
                    (row["word"], issue_id),
                ).fetchone()

                if other is not None:
                    raise APIError(
                        "Another open missing-word report already "
                        "exists for this word.",
                        409,
                    )

            connection.execute(
                """
                UPDATE reports
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, int(time.time()), issue_id),
            )

            updated = connection.execute(
                "SELECT * FROM reports WHERE id = ?",
                (issue_id,),
            ).fetchone()

            connection.commit()

        except Exception:
            connection.rollback()
            raise

        return jsonify(
            {
                "ok": True,
                "issue": serialize_report(updated),
            }
        )

    return app


# ============================================================
# DICTIONARY IMPORT / EXPORT
# ============================================================

def import_dictionary(filename):
    path = Path(filename).expanduser().resolve()

    # Read at most the configured size, rather than trusting file metadata.
    with path.open("rb") as handle:
        raw = handle.read(MAX_IMPORT_BYTES + 1)

    if len(raw) > MAX_IMPORT_BYTES:
        raise ValidationError("Import file must be 50 MB or smaller.")

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid JSON file: {exc}") from exc

    # Accept either an array or {"words": [...]}.
    entries = payload.get("words") if isinstance(payload, dict) else payload

    if not isinstance(entries, list):
        raise ValidationError(
            "The JSON file must contain an array of entries, "
            'or an object with a "words" array.'
        )

    validated = []
    seen = set()

    # Validate the entire file before writing anything.
    for index, raw_entry in enumerate(entries, start=1):
        try:
            entry = validate_entry(raw_entry)
        except ValidationError as exc:
            raise ValidationError(f"Entry {index}: {exc}") from exc

        if entry["word"] in seen:
            raise ValidationError(
                f"Duplicate word in import: {entry['word']}"
            )

        seen.add(entry["word"])
        validated.append(entry)

    initialize_database()
    connection = open_database()

    added = 0
    updated = 0
    resolved = 0
    now = int(time.time())

    try:
        connection.execute("BEGIN IMMEDIATE")

        for entry in validated:
            exists = connection.execute(
                "SELECT 1 FROM words WHERE word = ?",
                (entry["word"],),
            ).fetchone()

            if exists:
                updated += 1
            else:
                added += 1

            upsert_word(connection, entry, now)

            # A newly available word no longer needs a missing-word
            # report. Acronym-quality reports stay open for review.
            cursor = connection.execute(
                """
                UPDATE reports
                SET status = 'resolved', updated_at = ?
                WHERE word = ?
                  AND kind = 'missing'
                  AND status = 'open'
                """,
                (now, entry["word"]),
            )

            resolved += cursor.rowcount

        connection.commit()

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        f"Import complete: {added} added, {updated} updated, "
        f"{resolved} missing-word reports resolved."
    )


def export_dictionary(filename):
    initialize_database()
    connection = open_database()

    try:
        rows = connection.execute(
            """
            SELECT word, expansion
            FROM words
            ORDER BY word
            """
        ).fetchall()

        entries = [serialize_word(row) for row in rows]
    finally:
        connection.close()

    path = Path(filename).expanduser().resolve()

    if path == DATABASE_PATH:
        raise ValidationError(
            "The export path cannot be the database path."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Exported {len(entries)} words to {path}")


def print_password_hash():
    password = getpass.getpass("New admin password: ")
    confirmation = getpass.getpass("Confirm password: ")

    if password != confirmation:
        raise ValidationError("Passwords do not match.")

    if not 16 <= len(password) <= 256:
        raise ValidationError(
            "Use a password between 16 and 256 characters."
        )

    print("\nSet EWAA_ADMIN_PASSWORD_HASH to this value:\n")
    print(generate_password_hash(password))


# ============================================================
# COMMAND LINE
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="EWAA API server and dictionary tools."
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run the API server.")

    import_parser = subparsers.add_parser(
        "import",
        help="Import or update words from a JSON file.",
    )
    import_parser.add_argument("file")

    export_parser = subparsers.add_parser(
        "export",
        help="Export dictionary entries to a JSON file.",
    )
    export_parser.add_argument("file")

    subparsers.add_parser(
        "hash-password",
        help="Generate an admin password hash interactively.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.command == "import":
            import_dictionary(args.file)
            return

        if args.command == "export":
            export_dictionary(args.file)
            return

        if args.command == "hash-password":
            print_password_hash()
            return

        password = os.environ.get("EWAA_ADMIN_PASSWORD", "")

        if (
            not os.environ.get("EWAA_ADMIN_PASSWORD_HASH", "").strip()
            and len(password) > 256
        ):
            raise ValidationError(
                "EWAA_ADMIN_PASSWORD must be 256 characters or fewer."
            )

        app = create_app()

        LOGGER.info("Database: %s", DATABASE_PATH)
        LOGGER.info("EWAA API listening on http://%s:%s", HOST, PORT)

        serve(
            app,
            host=HOST,
            port=PORT,
            threads=8,
            max_request_body_size=16 * 1024,
            clear_untrusted_proxy_headers=True,
        )

    except (ValidationError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()