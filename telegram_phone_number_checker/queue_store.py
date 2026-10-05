"""Durable local storage for Telegram validation jobs."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

from telegram_phone_number_checker.web_service import (
    is_valid_phone,
    map_lookup_result,
    parse_company_phone_batch,
)


TERMINAL_STATUSES = {
    "FOUND",
    "FOUND_NO_USERNAME",
    "UNRESOLVABLE_OR_PRIVACY",
    "INVALID_PHONE",
    "API_ERROR_FINAL",
    "DEFERRED_NOT_CHECKED",
    "CANCELLED",
}

LEGACY_DIRECT_REQUEUE_FILTER = """method = 'DIRECT_LOOKUP'
    AND status IN ('PENDING', 'RETRYABLE_ERROR', 'UNRESOLVABLE_OR_PRIVACY')
    AND manual_confirmation != 'MANUAL_CHAT_CONFIRMED'
    AND api_status NOT IN ('FOUND', 'FOUND_NO_USERNAME')"""


class QueueStore:
    """SQLite-backed queue; every public mutation is a short transaction."""

    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self.path = path
        self.now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._create_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, raw_input TEXT NOT NULL, state TEXT NOT NULL,
                phase TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
                started_at REAL, updated_at REAL NOT NULL, completed_at REAL,
                total_rows INTEGER NOT NULL, paused_at REAL, resume_after REAL,
                import_batch_size INTEGER NOT NULL DEFAULT 10,
                import_retry_count INTEGER NOT NULL DEFAULT 0, import_success_streak INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                run_lease_owner TEXT, run_lease_expires_at REAL
            );
            CREATE TABLE IF NOT EXISTS job_items (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, company_name TEXT NOT NULL, company_link TEXT NOT NULL,
                raw_phone TEXT NOT NULL, normalized_phone TEXT NOT NULL, status TEXT NOT NULL,
                method TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL, lease_owner TEXT, lease_expires_at REAL,
                import_retry_count INTEGER NOT NULL DEFAULT 0,
                telegram_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '', telegram_link TEXT NOT NULL DEFAULT '',
                api_status TEXT NOT NULL DEFAULT '', manual_confirmation TEXT NOT NULL DEFAULT 'NOT_CONFIRMED',
                manual_confirmed_at REAL,
                started_at REAL, completed_at REAL, UNIQUE(job_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS job_item_attempts (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES job_items(id) ON DELETE CASCADE,
                created_at REAL NOT NULL, method TEXT NOT NULL, outcome TEXT NOT NULL,
                detail TEXT NOT NULL, source TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS item_attempt_history ON job_item_attempts(item_id, id);
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                created_at REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
                processed INTEGER NOT NULL, total INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_state (
                account_key TEXT PRIMARY KEY, lease_owner TEXT, lease_expires_at REAL,
                next_import_at REAL NOT NULL DEFAULT 0, next_direct_lookup_at REAL NOT NULL DEFAULT 0,
                next_available_at REAL NOT NULL DEFAULT 0, import_batches_since_cleanup INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contact_cleanup (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                telegram_user_id INTEGER NOT NULL, item_id INTEGER REFERENCES job_items(id), state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL, last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                completed_at REAL, lease_owner TEXT, lease_expires_at REAL,
                UNIQUE(job_id, telegram_user_id)
            );
            CREATE INDEX IF NOT EXISTS job_items_ready ON job_items(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS job_items_expired_lease ON job_items(lease_expires_at);
            CREATE INDEX IF NOT EXISTS cleanup_ready ON contact_cleanup(state, next_attempt_at);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1')"
        )
        cleanup_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(contact_cleanup)").fetchall()
        }
        if "lease_owner" not in cleanup_columns:
            connection.execute("ALTER TABLE contact_cleanup ADD COLUMN lease_owner TEXT")
        if "lease_expires_at" not in cleanup_columns:
            connection.execute("ALTER TABLE contact_cleanup ADD COLUMN lease_expires_at REAL")
        item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(job_items)").fetchall()}
        if "api_status" not in item_columns:
            connection.execute("ALTER TABLE job_items ADD COLUMN api_status TEXT NOT NULL DEFAULT ''")
        if "manual_confirmation" not in item_columns:
            connection.execute(
                "ALTER TABLE job_items ADD COLUMN manual_confirmation TEXT NOT NULL DEFAULT 'NOT_CONFIRMED'"
            )
        if "manual_confirmed_at" not in item_columns:
            connection.execute("ALTER TABLE job_items ADD COLUMN manual_confirmed_at REAL")
        if "import_retry_count" not in item_columns:
            connection.execute("ALTER TABLE job_items ADD COLUMN import_retry_count INTEGER NOT NULL DEFAULT 0")
        if "item_id" not in cleanup_columns:
            connection.execute("ALTER TABLE contact_cleanup ADD COLUMN item_id INTEGER REFERENCES job_items(id)")
        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "import_success_streak" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN import_success_streak INTEGER NOT NULL DEFAULT 0")
        worker_columns = {row["name"] for row in connection.execute("PRAGMA table_info(worker_state)")}
        if "import_batches_since_cleanup" not in worker_columns:
            connection.execute("ALTER TABLE worker_state ADD COLUMN import_batches_since_cleanup INTEGER NOT NULL DEFAULT 0")

    def record_item_attempt(
        self, item_id: int, method: str, outcome: str, detail: str = "",
        source: str = "TELEGRAM", *, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Append outcome evidence; callers can include it in their routing transaction."""
        if connection is None:
            with self._connect() as owned_connection:
                self.record_item_attempt(item_id, method, outcome, detail, source, connection=owned_connection)
            return
        connection.execute(
            """INSERT INTO job_item_attempts(item_id, created_at, method, outcome, detail, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, self.now(), method, outcome, detail, source),
        )

    def item_attempts(self, job_id: str, item_id: str) -> list[dict]:
        """Read append-only evidence only for an item belonging to this job."""
        try:
            parsed_item_id = int(item_id)
        except (TypeError, ValueError):
            return []
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT a.* FROM job_item_attempts a JOIN job_items i ON i.id = a.item_id
                   WHERE i.job_id = ? AND i.id = ? ORDER BY a.id""",
                (job_id, parsed_item_id),
            )]

    @staticmethod
    def _active_claim_count(connection: sqlite3.Connection, job_id: str) -> int:
        # Even an expired or unleased IN_PROGRESS row requires recovery first.
        return connection.execute(
            """SELECT (SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status = 'IN_PROGRESS')
                 + (SELECT COUNT(*) FROM contact_cleanup WHERE job_id = ? AND state = 'IN_PROGRESS')""",
            (job_id, job_id),
        ).fetchone()[0]

    @staticmethod
    def _requeue_block_reason(state: str, active_claims: int) -> str:
        if state != 'PAUSED':
            return 'Возврат в очередь доступен только для задачи в состоянии PAUSED.'
        if active_claims:
            return f'Активные запросы Telegram: {active_claims}. Дождитесь завершения перед возвратом в очередь.'
        return ''

    def requeue_legacy_direct_rows(self, job_id: str) -> int:
        """Explicitly migrate unconfirmed legacy direct rows without resuming."""
        return self._requeue_review_rows(job_id, unknown_outcomes=False)

    def requeue_unknown_outcomes(self, job_id: str) -> int:
        """Operator-approved item retries; unknown cleanup records stay untouched."""
        return self._requeue_review_rows(job_id, unknown_outcomes=True)

    def _requeue_review_rows(self, job_id: str, *, unknown_outcomes: bool) -> int:
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            job = connection.execute('SELECT state FROM jobs WHERE id = ?', (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            reason = self._requeue_block_reason(job['state'], self._active_claim_count(connection, job_id))
            if reason:
                raise ValueError(reason)
            row_filter = "status = 'UNKNOWN_OUTCOME_REVIEW'" if unknown_outcomes else LEGACY_DIRECT_REQUEUE_FILTER
            rows = connection.execute(
                'SELECT * FROM job_items WHERE job_id = ? AND ' + row_filter + ' ORDER BY id', (job_id,),
            ).fetchall()
            timestamp = self.now()
            detail = (
                'Оператор разрешил повтор запроса с неизвестным результатом через импорт.'
                if unknown_outcomes else 'Старая прямая проверка возвращена в очередь импорта.'
            )
            for row in rows:
                self.record_item_attempt(
                    row['id'], row['method'], 'UNKNOWN_OUTCOME_REQUEUED' if unknown_outcomes else row['status'],
                    detail, 'OPERATOR' if unknown_outcomes else 'LEGACY_DIRECT_MIGRATION', connection=connection,
                )
                connection.execute(
                    """UPDATE job_items SET status = 'PENDING_RETRY', method = 'IMPORT',
                       api_status = CASE WHEN api_status = '' THEN ? ELSE api_status END,
                       detail = ?, next_attempt_at = ?, completed_at = NULL,
                       lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    ('UNKNOWN_OUTCOME' if unknown_outcomes else row['status'], detail, timestamp + 60, row['id']),
                )
            message = f'Возвращено в очередь импорта: {len(rows)}. Обработка остаётся приостановленной.'
            connection.execute('UPDATE jobs SET phase = ?, message = ?, updated_at = ? WHERE id = ?',
                               ('IMPORT', message, timestamp, job_id))
            self._event(connection, job_id, 'UNKNOWN_OUTCOMES_REQUEUED' if unknown_outcomes else 'LEGACY_DIRECT_REQUEUED', message)
            connection.commit()
            return len(rows)

    @staticmethod
    def _release_account(connection: sqlite3.Connection, claim: dict) -> None:
        connection.execute(
            "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
            (claim["account_key"], claim["owner"]),
        )

    def _claim_and_duplicates(self, connection: sqlite3.Connection, claim: dict) -> list[sqlite3.Row]:
        if not connection.execute(
            "SELECT 1 FROM job_items WHERE id = ? AND status = 'IN_PROGRESS' AND lease_owner = ?",
            (claim["id"], claim["owner"]),
        ).fetchone():
            return []
        return connection.execute(
            """SELECT * FROM job_items WHERE id = ? OR (job_id = ? AND normalized_phone = ?
               AND status IN ('PENDING', 'RETRYABLE_ERROR', 'PENDING_RETRY')) ORDER BY id""",
            (claim["id"], claim["job_id"], claim["phone"]),
        ).fetchall()

    def defer_import_retries(
        self, claims: list[dict], reason: str, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Keep retry_contacts in IMPORT with a persisted per-phone retry schedule."""
        if not claims:
            return
        if connection is None:
            with self._connect() as owned_connection:
                owned_connection.execute('BEGIN IMMEDIATE')
                self.defer_import_retries(claims, reason, connection=owned_connection)
                owned_connection.commit()
            return
        timestamp = self.now()
        detail = 'Telegram отложил импорт; номер сохранён для повторного импорта.'
        changed_jobs = set()
        for claim in claims:
            for row in self._claim_and_duplicates(connection, claim):
                retry_number = row['import_retry_count'] + 1
                delay = (60, 300, 900)[min(retry_number - 1, 2)]
                self.record_item_attempt(row['id'], 'IMPORT', 'IMPORT_RETRY', detail, connection=connection)
                connection.execute(
                    """UPDATE job_items SET status = 'PENDING_RETRY', method = 'IMPORT', detail = ?,
                       import_retry_count = ?, next_attempt_at = ?, lease_owner = NULL,
                       lease_expires_at = NULL WHERE id = ?""",
                    (detail, retry_number, timestamp + delay, row['id']),
                )
                changed_jobs.add(claim['job_id'])
        for job_id in changed_jobs:
            connection.execute(
                "UPDATE jobs SET import_retry_count = import_retry_count + 1, import_success_streak = 0, updated_at = ? WHERE id = ?",
                (timestamp, job_id),
            )
            self._event(connection, job_id, 'IMPORT_RETRY', detail)
        self._release_account(connection, claims[0])

    def defer_unknown_outcome(self, claims: list[dict], method: str) -> None:
        """Quarantine a possibly dispatched request until explicit operator review."""
        if not claims:
            return
        detail = 'Результат отправленного запроса неизвестен; требуется решение оператора.'
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed_jobs = set()
            for claim in claims:
                for row in self._claim_and_duplicates(connection, claim):
                    self.record_item_attempt(row['id'], method, 'UNKNOWN_OUTCOME', detail, connection=connection)
                    connection.execute(
                        """UPDATE job_items SET status = 'UNKNOWN_OUTCOME_REVIEW', detail = ?,
                           next_attempt_at = NULL, lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                        (detail, row['id']),
                    )
                    changed_jobs.add(claim['job_id'])
            for job_id in changed_jobs:
                connection.execute("UPDATE jobs SET import_success_streak = 0, message = ?, updated_at = ? WHERE id = ?", (detail, self.now(), job_id))
                self._event(connection, job_id, 'UNKNOWN_OUTCOME', detail)
            self._release_account(connection, claims[0])
            connection.commit()

    def pause_for_authorization(self, claims: list[dict]) -> None:
        """Release undelivered work without counting an ordinary request failure."""
        if not claims:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for claim in claims:
                connection.execute(
                    """UPDATE job_items SET status = ?, detail = 'Требуется авторизация Telegram.',
                       lease_owner = NULL, lease_expires_at = NULL WHERE id = ?
                       AND status = 'IN_PROGRESS' AND lease_owner = ?""",
                    (claim.get('status', 'PENDING'), claim['id'], claim['owner']),
                )
            self._pause_authorization_connection(connection, claims[0])
            connection.commit()

    def _pause_authorization_connection(self, connection: sqlite3.Connection, claim: dict) -> None:
        connection.execute(
            """UPDATE jobs SET state = 'WAITING_AUTHORIZATION', message = 'Требуется авторизация Telegram; затем явное возобновление.',
               run_lease_owner = NULL, run_lease_expires_at = NULL, updated_at = ?
               WHERE state IN ('QUEUED', 'RUNNING', 'WAITING_RATE_LIMIT')""",
            (self.now(),),
        )
        self._release_account(connection, claim)
        self._event(connection, claim['job_id'], 'WAITING_AUTHORIZATION', 'Требуется авторизация Telegram; затем явное возобновление.')

    def record_import_batch_success(
        self, job_id: str, *, connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self._connect() as owned_connection:
                self.record_import_batch_success(job_id, connection=owned_connection)
            return
        connection.execute(
            """UPDATE jobs SET import_batch_size = CASE WHEN import_success_streak >= 4
                   THEN CASE import_batch_size WHEN 1 THEN 5 ELSE 10 END ELSE import_batch_size END,
               import_success_streak = CASE WHEN import_success_streak >= 4 THEN 0 ELSE import_success_streak + 1 END,
               updated_at = ? WHERE id = ?""",
            (self.now(), job_id),
        )

    def cleanup_due(self, account_key: str = 'default') -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT import_batches_since_cleanup FROM worker_state WHERE account_key = ?", (account_key,)).fetchone()
        return bool(row and row[0] >= 5)

    def create_job(self, raw: str) -> str:
        job_id = uuid4().hex
        timestamp = self.now()
        entries = parse_company_phone_batch(raw, deduplicate=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO jobs(id, raw_input, state, phase, message, created_at, updated_at, total_rows)
                   VALUES (?, ?, 'STAGED', 'IMPORT', 'Задача поставлена в очередь.', ?, ?, ?)""",
                (job_id, raw, timestamp, timestamp, len(entries)),
            )
            for ordinal, entry in enumerate(entries):
                phone = entry["phone"]
                status = "PENDING" if is_valid_phone(phone) else "INVALID_PHONE"
                detail = "" if status == "PENDING" else "Введите номер в международном формате, например +79991234567."
                completed_at = None if status == "PENDING" else timestamp
                connection.execute(
                    """INSERT INTO job_items(job_id, ordinal, company_name, company_link, raw_phone,
                       normalized_phone, status, method, detail, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'IMPORT', ?, ?)""",
                    (job_id, ordinal, entry["company_name"], entry["company_link"], phone, phone, status, detail, completed_at),
                )
            self._event(connection, job_id, "CREATED", "Задача поставлена в очередь.")
            connection.commit()
        return job_id

    def migrate_json(self, source: Path) -> None:
        """Import the former JSON snapshots once, without modifying their source."""
        try:
            contents = source.read_bytes()
            checksum = hashlib.sha256(contents).hexdigest()
            state = json.loads(contents.decode("utf-8"))
            legacy_jobs = state.get("jobs")
            if not isinstance(legacy_jobs, dict):
                raise ValueError("Файл очереди не содержит jobs.")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._set_migration_diagnostic(
                "Не удалось перенести JSON-очередь. Исходный файл сохранён без изменений."
            )
            return
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = f"json_migrated:{checksum}"
            if connection.execute("SELECT 1 FROM schema_meta WHERE key = ?", (marker,)).fetchone():
                connection.commit()
                return
            for legacy_id, legacy in legacy_jobs.items():
                if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (legacy_id,)).fetchone():
                    continue
                raw = str(legacy.get("raw", ""))
                # The legacy UI used its default de-duplicating parser. Keep its
                # persisted row count exactly during migration; new jobs retain
                # every ordinal through create_job(..., deduplicate=False).
                entries = parse_company_phone_batch(raw)
                # A legacy queue had no durable explicit-start state.  Do not make
                # network requests merely because the service is upgraded: retain
                # every row and let the operator start the migrated job explicitly.
                resume_after = legacy.get("waiting_until")
                state_name = "STAGED"
                connection.execute(
                    """INSERT INTO jobs(id, raw_input, state, phase, message, created_at, updated_at,
                       total_rows, resume_after)
                       VALUES (?, ?, ?, 'IMPORT', 'Задача перенесена из JSON; ожидает явного запуска.', ?, ?, ?, ?)""",
                    (legacy_id, raw, state_name, timestamp, timestamp, len(entries), resume_after),
                )
                results = legacy.get("results_by_phone", {})
                deferred = set(legacy.get("deferred_phones", []))
                skip_deferred = bool(legacy.get("skip_deferred"))
                for ordinal, entry in enumerate(entries):
                    phone = entry["phone"]
                    result = results.get(phone, {})
                    status, method, detail = "PENDING", "IMPORT", ""
                    fields: dict[str, str] = {}
                    if not is_valid_phone(phone):
                        status, method, detail = "INVALID_PHONE", "NONE", "Введите номер в международном формате, например +79991234567."
                    elif result.get("skipped") or (skip_deferred and phone in deferred):
                        status, method, detail = "DEFERRED_NOT_CHECKED", "NONE", "Проверка отложена пользователем."
                    elif result.get("id"):
                        username = result.get("username") or ""
                        status, method = ("FOUND" if username else "FOUND_NO_USERNAME"), "MIGRATED"
                        name = " ".join(filter(None, [result.get("first_name"), result.get("last_name")]))
                        fields = {
                            "telegram_id": str(result["id"]), "username": username, "name": name,
                            "telegram_link": f"https://t.me/{username}" if username else f"https://t.me/{phone}?profile",
                        }
                    elif result.get("retry") or phone in deferred:
                        method = "DIRECT_LOOKUP"
                    elif str(result.get("error", "")).startswith("No response"):
                        status, method, detail = "UNRESOLVABLE_OR_PRIVACY", "MIGRATED", "Telegram не раскрыл номер; результат может зависеть от настроек приватности."
                    elif result.get("error"):
                        status, method, detail = "RETRYABLE_ERROR", "IMPORT", "Сохранена ошибка старого запроса Telegram; требуется повторная проверка."
                    completed_at = timestamp if status in TERMINAL_STATUSES else None
                    connection.execute(
                        """INSERT INTO job_items(job_id, ordinal, company_name, company_link, raw_phone,
                           normalized_phone, status, method, detail, telegram_id, username, name,
                           telegram_link, completed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (legacy_id, ordinal, entry["company_name"], entry["company_link"], phone, phone,
                         status, method, detail, fields.get("telegram_id", ""), fields.get("username", ""),
                         fields.get("name", ""), fields.get("telegram_link", ""), completed_at),
                    )
                    item_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                    self.record_item_attempt(item_id, method, status, detail, 'JSON_MIGRATION', connection=connection)
                self._event(connection, legacy_id, "JSON_MIGRATED", "Очередь перенесена из JSON.")
            connection.execute("INSERT INTO schema_meta(key, value) VALUES (?, ?)", (marker, str(timestamp)))
            connection.execute("DELETE FROM schema_meta WHERE key = 'migration_diagnostic'")
            connection.commit()

    def _set_migration_diagnostic(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('migration_diagnostic', ?)",
                (message,),
            )

    def migration_diagnostic(self) -> dict[str, str | bool]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'migration_diagnostic'"
            ).fetchone()
        return {"active": bool(row), "message": row["value"] if row else ""}

    def _event(self, connection: sqlite3.Connection, job_id: str, kind: str, message: str) -> None:
        counts = connection.execute(
            "SELECT COUNT(*) AS total, SUM(status IN (%s)) AS completed FROM job_items WHERE job_id = ?"
            % ",".join("?" for _ in TERMINAL_STATUSES),
            (*TERMINAL_STATUSES, job_id),
        ).fetchone()
        connection.execute(
            "INSERT INTO job_events(job_id, created_at, kind, message, processed, total) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, self.now(), kind, message, int(counts["completed"] or 0), counts["total"]),
        )

    def events(self, job_id: str) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            )]

    def result_summary(self, job_id: str) -> dict[str, int]:
        """Count current API outcomes and unresolved work, never infer absence."""
        with self._connect() as connection:
            job = connection.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            return self._result_summary(connection, job_id, job['state'])

    @staticmethod
    def _result_summary(connection: sqlite3.Connection, job_id: str, state: str) -> dict[str, int]:
        rows = connection.execute(
            'SELECT status, method, COUNT(*) AS count FROM job_items WHERE job_id=? GROUP BY status, method', (job_id,),
        ).fetchall()
        return {
            'api_confirmed': sum(row['count'] for row in rows if row['status'] in {'FOUND', 'FOUND_NO_USERNAME'}),
            'pending_retry': sum(row['count'] for row in rows if row['status'] == 'PENDING_RETRY'),
            'privacy_ambiguous': sum(row['count'] for row in rows if row['status'] == 'UNRESOLVABLE_OR_PRIVACY'),
            'pending_import': sum(row['count'] for row in rows if row['method'] == 'IMPORT' and row['status'] in {'PENDING', 'RETRYABLE_ERROR', 'IN_PROGRESS'}),
            'waiting_authorization': sum(row['count'] for row in rows if row['status'] not in TERMINAL_STATUSES) if state == 'WAITING_AUTHORIZATION' else 0,
            'unknown_outcome_review': sum(row['count'] for row in rows if row['status'] == 'UNKNOWN_OUTCOME_REVIEW'),
        }

    def snapshot(self, job_id: str) -> dict | None:
        timestamp = self.now()
        with self._connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                return None
            counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM job_items WHERE job_id = ? GROUP BY status", (job_id,)
            ).fetchall()
            active_claims = self._active_claim_count(connection, job_id)
            legacy_direct_count = connection.execute(
                'SELECT COUNT(*) FROM job_items WHERE job_id = ? AND ' + LEGACY_DIRECT_REQUEUE_FILTER, (job_id,),
            ).fetchone()[0]
            summary = self._result_summary(connection, job_id, job['state'])
            cleanup_wait = connection.execute(
                "SELECT COUNT(*) FROM contact_cleanup WHERE job_id=? AND (state='UNKNOWN_OUTCOME_REVIEW' OR (state='PENDING' AND attempts > 0))", (job_id,),
            ).fetchone()[0]
            account = connection.execute(
                "SELECT next_available_at FROM worker_state WHERE account_key='default'",
            ).fetchone()
            account_wait_until = account['next_available_at'] if account and account['next_available_at'] > timestamp else None
            cleanup_rows = connection.execute(
                """SELECT state, last_error, next_attempt_at, COUNT(*) AS count FROM contact_cleanup
                   WHERE job_id=? AND (state='UNKNOWN_OUTCOME_REVIEW'
                   OR (state='PENDING' AND (attempts > 0 OR last_error != '')))
                   GROUP BY state, last_error, next_attempt_at ORDER BY next_attempt_at, state, last_error""", (job_id,),
            ).fetchall()
        safe_cleanup_reasons = {
            'Ошибка запроса очистки Telegram; повтор запланирован.',
            'Telegram ограничил очистку контактов.',
            'Результат очистки неизвестен; требуется решение оператора.',
            'После перезапуска результат отправленного запроса неизвестен; требуется решение оператора.',
            'Требуется авторизация Telegram.',
        }
        cleanup_waits = [{
            'state': row['state'], 'count': row['count'],
            'reason': row['last_error'] if row['last_error'] in safe_cleanup_reasons else 'Очистка контактов ожидает повторной проверки оператором.',
            'next_attempt_at': max(row['next_attempt_at'], account_wait_until or 0) if row['next_attempt_at'] is not None else None,
        } for row in cleanup_rows]
        wait_deadlines = [deadline for deadline in (job['resume_after'], account_wait_until)
                          if deadline is not None and deadline > timestamp]
        effective_wait_until = max(wait_deadlines) if wait_deadlines else None
        by_status = {row["status"]: row["count"] for row in counts}
        processed = sum(by_status.get(status, 0) for status in TERMINAL_STATUSES)
        confirmed = by_status.get("FOUND", 0) + by_status.get("FOUND_NO_USERNAME", 0)
        started_at = job["started_at"]
        eta_seconds = None
        uncertain_wait = (
            summary['pending_retry'] or summary['unknown_outcome_review']
            or by_status.get('RETRYABLE_ERROR', 0) or cleanup_wait
            or job['state'] in {'WAITING_AUTHORIZATION', 'WAITING_RATE_LIMIT'}
            or effective_wait_until is not None
        )
        if started_at and processed and not uncertain_wait:
            rate = processed / max(timestamp - started_at, 0.001)
            eta_seconds = max(0, round((job["total_rows"] - processed) / rate))
        phase = job["phase"]
        phase_total = phase_remaining = 0
        if phase in {"IMPORT", "DIRECT_LOOKUP"}:
            method_clause = "method = 'DIRECT_LOOKUP'" if phase == "DIRECT_LOOKUP" else "method != 'DIRECT_LOOKUP'"
            with self._connect() as connection:
                phase_total = connection.execute(
                    "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND " + method_clause,
                    (job_id,),
                ).fetchone()[0]
                phase_remaining = connection.execute(
                    "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND " + method_clause +
                    " AND status NOT IN (%s)" % ",".join("?" for _ in TERMINAL_STATUSES),
                    (job_id, *TERMINAL_STATUSES),
                ).fetchone()[0]
        return {
            **dict(job), "total": job["total_rows"], "processed": processed,
            "active_claims": active_claims, "legacy_direct_count": legacy_direct_count,
            "requeue_block_reason": self._requeue_block_reason(job['state'], active_claims),
            "confirmed": confirmed, "counts": by_status, "phase_remaining": phase_remaining,
            "result_summary": summary,
            "account_wait_until": account_wait_until, "effective_wait_until": effective_wait_until,
            "cleanup_waits": cleanup_waits,
            "phase_total": phase_total, "phase_processed": phase_total - phase_remaining,
            "items_per_minute": round(processed / max(timestamp - started_at, 0.001) * 60, 1) if started_at and processed else None,
            "eta_seconds": eta_seconds,
        }

    def list_jobs(self) -> list[dict]:
        """Return durable jobs for the local operator's queue overview."""
        with self._connect() as connection:
            job_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM jobs ORDER BY updated_at DESC, created_at DESC"
                ).fetchall()
            ]
        return [snapshot for job_id in job_ids if (snapshot := self.snapshot(job_id))]

    def rows(self, job_id: str) -> list[dict]:
        with self._connect() as connection:
            items = connection.execute(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY ordinal", (job_id,)
            ).fetchall()
        rows = []
        for item in items:
            status = item["status"]
            if status in {"FOUND", "FOUND_NO_USERNAME"}:
                rendered = {
                    "phone": item["normalized_phone"], "status": status,
                    "telegram_id": item["telegram_id"], "username": item["username"],
                    "name": item["name"], "telegram_link": item["telegram_link"], "detail": item["detail"],
                }
            else:
                phone_link = (
                    f"https://t.me/+{item['normalized_phone'].lstrip('+')}?profile"
                    if status == "UNRESOLVABLE_OR_PRIVACY" and is_valid_phone(item["normalized_phone"])
                    else ""
                )
                rendered = {
                    "phone": item["normalized_phone"], "status": status,
                    "telegram_id": "", "username": "", "name": "", "telegram_link": phone_link,
                    "detail": item["detail"],
                }
            rows.append(
                {
                    "_item_id": str(item["id"]), "method": item["method"],
                    "next_attempt_at": item["next_attempt_at"], "attempts": item["attempts"],
                    "company_name": item["company_name"], "company_link": item["company_link"],
                    "api_status": item["api_status"] or status,
                    "manual_confirmation": item["manual_confirmation"] or "NOT_CONFIRMED",
                    **rendered,
                }
            )
        return rows

    def confirm_manual_chat(self, job_id: str, item_id: str) -> bool:
        """Record an operator's observed chat opening without rewriting API evidence."""
        try:
            parsed_item_id = int(item_id)
        except (TypeError, ValueError):
            return False
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE job_items SET manual_confirmation = 'MANUAL_CHAT_CONFIRMED',
                   manual_confirmed_at = ? WHERE id = ? AND job_id = ?
                   AND status = 'UNRESOLVABLE_OR_PRIVACY'""",
                (timestamp, parsed_item_id, job_id),
            ).rowcount
            if changed:
                self._event(connection, job_id, "MANUAL_CHAT_CONFIRMED", "Оператор подтвердил открытие чата.")
            connection.commit()
            return bool(changed)

    def _reconcile_connection(self, connection: sqlite3.Connection, timestamp: float) -> None:
        """Release only expired leases and wake elapsed rate-limit waits."""
        expired_items = connection.execute(
            """SELECT * FROM job_items WHERE status = 'IN_PROGRESS'
               AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
            (timestamp,),
        ).fetchall()
        recovered_job_ids = set()
        detail = 'После перезапуска результат отправленного запроса неизвестен; требуется решение оператора.'
        for item in expired_items:
            claim = {**dict(item), 'phone': item['normalized_phone'], 'owner': item['lease_owner']}
            for row in self._claim_and_duplicates(connection, claim):
                self.record_item_attempt(row['id'], row['method'], 'UNKNOWN_OUTCOME', detail, 'RECOVERY', connection=connection)
                connection.execute(
                    """UPDATE job_items SET status = 'UNKNOWN_OUTCOME_REVIEW', detail = ?,
                       lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL WHERE id = ?""",
                    (detail, row['id']),
                )
            recovered_job_ids.add(item['job_id'])
        expired_cleanup = connection.execute(
            """SELECT * FROM contact_cleanup WHERE state = 'IN_PROGRESS'
               AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
            (timestamp,),
        ).fetchall()
        for cleanup in expired_cleanup:
            claim = {'cleanup_ids': [cleanup['id']], 'owner': cleanup['lease_owner']}
            self._record_cleanup_attempts(connection, claim, 'UNKNOWN_OUTCOME', detail, source='RECOVERY')
            connection.execute(
                """UPDATE contact_cleanup SET state = 'UNKNOWN_OUTCOME_REVIEW', lease_owner = NULL,
                   lease_expires_at = NULL, next_attempt_at = NULL, last_error = ? WHERE id = ?""",
                (detail, cleanup['id']),
            )
            recovered_job_ids.add(cleanup['job_id'])
        connection.execute(
            """UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
               WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
            (timestamp, timestamp),
        )
        waiting = connection.execute(
            """SELECT id FROM jobs WHERE state = 'WAITING_RATE_LIMIT'
               AND resume_after IS NOT NULL AND resume_after <= ?""",
            (timestamp,),
        ).fetchall()
        connection.execute(
            """UPDATE jobs SET state = 'WAITING_RATE_LIMIT', run_lease_owner = NULL,
               run_lease_expires_at = NULL, updated_at = ?
               WHERE state = 'RUNNING' AND run_lease_expires_at IS NOT NULL
               AND run_lease_expires_at <= ? AND resume_after IS NOT NULL AND resume_after > ?""",
            (timestamp, timestamp, timestamp),
        )
        stale_running = connection.execute(
            """SELECT id FROM jobs WHERE state = 'RUNNING'
               AND run_lease_expires_at IS NOT NULL AND run_lease_expires_at <= ?
               AND (resume_after IS NULL OR resume_after <= ?)""",
            (timestamp, timestamp),
        ).fetchall()
        connection.execute(
            """UPDATE jobs SET state = 'QUEUED', run_lease_owner = NULL, run_lease_expires_at = NULL,
               message = 'Задача восстановлена после перезапуска.', updated_at = ?
               WHERE state = 'RUNNING' AND run_lease_expires_at IS NOT NULL
               AND run_lease_expires_at <= ? AND (resume_after IS NULL OR resume_after <= ?)""",
            (timestamp, timestamp, timestamp),
        )
        connection.execute(
            """UPDATE jobs SET state = 'QUEUED', resume_after = NULL,
               message = 'Ожидание Telegram завершено; задача возобновлена.', updated_at = ?
               WHERE state = 'WAITING_RATE_LIMIT' AND resume_after IS NOT NULL AND resume_after <= ?""",
            (timestamp, timestamp),
        )
        for job in stale_running:
            if job['id'] not in recovered_job_ids:
                self._event(connection, job["id"], "RECOVERED", "Незавершённая работа восстановлена.")
        for job in waiting:
            self._event(connection, job["id"], "RATE_LIMIT_EXPIRED", "Ожидание Telegram завершено; задача возобновлена.")
        for job_id in recovered_job_ids:
            connection.execute("UPDATE jobs SET import_success_streak = 0, message = ?, updated_at = ? WHERE id = ?", (detail, timestamp, job_id))
            self._event(connection, job_id, 'UNKNOWN_OUTCOME', detail)

    def reconcile(self) -> None:
        """Recover work after a process crash without touching live leases."""
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_connection(connection, timestamp)
            connection.commit()

    def claim_next(self, owner: str, account_key: str = "default") -> dict | None:
        """Claim one ready item and the account lease before a Telegram request."""
        claims = self.claim_batch(owner, limit=1, account_key=account_key)
        return claims[0] if claims else None

    def claim_batch(self, owner: str, limit: int, account_key: str = "default") -> list[dict]:
        """Claim one direct lookup or up to ten unique import phones from one job."""
        timestamp = self.now()
        expires_at = timestamp + 60
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_connection(connection, timestamp)
            worker = connection.execute(
                "SELECT * FROM worker_state WHERE account_key = ?", (account_key,)
            ).fetchone()
            if worker and (
                worker["next_available_at"] > timestamp
                or (worker["lease_owner"] and worker["lease_owner"] != owner and worker["lease_expires_at"] > timestamp)
            ):
                connection.commit()
                return []
            connection.execute(
                """INSERT INTO worker_state(account_key, lease_owner, lease_expires_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_key) DO UPDATE SET lease_owner = excluded.lease_owner,
                   lease_expires_at = excluded.lease_expires_at, updated_at = excluded.updated_at""",
                (account_key, owner, expires_at, timestamp),
            )
            ready = """(i.status IN ('PENDING', 'RETRYABLE_ERROR') OR
                    (i.status = 'PENDING_RETRY' AND i.next_attempt_at IS NOT NULL))
                AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= ?)
                AND j.state IN ('QUEUED', 'RUNNING')
                AND NOT EXISTS (
                    SELECT 1 FROM job_items earlier
                    WHERE earlier.job_id = i.job_id
                    AND earlier.normalized_phone = i.normalized_phone
                    AND earlier.id < i.id
                    AND earlier.status NOT IN (%s)
                )""" % ",".join("?" for _ in TERMINAL_STATUSES)
            direct = None
            if not worker or worker["next_direct_lookup_at"] <= timestamp:
                direct = connection.execute(
                    """SELECT i.*, j.import_batch_size FROM job_items i JOIN jobs j ON j.id = i.job_id
                       WHERE i.method = 'DIRECT_LOOKUP' AND """ + ready + " ORDER BY i.id LIMIT 1",
                    (timestamp, *TERMINAL_STATUSES),
                ).fetchone()
            item = direct or connection.execute(
                """SELECT i.*, j.import_batch_size FROM job_items i JOIN jobs j ON j.id = i.job_id
                   WHERE i.method != 'DIRECT_LOOKUP' AND """ + ready + " ORDER BY i.id LIMIT 1",
                (timestamp, *TERMINAL_STATUSES),
            ).fetchone()
            if not item:
                connection.execute("UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ?", (account_key,))
                connection.commit()
                return []
            batch_limit = 1 if item["method"] == "DIRECT_LOOKUP" else min(max(1, int(limit)), 10, item["import_batch_size"])
            items = connection.execute(
                """SELECT i.*, j.import_batch_size FROM job_items i JOIN jobs j ON j.id = i.job_id
                   WHERE i.job_id = ? AND i.method = ? AND """ + ready + " ORDER BY i.id LIMIT ?",
                (item["job_id"], item["method"], timestamp, *TERMINAL_STATUSES, batch_limit),
            ).fetchall()
            for candidate in items:
                connection.execute(
                    """UPDATE job_items SET status = 'IN_PROGRESS', lease_owner = ?, lease_expires_at = ?,
                       started_at = COALESCE(started_at, ?) WHERE id = ?""",
                    (owner, expires_at, timestamp, candidate["id"]),
                )
            if item["method"] == "DIRECT_LOOKUP":
                connection.execute(
                    "UPDATE worker_state SET next_direct_lookup_at = ? WHERE account_key = ?",
                    (timestamp + 3.1, account_key),
                )
            else:
                connection.execute(
                    "UPDATE worker_state SET next_import_at = ? WHERE account_key = ?",
                    (timestamp, account_key),
                )
            connection.execute(
                """UPDATE jobs SET state = 'RUNNING', phase = ?, run_lease_owner = ?,
                   run_lease_expires_at = ?, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?""",
                (item["method"], owner, expires_at, timestamp, timestamp, item["job_id"]),
            )
            connection.commit()
            return [
                {**dict(candidate), "phone": candidate["normalized_phone"], "owner": owner, "account_key": account_key}
                for candidate in items
            ]

    def finish_claim(self, claim: dict, result: dict) -> None:
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._finish_claim_connection(connection, claim, result, timestamp)
            connection.execute(
                "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
                (claim["account_key"], claim["owner"]),
            )
            connection.commit()

    def _finish_claim_connection(
        self, connection: sqlite3.Connection, claim: dict, result: dict, timestamp: float
    ) -> None:
        mapped = map_lookup_result(claim["phone"], result)
        status = mapped["status"]
        if status == "NOT_FOUND_OR_PRIVACY_RESTRICTED":
            status = "UNRESOLVABLE_OR_PRIVACY"
        values = (
            status, status, claim["method"], mapped["detail"], mapped["telegram_id"], mapped["username"],
            mapped["name"], mapped["telegram_link"], timestamp,
        )
        evidence_rows = self._claim_and_duplicates(connection, claim)
        for row in evidence_rows:
            self.record_item_attempt(row['id'], claim['method'], status, mapped['detail'], connection=connection)
        changed = connection.execute(
            """UPDATE job_items SET status = ?, api_status = ?, method = ?, detail = ?, telegram_id = ?, username = ?,
               name = ?, telegram_link = ?, lease_owner = NULL, lease_expires_at = NULL, completed_at = ?
               WHERE id = ? AND status = 'IN_PROGRESS' AND lease_owner = ?""",
            (*values, claim["id"], claim["owner"]),
        ).rowcount
        if not changed:
            return
        connection.execute(
            """UPDATE job_items SET status = ?, api_status = ?, method = ?, detail = ?, telegram_id = ?, username = ?,
               name = ?, telegram_link = ?, completed_at = ?
               WHERE job_id = ? AND normalized_phone = ? AND id != ?
               AND status IN ('PENDING', 'RETRYABLE_ERROR', 'PENDING_RETRY')""",
            (*values, claim["job_id"], claim["phone"], claim["id"]),
        )
        cleanup_contact_id = result.get("_cleanup_contact_id") or mapped["telegram_id"]
        if claim["method"] == "IMPORT" and cleanup_contact_id:
            connection.execute(
                """INSERT OR IGNORE INTO contact_cleanup(job_id, telegram_user_id, item_id, state, created_at)
                   VALUES (?, ?, ?, 'PENDING', ?)""",
                (claim["job_id"], int(cleanup_contact_id), claim["id"], timestamp),
            )
        self._event(connection, claim["job_id"], "RESULT", "Результат сохранён.")

    def finish_import_batch(self, claims: list[dict], results: dict[str, dict]) -> None:
        """Persist one dispatched import response, including all scheduling effects."""
        self.finish_batch(claims, results, import_response=True)

    def finish_batch(
        self, claims: list[dict], results: dict[str, dict], *, import_response: bool = False,
    ) -> None:
        """Commit outcomes and actual import accounting together; cached results do not count."""
        if not claims:
            return
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            valid_claims = [claim for claim in claims if connection.execute(
                "SELECT 1 FROM job_items WHERE id = ? AND status = 'IN_PROGRESS' AND lease_owner = ?",
                (claim['id'], claim['owner']),
            ).fetchone()]
            retry_claims = []
            missing_result = False
            for claim in valid_claims:
                result = results.get(claim['phone'])
                if result is None:
                    missing_result = True
                    self._retry_claim_connection(connection, claim, RuntimeError('Missing import result'), timestamp)
                elif import_response and result.get('retry'):
                    retry_claims.append(claim)
                else:
                    self._finish_claim_connection(connection, claim, result, timestamp)
            if import_response and valid_claims:
                if retry_claims:
                    self.defer_import_retries(retry_claims, 'retry_contacts', connection=connection)
                    connection.execute(
                        """UPDATE jobs SET import_batch_size = CASE import_batch_size WHEN 10 THEN 5 ELSE 1 END,
                           updated_at = ? WHERE id = ?""",
                        (timestamp, claims[0]['job_id']),
                    )
                elif not missing_result:
                    self.record_import_batch_success(claims[0]['job_id'], connection=connection)
                connection.execute(
                    "UPDATE worker_state SET import_batches_since_cleanup = import_batches_since_cleanup + 1 WHERE account_key = ?",
                    (claims[0]['account_key'],),
                )
            self._release_account(connection, claims[0])
            connection.commit()

    def defer_account_floodwait(self, claims: list[dict], seconds: int) -> None:
        """Persist an account-wide Telegram FloodWait and return claimed work to its queue."""
        if not claims:
            return
        if seconds <= 0:
            raise ValueError("FloodWait must be positive.")
        timestamp = self.now()
        resume_after = timestamp + seconds
        owner = claims[0]["owner"]
        account_key = claims[0]["account_key"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for claim in claims:
                evidence_rows = self._claim_and_duplicates(connection, claim)
                for row in evidence_rows:
                    self.record_item_attempt(row['id'], claim['method'], 'FLOOD_WAIT', f'FloodWait: {seconds} seconds', connection=connection)
                    connection.execute(
                        "UPDATE job_items SET status = 'PENDING_RETRY', next_attempt_at = ?, detail = 'Telegram ограничил запросы; ожидание сохранено.' WHERE id = ? AND status != 'IN_PROGRESS'",
                        (resume_after, row['id']),
                    )
                connection.execute(
                    """UPDATE job_items SET status = 'PENDING_RETRY', next_attempt_at = ?, lease_owner = NULL,
                       lease_expires_at = NULL, detail = 'Telegram ограничил запросы; ожидание сохранено.'
                       WHERE id = ? AND status = 'IN_PROGRESS' AND lease_owner = ?""",
                    (resume_after, claim["id"], owner),
                )
                connection.execute(
                    """UPDATE jobs SET state = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN state ELSE 'WAITING_RATE_LIMIT' END,
                       phase = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN phase ELSE 'WAITING_RATE_LIMIT' END,
                       message = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN message ELSE 'Telegram ограничил запросы; ожидание сохранено.' END,
                       import_success_streak = 0, resume_after = ?,
                       run_lease_owner = NULL, run_lease_expires_at = NULL, updated_at = ?
                       WHERE id = ? AND state NOT IN ('CANCELLED', 'COMPLETED', 'FAILED')""",
                    (resume_after, timestamp, claim["job_id"]),
                )
                self._event(connection, claim["job_id"], "FLOOD_WAIT", "Telegram ограничил запросы; ожидание сохранено.")
            connection.execute(
                """UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL,
                   next_available_at = CASE WHEN next_available_at > ? THEN next_available_at ELSE ? END,
                   updated_at = ? WHERE account_key = ? AND lease_owner = ?""",
                (resume_after, resume_after, timestamp, account_key, owner),
            )
            connection.commit()

    def defer_cleanup_floodwait(self, claim: dict, seconds: int) -> None:
        """Return cleanup work to the same account-wide FloodWait schedule."""
        if seconds <= 0:
            raise ValueError("FloodWait must be positive.")
        timestamp = self.now()
        resume_after = timestamp + seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record_cleanup_attempts(connection, claim, 'FLOOD_WAIT', f'FloodWait: {seconds} seconds')
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'PENDING', next_attempt_at = ?, lease_owner = NULL,
                   lease_expires_at = NULL, last_error = 'Telegram ограничил очистку контактов.'
                   WHERE id = ? AND state = 'IN_PROGRESS' AND lease_owner = ?""",
                [(resume_after, cleanup_id, claim["owner"]) for cleanup_id in claim["cleanup_ids"]],
            )
            connection.execute(
                """UPDATE jobs SET state = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN state ELSE 'WAITING_RATE_LIMIT' END,
                   phase = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN phase ELSE 'WAITING_RATE_LIMIT' END,
                   resume_after = ?, import_success_streak = 0,
                   message = CASE WHEN state IN ('PAUSED', 'WAITING_AUTHORIZATION') THEN message ELSE 'Telegram ограничил очистку контактов; ожидание сохранено.' END,
                   updated_at = ? WHERE id = ? AND state NOT IN ('CANCELLED', 'COMPLETED', 'FAILED')""",
                (resume_after, timestamp, claim["job_id"]),
            )
            connection.execute(
                """UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL,
                   next_available_at = CASE WHEN next_available_at > ? THEN next_available_at ELSE ? END,
                   updated_at = ? WHERE account_key = ? AND lease_owner = ?""",
                (resume_after, resume_after, timestamp, claim["account_key"], claim["owner"]),
            )
            self._event(connection, claim["job_id"], "CLEANUP_FLOOD_WAIT", "Очистка контактов отложена Telegram.")
            connection.commit()

    def renew_claims(self, claims: list[dict], lease_seconds: int = 60) -> None:
        """Extend ownership while a synchronous Telegram request is still running."""
        if not claims:
            return
        timestamp = self.now()
        expires_at = timestamp + lease_seconds
        owner = claims[0]["owner"]
        account_key = claims[0]["account_key"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE worker_state SET lease_expires_at = ?, updated_at = ?
                   WHERE account_key = ? AND lease_owner = ?""",
                (expires_at, timestamp, account_key, owner),
            )
            connection.executemany(
                """UPDATE job_items SET lease_expires_at = ? WHERE id = ?
                   AND status = 'IN_PROGRESS' AND lease_owner = ?""",
                [(expires_at, claim["id"], owner) for claim in claims],
            )
            for job_id in {claim["job_id"] for claim in claims}:
                connection.execute(
                    """UPDATE jobs SET run_lease_expires_at = ?, updated_at = ? WHERE id = ?
                       AND state = 'RUNNING' AND run_lease_owner = ?""",
                    (expires_at, timestamp, job_id, owner),
                )
            connection.commit()

    def renew_cleanup_claim(self, claim: dict, lease_seconds: int = 60) -> None:
        """Extend cleanup ownership while Telegram is deleting imported contacts."""
        timestamp = self.now()
        expires_at = timestamp + lease_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE worker_state SET lease_expires_at = ?, updated_at = ?
                   WHERE account_key = ? AND lease_owner = ?""",
                (expires_at, timestamp, claim["account_key"], claim["owner"]),
            )
            connection.executemany(
                """UPDATE contact_cleanup SET lease_expires_at = ? WHERE id = ?
                   AND state = 'IN_PROGRESS' AND lease_owner = ?""",
                [(expires_at, cleanup_id, claim["owner"]) for cleanup_id in claim["cleanup_ids"]],
            )
            connection.commit()

    def set_import_batch_size(self, job_id: str, size: int) -> None:
        """Persist a bounded adaptive import batch size for the given job."""
        if not 1 <= size <= 10:
            raise ValueError("Import batch size must be between 1 and 10.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET import_batch_size = ?, updated_at = ? WHERE id = ?",
                (size, self.now(), job_id),
            )

    def retry_claim(self, claim: dict, error: Exception) -> None:
        """Release an interrupted request without losing the item or account lease."""
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._retry_claim_connection(connection, claim, error, timestamp)
            connection.execute(
                "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
                (claim["account_key"], claim["owner"]),
            )
            connection.commit()

    def _retry_claim_connection(
        self, connection: sqlite3.Connection, claim: dict, error: Exception, timestamp: float
    ) -> None:
        row = connection.execute(
            "SELECT attempts FROM job_items WHERE id = ? AND status = 'IN_PROGRESS' AND lease_owner = ?",
            (claim["id"], claim["owner"]),
        ).fetchone()
        if not row:
            return
        attempts = row["attempts"] + 1
        status = "API_ERROR_FINAL" if attempts >= 3 else "RETRYABLE_ERROR"
        delay = (30, 120, 600)[min(attempts - 1, 2)]
        detail = 'Ошибка запроса Telegram; номер сохранён для повтора.'
        self.record_item_attempt(claim['id'], claim['method'], status, detail, connection=connection)
        connection.execute("UPDATE jobs SET import_success_streak = 0 WHERE id = ?", (claim['job_id'],))
        connection.execute(
            """UPDATE job_items SET status = ?, detail = ?, next_attempt_at = ?, attempts = attempts + 1, lease_owner = NULL,
               lease_expires_at = NULL, completed_at = ? WHERE id = ? AND lease_owner = ?""",
            (status, detail, timestamp + delay, None if status == "RETRYABLE_ERROR" else timestamp,
             claim["id"], claim["owner"]),
        )
        self._event(connection, claim["job_id"], "RETRYABLE_ERROR", "Временная ошибка Telegram; номер сохранён для повтора.")

    def retry_batch(self, claims: list[dict], error: Exception) -> None:
        if not claims:
            return
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for claim in claims:
                self._retry_claim_connection(connection, claim, error, timestamp)
            connection.execute(
                "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
                (claims[0]["account_key"], claims[0]["owner"]),
            )
            connection.commit()

    def claim_cleanup(self, owner: str, limit: int = 100, account_key: str = "default") -> dict | None:
        return self.claim_cleanup_batch(owner, limit, account_key)

    def claim_cleanup_batch(self, owner: str, limit: int = 100, account_key: str = "default") -> dict | None:
        """Claim a same-job cleanup group without interleaving another Telegram request."""
        timestamp = self.now()
        expires_at = timestamp + 60
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_connection(connection, timestamp)
            worker = connection.execute(
                "SELECT * FROM worker_state WHERE account_key = ?", (account_key,)
            ).fetchone()
            if worker and (
                worker["next_available_at"] > timestamp
                or (worker["lease_owner"] and worker["lease_owner"] != owner and worker["lease_expires_at"] > timestamp)
            ):
                connection.commit()
                return None
            connection.execute(
                """INSERT INTO worker_state(account_key, lease_owner, lease_expires_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_key) DO UPDATE SET lease_owner = excluded.lease_owner,
                   lease_expires_at = excluded.lease_expires_at, updated_at = excluded.updated_at""",
                (account_key, owner, expires_at, timestamp),
            )
            first = connection.execute(
                """SELECT c.* FROM contact_cleanup c JOIN jobs j ON j.id = c.job_id
                   WHERE c.state = 'PENDING' AND j.state IN ('QUEUED', 'RUNNING')
                   AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= ?) ORDER BY c.id LIMIT 1""",
                (timestamp,),
            ).fetchone()
            if not first:
                connection.execute(
                    "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ?",
                    (account_key,),
                )
                connection.commit()
                return None
            rows = connection.execute(
                """SELECT * FROM contact_cleanup WHERE job_id = ? AND state = 'PENDING'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?""",
                (first["job_id"], timestamp, max(1, int(limit))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            connection.execute("UPDATE worker_state SET import_batches_since_cleanup = 0 WHERE account_key = ?", (account_key,))
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'IN_PROGRESS', attempts = attempts + 1,
                   lease_owner = ?, lease_expires_at = ? WHERE id = ?""",
                [(owner, expires_at, cleanup_id) for cleanup_id in ids],
            )
            connection.commit()
            return {
                "id": ids[0], "cleanup_ids": ids, "job_id": first["job_id"],
                "telegram_user_ids": [row["telegram_user_id"] for row in rows],
                "owner": owner, "account_key": account_key,
            }

    def _record_cleanup_attempts(self, connection: sqlite3.Connection, claim: dict, outcome: str, detail: str, source: str = "CLEANUP") -> None:
        rows = connection.execute(
            """SELECT DISTINCT i.id FROM job_items i JOIN contact_cleanup c ON c.job_id = i.job_id
               AND (c.item_id = i.id OR CAST(i.telegram_id AS TEXT) = CAST(c.telegram_user_id AS TEXT))
               WHERE c.id IN (%s) AND c.state = 'IN_PROGRESS' AND c.lease_owner = ?"""
            % ','.join('?' for _ in claim['cleanup_ids']),
            (*claim['cleanup_ids'], claim['owner']),
        ).fetchall()
        for row in rows:
            self.record_item_attempt(row['id'], 'CLEANUP', outcome, detail, source, connection=connection)

    def defer_cleanup_unknown_outcome(self, claim: dict) -> None:
        detail = 'Результат очистки неизвестен; требуется решение оператора.'
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._record_cleanup_attempts(connection, claim, 'UNKNOWN_OUTCOME', detail)
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'UNKNOWN_OUTCOME_REVIEW', last_error = ?,
                   lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL
                   WHERE id = ? AND state = 'IN_PROGRESS' AND lease_owner = ?""",
                [(detail, cleanup_id, claim['owner']) for cleanup_id in claim['cleanup_ids']],
            )
            connection.execute("UPDATE jobs SET import_success_streak = 0, message = ?, updated_at = ? WHERE id = ?", (detail, self.now(), claim['job_id']))
            self._release_account(connection, claim)
            self._event(connection, claim['job_id'], 'UNKNOWN_OUTCOME', detail)
            connection.commit()

    def pause_cleanup_for_authorization(self, claim: dict) -> None:
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'PENDING', attempts = MAX(0, attempts - 1),
                   lease_owner = NULL, lease_expires_at = NULL, last_error = 'Требуется авторизация Telegram.'
                   WHERE id = ? AND state = 'IN_PROGRESS' AND lease_owner = ?""",
                [(cleanup_id, claim['owner']) for cleanup_id in claim['cleanup_ids']],
            )
            self._pause_authorization_connection(connection, claim)
            connection.commit()

    def finish_cleanup(self, claim: dict) -> None:
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record_cleanup_attempts(connection, claim, 'CONTACTS_CLEANED', 'Импортированные контакты удалены.')
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'COMPLETED', completed_at = ?, lease_owner = NULL,
                   lease_expires_at = NULL, last_error = '' WHERE id = ? AND state = 'IN_PROGRESS'
                   AND lease_owner = ?""",
                [(timestamp, cleanup_id, claim["owner"]) for cleanup_id in claim["cleanup_ids"]],
            )
            connection.execute(
                "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
                (claim["account_key"], claim["owner"]),
            )
            self._event(connection, claim["job_id"], "CONTACTS_CLEANED", "Импортированные контакты удалены.")
            connection.commit()

    def retry_cleanup(self, claim: dict, error: Exception) -> None:
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record_cleanup_attempts(connection, claim, 'CLEANUP_RETRY', 'Удаление импортированных контактов будет повторено.')
            connection.execute("UPDATE jobs SET import_success_streak = 0 WHERE id = ?", (claim['job_id'],))
            connection.executemany(
                """UPDATE contact_cleanup SET state = 'PENDING', next_attempt_at = ?, last_error = ?,
                   lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND state = 'IN_PROGRESS'
                   AND lease_owner = ?""",
                [(timestamp + 120, "Ошибка запроса очистки Telegram; повтор запланирован.", cleanup_id, claim["owner"]) for cleanup_id in claim["cleanup_ids"]],
            )
            connection.execute(
                "UPDATE worker_state SET lease_owner = NULL, lease_expires_at = NULL WHERE account_key = ? AND lease_owner = ?",
                (claim["account_key"], claim["owner"]),
            )
            self._event(connection, claim["job_id"], "CONTACT_CLEANUP_RETRY", "Удаление импортированных контактов будет повторено.")
            connection.commit()

    def cancel(self, job_id: str) -> None:
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE job_items SET status = 'CANCELLED', detail = 'Проверка отменена пользователем.',
                   completed_at = ? WHERE job_id = ? AND status IN ('PENDING', 'RETRYABLE_ERROR', 'PENDING_RETRY')""",
                (timestamp, job_id),
            )
            connection.execute(
                "UPDATE jobs SET state = 'CANCELLED', updated_at = ? WHERE id = ?", (timestamp, job_id)
            )
            self._event(connection, job_id, "CANCELLED", "Проверка отменена пользователем.")
            connection.commit()

    def start(self, job_id: str, *, authorization_confirmed: bool = False) -> bool:
        """Resume an authorization wait only after the application checked its session."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE jobs SET state = 'QUEUED', phase = 'IMPORT', message = 'Задача ожидает worker.',
                   started_at = COALESCE(started_at, ?), updated_at = ?
                   WHERE id = ? AND (state IN ('STAGED', 'PAUSED', 'WAITING_RATE_LIMIT')
                   OR (state = 'WAITING_AUTHORIZATION' AND ?))""",
                (self.now(), self.now(), job_id, authorization_confirmed),
            ).rowcount
            if changed:
                self._event(connection, job_id, "STARTED", "Запущена обработка задачи.")
            connection.commit()
            return bool(changed)

    def pause(self, job_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE jobs SET state = 'PAUSED', paused_at = ?, message = 'Обработка приостановлена.', updated_at = ? WHERE id = ? AND state IN ('QUEUED', 'RUNNING', 'WAITING_RATE_LIMIT')",
                (self.now(), self.now(), job_id),
            ).rowcount
            if changed:
                self._event(connection, job_id, "PAUSED", "Обработка приостановлена.")
            connection.commit()
            return bool(changed)

    def finalize_ready_jobs(self) -> None:
        timestamp = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job in connection.execute("SELECT id FROM jobs WHERE state IN ('QUEUED', 'RUNNING')").fetchall():
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM job_items WHERE job_id = ? AND status NOT IN (%s)"
                    % ",".join("?" for _ in TERMINAL_STATUSES),
                    (job["id"], *TERMINAL_STATUSES),
                ).fetchone()[0]
                cleanup_remaining = connection.execute(
                    "SELECT COUNT(*) FROM contact_cleanup WHERE job_id = ? AND state != 'COMPLETED'",
                    (job['id'],),
                ).fetchone()[0]
                if not remaining and not cleanup_remaining:
                    connection.execute(
                        "UPDATE jobs SET state = 'COMPLETED', message = 'Готово.', completed_at = ?, updated_at = ? WHERE id = ?",
                        (timestamp, timestamp, job["id"]),
                    )
                    self._event(connection, job["id"], "COMPLETED", "Задача завершена.")
            connection.commit()
