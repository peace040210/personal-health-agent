import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .domain import CoachMemory, Context, Metric, Window


class Store:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        if read_only:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
            tables = {name for name, in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self._has_sleep_details = "sleep_details" in tables
            self._has_bedtime = "bedtime_clock" in tables
            self._has_coaching = "coaching_memory" in tables
            return
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY,
                sleep_hours REAL,
                steps INTEGER
            );
            CREATE TABLE IF NOT EXISTS sleep_details (
                day TEXT PRIMARY KEY,
                bedtime_day TEXT,
                deep_sleep_hours REAL
            );
            CREATE TABLE IF NOT EXISTS bedtime_clock (
                day TEXT PRIMARY KEY,
                night TEXT NOT NULL,
                minute INTEGER NOT NULL CHECK(minute >= 0 AND minute < 1440)
            );
            CREATE INDEX IF NOT EXISTS bedtime_clock_night ON bedtime_clock(night);
            CREATE TABLE IF NOT EXISTS goals (
                metric TEXT PRIMARY KEY,
                target REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                last_metric TEXT,
                pending_message TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY,
                session TEXT NOT NULL,
                message TEXT NOT NULL,
                reply TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS turns_session_idx ON turns(session, id);
            CREATE TABLE IF NOT EXISTS coaching_memory (
                session TEXT NOT NULL,
                metric TEXT NOT NULL,
                details TEXT NOT NULL,
                updated_turn INTEGER NOT NULL,
                PRIMARY KEY (session, metric)
            );
        """)
        self._has_sleep_details = True
        self._has_bedtime = True
        self._has_coaching = True

    def close(self) -> None:
        self.db.close()

    def import_csv(self, path: Path) -> int:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            fields = set(reader.fieldnames or ())
            pha_format = not {"date", "sleep_hours", "steps"} <= fields
            if pha_format and not {"datetime", "sleep_minutes", "steps"} <= fields:
                raise ValueError("CSV 需要 date、sleep_hours、steps，或 PHA 的 datetime、sleep_minutes、steps 三列。")
            rows = []
            details = []
            bedtime_times = []
            seen = set()
            for row in reader:
                try:
                    if pha_format:
                        day = datetime.fromisoformat(row["datetime"].strip()).date().isoformat()
                        raw_sleep = row["sleep_minutes"].strip()
                    else:
                        day = date.fromisoformat(row["date"].strip()).isoformat()
                        raw_sleep = row["sleep_hours"].strip()
                    raw_steps = row["steps"].strip()
                    sleep = float(raw_sleep) if raw_sleep else None
                    if pha_format and sleep is not None:
                        sleep /= 60
                    steps = None
                    if raw_steps:
                        quantity = Decimal(raw_steps) if pha_format else int(raw_steps)
                        if not Metric.STEPS.valid(quantity):
                            raise ValueError("步数必须是有效的非负整数")
                        steps = int(quantity)
                    if sleep is not None and not Metric.SLEEP.valid(sleep):
                        raise ValueError("睡眠时长超出有效范围")
                    bedtime = (row.get("bed_time") or "").strip()
                    recorded_bedtime = datetime.fromisoformat(bedtime) if bedtime else None
                    bedtime_day = recorded_bedtime.date().isoformat() if recorded_bedtime else None
                    raw_deep = (row.get("deep_sleep_minutes") or "").strip()
                    deep_sleep = float(raw_deep) / 60 if raw_deep else None
                    if deep_sleep is not None and not Metric.SLEEP.valid(deep_sleep):
                        raise ValueError("深睡时长超出有效范围")
                    if day in seen:
                        raise ValueError("日期重复")
                except (ValueError, AttributeError, InvalidOperation) as error:
                    raise ValueError(f"CSV 第 {reader.line_num} 行无效：{error}") from error
                seen.add(day)
                rows.append((day, sleep, steps))
                if bedtime_day is not None or deep_sleep is not None:
                    details.append((day, bedtime_day, deep_sleep))
                if recorded_bedtime is not None and ("T" in bedtime or " " in bedtime):
                    bedtime_times.append((day, bedtime_day, recorded_bedtime.hour * 60 + recorded_bedtime.minute))

        with self.db:
            self.db.executemany("""
                INSERT INTO daily(day, sleep_hours, steps) VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    sleep_hours = COALESCE(excluded.sleep_hours, daily.sleep_hours),
                    steps = COALESCE(excluded.steps, daily.steps)
            """, rows)
            self.db.executemany("""
                INSERT INTO sleep_details(day, bedtime_day, deep_sleep_hours) VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    bedtime_day = COALESCE(excluded.bedtime_day, sleep_details.bedtime_day),
                    deep_sleep_hours = COALESCE(excluded.deep_sleep_hours, sleep_details.deep_sleep_hours)
            """, details)
            self.db.executemany("""
                INSERT INTO bedtime_clock(day, night, minute) VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET night = excluded.night, minute = excluded.minute
            """, bedtime_times)
        return len(rows)

    def analysis_rows(self, window: Window) -> list[dict]:
        start, end = window.start.isoformat(), window.end.isoformat()
        columns = ("day", "steps", "sleep_hours", "bedtime_day", "deep_sleep_hours")
        if self._has_sleep_details:
            rows = self.db.execute("""
                SELECT daily.day, daily.steps, daily.sleep_hours, bedtime_day, deep_sleep_hours
                FROM daily LEFT JOIN sleep_details ON daily.day = sleep_details.day
                WHERE daily.day BETWEEN ? AND ? OR bedtime_day BETWEEN ? AND ?
                ORDER BY daily.day
            """, (start, end, start, end))
        else:
            rows = self.db.execute("""
                SELECT day, steps, sleep_hours, NULL, NULL FROM daily
                WHERE day BETWEEN ? AND ? ORDER BY day
            """, (start, end))
        return [dict(zip(columns, row)) for row in rows]

    def last_observation(self, metric: Metric, through: date) -> date | None:
        day, = self.db.execute(
            f"SELECT MAX(day) FROM daily WHERE day <= ? AND {metric.value} IS NOT NULL",
            (through.isoformat(),),
        ).fetchone()
        return date.fromisoformat(day) if day else None

    def read_series(self, metric: Metric, window: Window) -> tuple[tuple[date, float], ...]:
        with closing(self.db.execute(
            "SELECT day, sleep_hours, steps FROM daily WHERE day BETWEEN ? AND ? ORDER BY day",
            (window.start.isoformat(), window.end.isoformat()),
        )) as cursor:
            position = 1 if metric is Metric.SLEEP else 2
            return tuple((date.fromisoformat(row[0]), row[position]) for row in cursor if row[position] is not None)

    def goals(self) -> dict[Metric, float]:
        return {Metric(metric): value for metric, value in self.db.execute("SELECT metric, target FROM goals")}

    def bedtime_minutes(self, window: Window) -> tuple[int, ...]:
        if not self._has_bedtime:
            return ()
        rows = self.db.execute("""
            SELECT MIN(clock.minute) FROM bedtime_clock AS clock
            JOIN sleep_details AS sleep ON sleep.day = clock.day AND sleep.bedtime_day = clock.night
            WHERE clock.night BETWEEN ? AND ? GROUP BY clock.night HAVING COUNT(*) = 1
            ORDER BY clock.night
        """, (window.start.isoformat(), window.end.isoformat()))
        return tuple(minute for minute, in rows)

    def context(self, session: str) -> Context:
        state = self.db.execute(
            "SELECT last_metric, pending_message FROM sessions WHERE id = ?", (session,)
        ).fetchone()
        recent = self.db.execute(
            "SELECT message, reply FROM turns WHERE session = ? ORDER BY id DESC LIMIT 3", (session,)
        ).fetchall()
        last_metric = Metric(state[0]) if state and state[0] else None
        memories = ()
        if self._has_coaching:
            coaching = self.db.execute("""
                SELECT metric, details FROM coaching_memory WHERE session = ? ORDER BY updated_turn DESC, metric
            """, (session,))
            memories = tuple(CoachMemory(Metric(metric), **json.loads(details)) for metric, details in coaching)
        return Context(last_metric, state[1] if state else None, tuple(reversed(recent)), self.goals(), memories)

    def finish_turn(
        self, session: str, message: str, reply: str, metric: Metric | None,
        pending: str | None, updates: dict[Metric, float],
        *, coaching: dict[Metric, CoachMemory] | None = None,
    ) -> None:
        with self.db:
            self.db.executemany("""
                INSERT INTO goals(metric, target) VALUES (?, ?)
                ON CONFLICT(metric) DO UPDATE SET target = excluded.target
            """, updates.items())
            self.db.execute("""
                INSERT INTO sessions(id, last_metric, pending_message) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET last_metric = excluded.last_metric,
                    pending_message = excluded.pending_message
            """, (session, metric, pending))
            cursor = self.db.execute(
                "INSERT INTO turns(session, message, reply) VALUES (?, ?, ?)", (session, message, reply)
            )
            for memory in (coaching or {}).values():
                fields = asdict(memory)
                fields.pop("metric")
                self.db.execute("""
                    INSERT INTO coaching_memory(session, metric, details, updated_turn) VALUES (?, ?, ?, ?)
                    ON CONFLICT(session, metric) DO UPDATE SET
                        details = excluded.details, updated_turn = excluded.updated_turn
                """, (session, memory.metric.value, json.dumps(fields, ensure_ascii=False), cursor.lastrowid))
