from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from .advisor import robust_stats


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    transcript_path TEXT,
    model TEXT,
    cwd_hash TEXT,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    baseline_total INTEGER NOT NULL DEFAULT 0,
    baseline_input INTEGER NOT NULL DEFAULT 0,
    baseline_cached INTEGER NOT NULL DEFAULT 0,
    baseline_output INTEGER NOT NULL DEFAULT 0,
    baseline_reasoning INTEGER NOT NULL DEFAULT 0,
    baseline_primary_percent REAL,
    baseline_secondary_percent REAL
);
CREATE TABLE IF NOT EXISTS token_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    observed_at REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    last_input_tokens INTEGER NOT NULL,
    last_cached_input_tokens INTEGER NOT NULL,
    last_output_tokens INTEGER NOT NULL,
    last_reasoning_output_tokens INTEGER NOT NULL,
    last_total_tokens INTEGER NOT NULL,
    model_context_window INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS token_session_time ON token_snapshots(session_id, observed_at);
CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at REAL NOT NULL,
    limit_id TEXT NOT NULL,
    limit_name TEXT,
    window_kind TEXT NOT NULL,
    used_percent REAL,
    window_minutes INTEGER,
    resets_at REAL,
    plan_type TEXT,
    credits_balance TEXT,
    reset_credit_count INTEGER,
    reset_credit_details_json TEXT,
    reached_type TEXT,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limit_time ON rate_limit_snapshots(limit_id, window_kind, observed_at);
