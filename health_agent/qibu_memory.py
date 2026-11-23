"""Browser ownership, conversation index, and confirmed memories for Qibu."""

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

_COOKIE = "qibu_device"
_SCOPE = "weekday_lunch_budget"


class MemoryConflict(ValueError):
    pass


class QibuMemory:
    cookie_name = _COOKIE

    def __init__(self, directory: Path):
        self.path = directory / "account.sqlite3"
        with self.connection() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    updated TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_owner ON conversation(owner, updated);
                CREATE TABLE IF NOT EXISTS memory_draft (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, session TEXT NOT NULL,
                    minutes INTEGER NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory (
                    owner TEXT NOT NULL, scope TEXT NOT NULL, minutes INTEGER NOT NULL,
                    draft TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (owner, scope)
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('cookie_key', ?)", (secrets.token_hex(32),))
            self.secret = bytes.fromhex(db.execute("SELECT value FROM settings WHERE key = 'cookie_key'").fetchone()[0])
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            yield db

    def identify(self, cookie: str) -> tuple[str, str | None]:
        owner, separator, signature = cookie.partition(".")
        if separator and len(owner) == 48 and owner.isascii() and hmac.compare_digest(signature.encode("utf-8"), self._sign(owner).encode("ascii")):
            return owner, None
        owner = secrets.token_hex(24)
        return owner, owner + "." + self._sign(owner)

    def _sign(self, owner: str) -> str:
        return hmac.new(self.secret, owner.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _time() -> str:
        return datetime.now(timezone.utc).isoformat()

    def bind(self, owner: str, session: UUID, *, create: bool = True) -> None:
        with self.connection() as db, db:
            if create:
                db.execute("INSERT OR IGNORE INTO conversation(id, owner, updated) VALUES (?, ?, ?)",
                           (str(session), owner, self._time()))
            row = db.execute("SELECT owner FROM conversation WHERE id = ?", (str(session),)).fetchone()
            if not row or row[0] != owner:
                raise LookupError("找不到这个会话。")

    def touch(self, owner: str, session: UUID, message: str) -> None:
        with self.connection() as db, db:
            db.execute("""
                UPDATE conversation SET title = CASE WHEN title = '' THEN ? ELSE title END, updated = ?
                WHERE id = ? AND owner = ?
            """, (message[:36], self._time(), str(session), owner))

    def conversations(self, owner: str) -> list[dict]:
        with self.connection() as db:
            return [{"id": sid, "title": title or "新会话"} for sid, title in db.execute("""
                SELECT id, title FROM conversation WHERE owner = ? ORDER BY updated DESC, rowid DESC
            """, (owner,))]

    def get(self, owner: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT minutes, updated FROM memory WHERE owner = ? AND scope = ?", (owner, _SCOPE)).fetchone()
        return {"scope": _SCOPE, "minutes": row[0], "updated_at": row[1]} if row else None

    def propose(self, owner: str, session: UUID, minutes: int) -> dict:
        draft = str(uuid4())
        with self.connection() as db, db:
            db.execute("UPDATE memory_draft SET status = 'replaced' WHERE owner = ? AND status = 'pending'", (owner,))
            db.execute("INSERT INTO memory_draft VALUES (?, ?, ?, ?, 'pending', ?)",
                       (draft, owner, str(session), minutes, self._time()))
        return {"id": draft, "scope": _SCOPE, "minutes": minutes}

    def draft_status(self, owner: str) -> dict[str, str]:
        with self.connection() as db:
            return dict(db.execute("SELECT id, status FROM memory_draft WHERE owner = ?", (owner,)))

    def change(self, owner: str, session: UUID, operation: str, draft: UUID | None) -> dict | None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if operation == "forget":
                db.execute("DELETE FROM memory WHERE owner = ? AND scope = ?", (owner, _SCOPE))
                db.execute("UPDATE memory_draft SET status = 'forgotten' WHERE owner = ? AND status IN ('pending', 'confirmed')", (owner,))
                return None
            row = db.execute("SELECT minutes, status FROM memory_draft WHERE id = ? AND owner = ? AND session = ?",
                             (str(draft), owner, str(session))).fetchone()
            if not row:
                raise LookupError("找不到这条记忆提议。")
            minutes, status = row
            if operation == "confirm" and status == "confirmed":
                current = db.execute("SELECT minutes, updated FROM memory WHERE owner = ? AND draft = ?",
                                     (owner, str(draft))).fetchone()
                if current:
                    return {"scope": _SCOPE, "minutes": current[0], "updated_at": current[1]}
            if status != "pending":
                raise MemoryConflict("这条记忆提议已处理，请用最新的提议。")
            if operation == "decline":
                db.execute("UPDATE memory_draft SET status = 'declined' WHERE id = ?", (str(draft),))
                return None
            updated = self._time()
            db.execute("UPDATE memory_draft SET status = 'confirmed' WHERE id = ?", (str(draft),))
            db.execute("""
                INSERT INTO memory VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner, scope) DO UPDATE SET minutes = excluded.minutes,
                    draft = excluded.draft, updated = excluded.updated
            """, (owner, _SCOPE, minutes, str(draft), updated))
            return {"scope": _SCOPE, "minutes": minutes, "updated_at": updated}