CREATE TABLE IF NOT EXISTS account_usage_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at REAL NOT NULL,
    lifetime_tokens INTEGER,
    peak_daily_tokens INTEGER,
    longest_running_turn_sec INTEGER,
    current_streak_days INTEGER,
    longest_streak_days INTEGER,
    daily_buckets_json TEXT,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    tool_name TEXT NOT NULL,
    category TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    success INTEGER
);
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    phase TEXT NOT NULL,
    trigger TEXT,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subagents (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    agent_type TEXT,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    turn_id TEXT,
    event_name TEXT NOT NULL,
    observed_at REAL NOT NULL,
    duration_ms REAL
);
CREATE TABLE IF NOT EXISTS ui_message_snapshots (
    thread_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    turn_id TEXT,
    phase TEXT NOT NULL,
    completed INTEGER NOT NULL,
    first_seen_at REAL,
    completed_at REAL,
    observed_at REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY(thread_id,item_id)
);
CREATE INDEX IF NOT EXISTS ui_message_time ON ui_message_snapshots(observed_at);
CREATE TABLE IF NOT EXISTS turn_aggregates (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    model TEXT,
    total_tokens INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    duration_seconds REAL,
    tool_seconds REAL,
    ending_context_percent REAL,
    context_source TEXT,
    primary_quota_delta REAL,
    secondary_quota_delta REAL,
    tool_calls INTEGER,
    failed_tool_calls INTEGER,
    file_edits INTEGER,
    compaction_count INTEGER,
    cache_hit_percent REAL,
    reasoning_ratio REAL,
    materialized_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS aggregate_session_time ON turn_aggregates(session_id, ended_at);
CREATE INDEX IF NOT EXISTS aggregate_time ON turn_aggregates(ended_at);
CREATE TABLE IF NOT EXISTS prompt_features (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    section_count INTEGER NOT NULL,
    bullet_count INTEGER NOT NULL,
    clause_count INTEGER NOT NULL,
    recommendation_codes_json TEXT NOT NULL,
    analyzed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_advice (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    confidence TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(session_id,turn_id,code)
);
CREATE INDEX IF NOT EXISTS advice_session_time ON turn_advice(session_id,created_at);
"""


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=3.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        previous_schema = self.get_meta("schema_version")
        self._ensure_column("rate_limit_snapshots", "reset_credit_count", "INTEGER")
        self._ensure_column("rate_limit_snapshots", "reset_credit_details_json", "TEXT")
        self._ensure_column("ui_message_snapshots", "first_seen_at", "REAL")
        self._ensure_column("ui_message_snapshots", "completed_at", "REAL")
        for column, declaration in (
            ("tool_calls", "INTEGER"), ("failed_tool_calls", "INTEGER"), ("file_edits", "INTEGER"),
            ("compaction_count", "INTEGER"), ("cache_hit_percent", "REAL"), ("reasoning_ratio", "REAL"),
        ):
            self._ensure_column("turn_aggregates", column, declaration)
        self.conn.execute("UPDATE ui_message_snapshots SET first_seen_at=observed_at WHERE first_seen_at IS NULL")
        if self.get_meta("removed_legacy_index_snapshots") != "1":
            self.conn.execute("DELETE FROM ui_message_snapshots WHERE thread_id='/index.html'")
            self.set_meta("removed_legacy_index_snapshots", "1")
        self.conn.commit()
        self.set_meta("schema_version", "3")
        self.materialize_completed_turns()
        if previous_schema != "3":
            turn_ids = self.conn.execute("SELECT turn_id FROM turns WHERE ended_at IS NOT NULL").fetchall()
            for row in turn_ids:
                self.materialize_turn(str(row["turn_id"]), commit=False)
            self.conn.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        if not isinstance(value, str):
            value = json.dumps(value, separators=(",", ":"))
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def upsert_session(self, session_id: str, transcript_path: str | None, model: str | None, cwd_hash: str | None) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO sessions(session_id,transcript_path,model,cwd_hash,started_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET transcript_path=COALESCE(excluded.transcript_path,sessions.transcript_path),
                 model=COALESCE(excluded.model,sessions.model), cwd_hash=COALESCE(excluded.cwd_hash,sessions.cwd_hash),
                 updated_at=excluded.updated_at""",
            (session_id, transcript_path, model, cwd_hash, now, now),
        )
        self.conn.commit()

    def latest_tokens(self, session_id: str | None = None) -> sqlite3.Row | None:
        if session_id:
            return self.conn.execute(
                "SELECT * FROM token_snapshots WHERE session_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self.conn.execute("SELECT * FROM token_snapshots ORDER BY observed_at DESC,id DESC LIMIT 1").fetchone()

    def start_turn(self, turn_id: str, session_id: str) -> None:
        now = time.time()
        # A session can only have one live turn. Closing an orphaned predecessor
        # prevents it from winning later session-local selection forever.
        self.conn.execute(
            "UPDATE turns SET ended_at=? WHERE session_id=? AND ended_at IS NULL AND turn_id<>?",
            (now, session_id, turn_id),
        )
        token = self.latest_tokens(session_id)
        rates = self.latest_rates()
        primary = rates.get(("codex", "primary")) or next((v for k, v in rates.items() if k[1] == "primary"), None)
        secondary = rates.get(("codex", "secondary")) or next((v for k, v in rates.items() if k[1] == "secondary"), None)
        values = (
            token["total_tokens"] if token else 0,
            token["input_tokens"] if token else 0,
            token["cached_input_tokens"] if token else 0,
            token["output_tokens"] if token else 0,
            token["reasoning_output_tokens"] if token else 0,
            primary["used_percent"] if primary else None,
            secondary["used_percent"] if secondary else None,
        )
        self.conn.execute(
            """INSERT INTO turns(turn_id,session_id,started_at,baseline_total,baseline_input,baseline_cached,
               baseline_output,baseline_reasoning,baseline_primary_percent,baseline_secondary_percent)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(turn_id) DO NOTHING""",
            (turn_id, session_id, now, *values),
        )
        self.conn.commit()

    def save_prompt_features(self, turn_id: str, session_id: str, features: dict[str, Any]) -> None:
        codes = [str(code) for code in features.get("recommendation_codes", [])]
        self.conn.execute(
            """INSERT INTO prompt_features(turn_id,session_id,char_count,line_count,section_count,bullet_count,
               clause_count,recommendation_codes_json,analyzed_at) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(turn_id) DO UPDATE SET char_count=excluded.char_count,line_count=excluded.line_count,
               section_count=excluded.section_count,bullet_count=excluded.bullet_count,clause_count=excluded.clause_count,
               recommendation_codes_json=excluded.recommendation_codes_json,analyzed_at=excluded.analyzed_at""",
            (turn_id, session_id, int(features.get("char_count") or 0), int(features.get("line_count") or 0),
             int(features.get("section_count") or 0), int(features.get("bullet_count") or 0),
             int(features.get("clause_count") or 0), json.dumps(codes, separators=(",", ":")), time.time()),
        )
        self.conn.commit()

    def prompt_features(self, turn_id: str | None) -> dict[str, Any] | None:
        if not turn_id:
            return None
        row = self.conn.execute("SELECT * FROM prompt_features WHERE turn_id=?", (turn_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        try:
            value["recommendation_codes"] = json.loads(value.pop("recommendation_codes_json"))
        except json.JSONDecodeError:
            value["recommendation_codes"] = []
        return value

    def end_turn(self, turn_id: str) -> None:
        self.conn.execute("UPDATE turns SET ended_at=COALESCE(ended_at,?) WHERE turn_id=?", (time.time(), turn_id))
        self.conn.commit()
        self.materialize_turn(turn_id)

    def active_turn(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def latest_turn(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY (ended_at IS NULL) DESC,started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    def has_session(self, session_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sessions WHERE session_id=? LIMIT 1", (session_id,)).fetchone() is not None

    def add_tokens(self, session_id: str, turn_id: str | None, info: dict[str, Any], observed_at: float, source: str) -> None:
        total = info.get("total_token_usage") or info.get("total") or {}
        last = info.get("last_token_usage") or info.get("last") or {}
        row = (
            session_id,
            turn_id,
            observed_at,
            int(total.get("input_tokens", total.get("inputTokens", 0)) or 0),
            int(total.get("cached_input_tokens", total.get("cachedInputTokens", 0)) or 0),
            int(total.get("output_tokens", total.get("outputTokens", 0)) or 0),
            int(total.get("reasoning_output_tokens", total.get("reasoningOutputTokens", 0)) or 0),
            int(total.get("total_tokens", total.get("totalTokens", 0)) or 0),
            int(last.get("input_tokens", last.get("inputTokens", 0)) or 0),
            int(last.get("cached_input_tokens", last.get("cachedInputTokens", 0)) or 0),
            int(last.get("output_tokens", last.get("outputTokens", 0)) or 0),
            int(last.get("reasoning_output_tokens", last.get("reasoningOutputTokens", 0)) or 0),
            int(last.get("total_tokens", last.get("totalTokens", 0)) or 0),
            info.get("model_context_window", info.get("modelContextWindow")),
            source,
        )
        previous = self.latest_tokens(session_id)
        if (
            previous
            and previous["turn_id"] == turn_id
            and previous["total_tokens"] == row[7]
            and previous["last_total_tokens"] == row[12]
        ):
            return
        self.conn.execute(
            """INSERT INTO token_snapshots(session_id,turn_id,observed_at,input_tokens,cached_input_tokens,
               output_tokens,reasoning_output_tokens,total_tokens,last_input_tokens,last_cached_input_tokens,
               last_output_tokens,last_reasoning_output_tokens,last_total_tokens,model_context_window,source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        self.conn.commit()

    def add_rate_limits(self, payload: dict[str, Any], observed_at: float, source: str) -> None:
        multi = payload.get("rateLimitsByLimitId") or payload.get("rate_limits_by_limit_id")
        if not multi:
            single = payload.get("rateLimits") or payload.get("rate_limits") or payload
            multi = {single.get("limitId", single.get("limit_id", "codex")): single}
        reset_credits = payload.get("rateLimitResetCredits", payload.get("rate_limit_reset_credits"))
        reset_count = None
        reset_details = None
        if isinstance(reset_credits, dict):
            reset_count = reset_credits.get("availableCount", reset_credits.get("available_count"))
            details = reset_credits.get("credits")
            if details is not None:
                reset_details = json.dumps(details, separators=(",", ":"))
        for limit_id, bucket in multi.items():
            for kind in ("primary", "secondary"):
                window = bucket.get(kind)
                if not window:
                    continue
                self.conn.execute(
                    """INSERT INTO rate_limit_snapshots(observed_at,limit_id,limit_name,window_kind,used_percent,
                       window_minutes,resets_at,plan_type,credits_balance,reset_credit_count,
                       reset_credit_details_json,reached_type,source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observed_at,
                        bucket.get("limitId", bucket.get("limit_id", limit_id)),
                        bucket.get("limitName", bucket.get("limit_name")),
                        kind,
                        window.get("usedPercent", window.get("used_percent")),
                        window.get("windowDurationMins", window.get("window_minutes")),
                        window.get("resetsAt", window.get("resets_at")),
                        bucket.get("planType", bucket.get("plan_type")),
                        _credit_balance(bucket.get("credits")),
                        reset_count,
                        reset_details,
                        bucket.get("rateLimitReachedType", bucket.get("rate_limit_reached_type")),
                        source,
                    ),
                )
        self.conn.commit()

    def latest_rates(self) -> dict[tuple[str, str], sqlite3.Row]:
        rows = self.conn.execute(
            """SELECT r.* FROM rate_limit_snapshots r JOIN (
                 SELECT limit_id,window_kind,MAX(id) AS id FROM rate_limit_snapshots GROUP BY limit_id,window_kind
               ) latest ON latest.id=r.id"""
        ).fetchall()
        return {(row["limit_id"], row["window_kind"]): row for row in rows}

    def rates_at(self, observed_at: float) -> dict[tuple[str, str], sqlite3.Row]:
        rows = self.conn.execute(
            """SELECT r.* FROM rate_limit_snapshots r JOIN (
                 SELECT limit_id,window_kind,MAX(id) AS id FROM rate_limit_snapshots
                 WHERE observed_at<=? GROUP BY limit_id,window_kind
               ) latest ON latest.id=r.id""",
            (observed_at,),
        ).fetchall()
        return {(row["limit_id"], row["window_kind"]): row for row in rows}

    def add_account_usage(self, result: dict[str, Any], observed_at: float, source: str) -> None:
        summary = result.get("summary") or {}
        buckets = result.get("dailyUsageBuckets", result.get("daily_usage_buckets"))
        self.conn.execute(
            """INSERT INTO account_usage_snapshots(observed_at,lifetime_tokens,peak_daily_tokens,
               longest_running_turn_sec,current_streak_days,longest_streak_days,daily_buckets_json,source)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                observed_at,
                summary.get("lifetimeTokens", summary.get("lifetime_tokens")),
                summary.get("peakDailyTokens", summary.get("peak_daily_tokens")),
                summary.get("longestRunningTurnSec", summary.get("longest_running_turn_sec")),
                summary.get("currentStreakDays", summary.get("current_streak_days")),
                summary.get("longestStreakDays", summary.get("longest_streak_days")),
                json.dumps(buckets, separators=(",", ":")) if buckets is not None else None,
                source,
            ),
        )
        self.conn.commit()

    def latest_account_usage(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM account_usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()

    def record_tool_start(self, payload: dict[str, Any], session_id: str, turn_id: str | None) -> None:
        tool_id = payload.get("tool_use_id") or f"{session_id}:{time.time_ns()}"
        tool_name = str(payload.get("tool_name") or "unknown")
        category = categorize_tool(tool_name)
        self.conn.execute(
            """INSERT INTO tool_calls(tool_use_id,session_id,turn_id,tool_name,category,started_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(tool_use_id) DO NOTHING""",
            (tool_id, session_id, turn_id, tool_name, category, time.time()),
        )
        self.conn.commit()

    def record_tool_end(self, payload: dict[str, Any], session_id: str, turn_id: str | None) -> None:
        tool_id = payload.get("tool_use_id") or f"{session_id}:{time.time_ns()}"
        tool_name = str(payload.get("tool_name") or "unknown")
        success = infer_success(payload.get("tool_response"))
        cursor = self.conn.execute(
            "UPDATE tool_calls SET ended_at=?,success=? WHERE tool_use_id=?",
            (time.time(), int(success), tool_id),
        )
        if cursor.rowcount == 0:
            self.conn.execute(
                """INSERT INTO tool_calls(tool_use_id,session_id,turn_id,tool_name,category,started_at,ended_at,success)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (tool_id, session_id, turn_id, tool_name, categorize_tool(tool_name), time.time(), time.time(), int(success)),
            )
        self.conn.commit()

    def record_compaction(self, session_id: str, turn_id: str | None, phase: str, trigger: str | None) -> None:
        self.conn.execute(
            "INSERT INTO compactions(session_id,turn_id,phase,trigger,observed_at) VALUES(?,?,?,?,?)",
            (session_id, turn_id, phase, trigger, time.time()),
        )
        self.conn.commit()

    def record_subagent(self, payload: dict[str, Any], session_id: str, turn_id: str | None, start: bool) -> None:
        agent_id = str(payload.get("agent_id") or "unknown")
        if start:
            self.conn.execute(
                """INSERT INTO subagents(agent_id,session_id,turn_id,agent_type,started_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET started_at=COALESCE(subagents.started_at,excluded.started_at)""",
                (agent_id, session_id, turn_id, payload.get("agent_type"), time.time()),
            )
        else:
            self.conn.execute(
                """INSERT INTO subagents(agent_id,session_id,turn_id,agent_type,ended_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET ended_at=excluded.ended_at""",
                (agent_id, session_id, turn_id, payload.get("agent_type"), time.time()),
            )
        self.conn.commit()

    def record_hook(self, session_id: str | None, turn_id: str | None, event_name: str, duration_ms: float) -> None:
        self.conn.execute(
            "INSERT INTO hook_events(session_id,turn_id,event_name,observed_at,duration_ms) VALUES(?,?,?,?,?)",
            (session_id, turn_id, event_name, time.time(), duration_ms),
        )
        self.conn.commit()

    def save_message_snapshot(
        self,
        thread_id: str,
        item_id: str,
        turn_id: str | None,
        phase: str,
        completed: bool,
        snapshot: dict[str, Any],
    ) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO ui_message_snapshots(thread_id,item_id,turn_id,phase,completed,first_seen_at,completed_at,observed_at,snapshot_json)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,item_id) DO UPDATE SET
               turn_id=excluded.turn_id, phase=excluded.phase, completed=excluded.completed,
               completed_at=CASE WHEN ui_message_snapshots.completed_at IS NULL THEN excluded.completed_at ELSE ui_message_snapshots.completed_at END,
               observed_at=excluded.observed_at, snapshot_json=excluded.snapshot_json""",
            (thread_id, item_id, turn_id, phase, int(completed), now, now if completed else None, now,
             json.dumps(snapshot, separators=(",", ":"))),
        )
        self.conn.commit()

    def message_snapshots(self, thread_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if thread_id:
            rows = self.conn.execute(
                "SELECT * FROM ui_message_snapshots WHERE thread_id=? ORDER BY observed_at DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ui_message_snapshots ORDER BY observed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["completed"] = bool(value["completed"])
            end = value.get("completed_at") or time.time()
            start = value.get("first_seen_at") or value.get("observed_at")
            value["duration_seconds"] = max(0.0, end - start) if start else None
            try:
                value["snapshot"] = json.loads(value.pop("snapshot_json"))
            except json.JSONDecodeError:
                value["snapshot"] = {}
                value.pop("snapshot_json", None)
            result.append(value)
        return result

    def materialize_completed_turns(self) -> int:
        rows = self.conn.execute(
            "SELECT turn_id FROM turns WHERE ended_at IS NOT NULL AND turn_id NOT IN (SELECT turn_id FROM turn_aggregates)"
        ).fetchall()
        for row in rows:
            self.materialize_turn(str(row["turn_id"]), commit=False)
        self.conn.commit()
        return len(rows)

    def materialize_turn(
        self, turn_id: str, ending_context_percent: float | None = None,
        context_source: str | None = None, commit: bool = True,
    ) -> None:
        turn = self.conn.execute("SELECT * FROM turns WHERE turn_id=? AND ended_at IS NOT NULL", (turn_id,)).fetchone()
        if not turn:
            return
        token = self.conn.execute(
            "SELECT * FROM token_snapshots WHERE turn_id=? ORDER BY observed_at DESC,id DESC LIMIT 1", (turn_id,)
        ).fetchone()
        if not token:
            token = self.conn.execute(
                "SELECT * FROM token_snapshots WHERE session_id=? AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (turn["session_id"], float(turn["ended_at"]) + 10.0),
            ).fetchone()
        def delta(column: str, baseline: str) -> int | None:
            return max(0, int(token[column]) - int(turn[baseline] or 0)) if token else None
        if ending_context_percent is None and token and token["model_context_window"]:
            ending_context_percent = 100.0 * int(token["last_input_tokens"] or 0) / int(token["model_context_window"])
            context_source = context_source or "estimated"
        rates = self.rates_at(float(turn["ended_at"]) + 10.0)
        primary = next((v for k, v in rates.items() if k[1] == "primary"), None)
        secondary = next((v for k, v in rates.items() if k[1] == "secondary"), None)
        def quota(rate: sqlite3.Row | None, baseline: Any) -> float | None:
            if not rate or rate["used_percent"] is None or baseline is None:
                return None
            value = float(rate["used_percent"]) - float(baseline)
            return value if value >= 0 else None
        tools = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN ended_at IS NOT NULL THEN ended_at-started_at ELSE 0 END),0) seconds,
               COUNT(*) calls,SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) failures,
               SUM(CASE WHEN category='file_edit' THEN 1 ELSE 0 END) edits FROM tool_calls WHERE turn_id=?""",
            (turn_id,),
        ).fetchone()
        compactions = self.conn.execute(
            "SELECT COUNT(*) count FROM compactions WHERE turn_id=? AND phase='post'", (turn_id,)
        ).fetchone()
        session = self.conn.execute("SELECT model FROM sessions WHERE session_id=?", (turn["session_id"],)).fetchone()
        input_tokens = delta("input_tokens", "baseline_input")
        cached_tokens = delta("cached_input_tokens", "baseline_cached")
        total_tokens = delta("total_tokens", "baseline_total")
        reasoning_tokens = delta("reasoning_output_tokens", "baseline_reasoning")
        cache_hit = 100.0 * cached_tokens / input_tokens if input_tokens else None
        reasoning_ratio = 100.0 * reasoning_tokens / total_tokens if total_tokens else None
        self.conn.execute(
            """INSERT INTO turn_aggregates(turn_id,session_id,started_at,ended_at,model,total_tokens,input_tokens,
               cached_input_tokens,output_tokens,reasoning_tokens,duration_seconds,tool_seconds,ending_context_percent,
               context_source,primary_quota_delta,secondary_quota_delta,tool_calls,failed_tool_calls,file_edits,
               compaction_count,cache_hit_percent,reasoning_ratio,materialized_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(turn_id) DO UPDATE SET model=excluded.model,total_tokens=excluded.total_tokens,
               input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,
               output_tokens=excluded.output_tokens,reasoning_tokens=excluded.reasoning_tokens,
               duration_seconds=excluded.duration_seconds,tool_seconds=excluded.tool_seconds,
               ending_context_percent=COALESCE(excluded.ending_context_percent,turn_aggregates.ending_context_percent),
               context_source=COALESCE(excluded.context_source,turn_aggregates.context_source),
               primary_quota_delta=excluded.primary_quota_delta,secondary_quota_delta=excluded.secondary_quota_delta,
               tool_calls=excluded.tool_calls,failed_tool_calls=excluded.failed_tool_calls,file_edits=excluded.file_edits,
               compaction_count=excluded.compaction_count,cache_hit_percent=excluded.cache_hit_percent,
               reasoning_ratio=excluded.reasoning_ratio,
               materialized_at=excluded.materialized_at""",
            (turn_id, turn["session_id"], turn["started_at"], turn["ended_at"], session["model"] if session else None,
             total_tokens, input_tokens, cached_tokens, delta("output_tokens", "baseline_output"),
             reasoning_tokens, float(turn["ended_at"])-float(turn["started_at"]),
             float(tools["seconds"] or 0), ending_context_percent, context_source,
             quota(primary, turn["baseline_primary_percent"]), quota(secondary, turn["baseline_secondary_percent"]),
             int(tools["calls"] or 0), int(tools["failures"] or 0), int(tools["edits"] or 0),
             int(compactions["count"] or 0), cache_hit, reasoning_ratio, time.time()),
        )
        if commit:
            self.conn.commit()

    def history(self, session_id: str | None, since: float | None, scope: str = "current_chat", limit: int = 500,
                baseline_window: int = 50) -> list[dict[str, Any]]:
        self.materialize_completed_turns()
        where, params = ["ended_at>=?"], [float(since or 0)]
        if scope == "current_chat":
            if not session_id:
                return []
            where.append("session_id=?")
            params.append(session_id)
        elif scope != "all_chats":
            raise ValueError("scope must be current_chat or all_chats")
        rows = self.conn.execute(
            f"SELECT * FROM turn_aggregates WHERE {' AND '.join(where)} ORDER BY ended_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 5000))),
        ).fetchall()
        result = []
        baselines: dict[str, dict[str, Any]] = {}
        for row in reversed(rows):
            value = dict(row)
            value["session_label"] = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(value['started_at']))} · {value.get('model') or '—'} · {value['session_id'][:8]}"
            advice = self.conn.execute(
                "SELECT code,level,confidence,evidence_json FROM turn_advice WHERE turn_id=? ORDER BY created_at",
                (value["turn_id"],),
            ).fetchall()
            value["advice"] = [{**dict(item), "evidence": json.loads(item["evidence_json"])} for item in advice]
            for item in value["advice"]:
                item.pop("evidence_json", None)
            model_key = str(value.get("model") or "")
            if model_key not in baselines:
                baselines[model_key] = self.advisor_baseline(model_key, baseline_window)
            value["baseline_total_tokens"] = (baselines[model_key].get("total_tokens") or {}).get("median")
            result.append(value)
        return result

    def advisor_baseline(self, model: str | None, window: int = 50, exclude_turn_id: str | None = None) -> dict[str, Any]:
        if not model:
            return {}
        exclude = "AND turn_id<>?" if exclude_turn_id else ""
        params: tuple[Any, ...] = (model, exclude_turn_id, max(1, min(int(window), 500))) if exclude_turn_id else (
            model, max(1, min(int(window), 500)))
        rows = self.conn.execute(
            """SELECT total_tokens,primary_quota_delta,tool_calls,tool_seconds,reasoning_tokens
               FROM turn_aggregates WHERE model=? AND total_tokens IS NOT NULL """ + exclude +
            " ORDER BY ended_at DESC LIMIT ?",
            params,
        ).fetchall()
        return {
            "model": model,
            "total_tokens": robust_stats(row["total_tokens"] for row in rows),
            "quota_delta": robust_stats(row["primary_quota_delta"] for row in rows),
            "tool_calls": robust_stats(row["tool_calls"] for row in rows),
            "tool_seconds": robust_stats(row["tool_seconds"] for row in rows),
            "reasoning_tokens": robust_stats(row["reasoning_tokens"] for row in rows),
        }

    def save_advice(self, session_id: str, turn_id: str, items: list[dict[str, Any]]) -> None:
        for item in items:
            evidence = item.get("evidence") or {}
            if any(not (value is None or isinstance(value, (bool, int, float))) for value in evidence.values()):
                continue
            self.conn.execute(
                """INSERT INTO turn_advice(session_id,turn_id,code,level,confidence,source,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,turn_id,code) DO UPDATE SET
                   level=excluded.level,confidence=excluded.confidence,source=excluded.source,
                   evidence_json=excluded.evidence_json,created_at=excluded.created_at""",
                (session_id, turn_id, item["code"], item["level"], item["confidence"], item["source"],
                 json.dumps(evidence, separators=(",", ":")), time.time()),
            )
        self.conn.commit()

    def rolling_forecast(self) -> dict[str, Any]:
        latest = self.latest_rates()
        primary = next((v for k, v in latest.items() if k[1] == "primary" and v["used_percent"] is not None), None)
        if not primary:
            return {"windows": {}, "remaining_turns": None}
        now = time.time()
        rows = self.conn.execute(
            """SELECT observed_at,used_percent,resets_at FROM rate_limit_snapshots
               WHERE limit_id=? AND window_kind=? AND used_percent IS NOT NULL AND observed_at>=?
               ORDER BY observed_at""",
            (primary["limit_id"], primary["window_kind"], now-3600),
        ).fetchall()
        windows: dict[str, Any] = {}
        for minutes in (15, 60):
            sample = [r for r in rows if float(r["observed_at"]) >= now-minutes*60]
            segment: list[sqlite3.Row] = []
            for row in sample:
                if segment and float(row["used_percent"]) < float(segment[-1]["used_percent"]):
                    segment = []
                segment.append(row)
            span = float(segment[-1]["observed_at"])-float(segment[0]["observed_at"]) if len(segment)>1 else 0
            if len(segment) >= 3 and span >= 300:
                slope = max(0.0, (float(segment[-1]["used_percent"])-float(segment[0]["used_percent"])) / (span/3600))
                windows[str(minutes)] = {"burn_percent_per_hour": slope, "samples": len(segment), "span_seconds": span,
                    "projected_exhaustion_hours": (100-float(primary["used_percent"]))/slope if slope>0 else None,
                    "confidence": "high" if len(segment)>=8 and span>=minutes*45 else "medium"}
        deltas = [float(r[0]) for r in self.conn.execute(
            "SELECT primary_quota_delta FROM turn_aggregates WHERE primary_quota_delta>0 ORDER BY ended_at DESC LIMIT 20"
        ).fetchall()]
        median = statistics.median(deltas) if deltas else None
        return {"windows": windows, "remaining_turns": (100-float(primary["used_percent"]))/median if median else None,
                "median_turn_quota_delta": median}

    def refresh_completed_turn_snapshots(self, turn_id: str, snapshot: dict[str, Any]) -> int:
        cursor = self.conn.execute(
            """UPDATE ui_message_snapshots SET observed_at=?,snapshot_json=?
               WHERE turn_id=? AND completed=1 AND phase='final_answer'""",
            (time.time(), json.dumps(snapshot, separators=(",", ":")), turn_id),
        )
        self.conn.commit()
        return cursor.rowcount

    def summary(self, session_id: str | None, turn_id: str | None, baseline_window: int = 50) -> dict[str, Any]:
        turn = self.conn.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone() if turn_id else None
        if not turn and session_id:
            turn = self.latest_turn(session_id)
        if not turn and not session_id:
            turn = self.conn.execute(
                "SELECT * FROM turns ORDER BY (ended_at IS NULL) DESC,started_at DESC LIMIT 1"
            ).fetchone()
        if turn and not session_id:
            session_id = str(turn["session_id"])
        token = None
        rates = self.latest_rates()
        if turn_id and turn:
            token = self.conn.execute(
                "SELECT * FROM token_snapshots WHERE turn_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
            if not token:
                cutoff = float(turn["ended_at"] or time.time()) + (10.0 if turn["ended_at"] else 0.0)
                token = self.conn.execute(
                    """SELECT * FROM token_snapshots WHERE session_id=? AND observed_at<=?
                       ORDER BY observed_at DESC,id DESC LIMIT 1""",
                    (session_id, cutoff),
                ).fetchone()
            if turn["ended_at"]:
                rates = self.rates_at(float(turn["ended_at"]) + 10.0)
        if token is None:
            token = self.latest_tokens(session_id)
        account = self.latest_account_usage()
        tools = None
        if turn:
            tools = self.conn.execute(
                """SELECT COUNT(*) total_calls,SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) successful_calls,
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) failed_calls,
                   SUM(CASE WHEN category='bash' THEN 1 ELSE 0 END) bash_calls,
                   SUM(CASE WHEN category='file_edit' THEN 1 ELSE 0 END) file_edits,
                   SUM(CASE WHEN category='mcp' THEN 1 ELSE 0 END) mcp_calls,
                   SUM(CASE WHEN category='web' THEN 1 ELSE 0 END) web_searches,
                   SUM(CASE WHEN ended_at IS NOT NULL THEN ended_at-started_at ELSE 0 END) tool_seconds
                   FROM tool_calls WHERE turn_id=?""",
                (turn["turn_id"],),
            ).fetchone()
        compactions = self.conn.execute(
            "SELECT COUNT(*) count,MAX(observed_at) last_time FROM compactions WHERE session_id=? AND phase='post'",
            (session_id,),
        ).fetchone() if session_id else None
        subagents = self.conn.execute(
            """SELECT COUNT(*) started,SUM(CASE WHEN ended_at IS NOT NULL THEN 1 ELSE 0 END) completed,
               SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) active
               FROM subagents WHERE session_id=?""",
            (session_id,),
        ).fetchone() if session_id else None
        model = None
        if turn:
            session_row = self.conn.execute("SELECT model FROM sessions WHERE session_id=?", (turn["session_id"],)).fetchone()
            model = session_row["model"] if session_row else None
        return {
            "token": dict(token) if token else None,
            "turn": dict(turn) if turn else None,
            "rates": {f"{k[0]}:{k[1]}": dict(v) for k, v in rates.items()},
            "account": dict(account) if account else None,
            "tools": dict(tools) if tools else None,
            "compactions": dict(compactions) if compactions else {"count": 0, "last_time": None},
            "subagents": dict(subagents) if subagents else {"started": 0, "completed": 0, "active": 0},
            "rolling_forecast": self.rolling_forecast(),
            "advisor_baseline": self.advisor_baseline(model, baseline_window, str(turn["turn_id"]) if turn else None),
            "prompt_features": self.prompt_features(str(turn["turn_id"])) if turn else None,
        }

    def reset(self) -> None:
        self.conn.close()
        self.path.unlink(missing_ok=True)

    def prune(self, retention_days: int) -> None:
        cutoff = time.time() - max(1, retention_days) * 86400
        for table in ("token_snapshots", "rate_limit_snapshots", "account_usage_snapshots", "compactions", "hook_events", "ui_message_snapshots"):
            self.conn.execute(f"DELETE FROM {table} WHERE observed_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM turns WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM turn_aggregates WHERE ended_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM prompt_features WHERE analyzed_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM turn_advice WHERE created_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM tool_calls WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM subagents WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
        self.conn.commit()


def categorize_tool(name: str) -> str:
    lowered = name.lower()
    if lowered in {"bash", "shell", "shell_command"}:
        return "bash"
    if lowered in {"apply_patch", "edit", "write"}:
        return "file_edit"
    if lowered.startswith("mcp__"):
        return "mcp"
    if "websearch" in lowered or "web_search" in lowered:
        return "web"
    if "computer" in lowered:
        return "computer"
    return "other"


def infer_success(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("error"):
            return False
        code = response.get("exit_code", response.get("exitCode"))
        if code is not None:
            return int(code) == 0
        if response.get("isError") is True:
            return False
    return True


def _credit_balance(credits: Any) -> str | None:
    if isinstance(credits, dict):
        value = credits.get("balance")
        return str(value) if value is not None else None
    return None
