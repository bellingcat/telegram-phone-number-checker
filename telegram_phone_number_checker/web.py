"""Small local web interface for the Telegram phone-number checker."""

import asyncio
import csv
import html
import inspect
import io
import json
import os
import signal
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from telethon.errors import FloodWaitError

from telegram_phone_number_checker import main as upstream
from telegram_phone_number_checker.web_service import (
    is_valid_phone,
    map_lookup_result,
    parse_company_phone_batch,
)


ROW_FIELDS = (
    "company_name",
    "company_link",
    "phone",
    "status",
    "api_status",
    "manual_confirmation",
    "telegram_id",
    "username",
    "name",
    "telegram_link",
    "detail",
)
BATCH_SIZE = 10
BATCH_PAUSE_SECONDS = 1
RESOLVE_PHONE_PAUSE_SECONDS = 3.1
RETRY_CONTACT_PAUSE_SECONDS = 60
MAX_RETRY_CONTACT_PAUSE_SECONDS = 600
MAX_DEFERRED_RETRY_ATTEMPTS = 3
RETRY_WINDOWS_SECONDS = (15 * 60, 60 * 60, 4 * 60 * 60, 12 * 60 * 60)


class ConfigurationError(Exception):
    """Raised when the local Telegram credentials are not configured."""


class RetryLater(Exception):
    """Stop this run and let the durable scheduler retry after a cooldown."""


def rows_to_csv(rows: list[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=ROW_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: (
                    f"'{row.get(field, '')}"
                    if row.get(field, "").startswith(("=", "+", "-", "@"))
                    else row.get(field, "")
                )
                for field in ROW_FIELDS
            }
        )
    return output.getvalue()


def deferred_rows(raw: str, phones: list[str]) -> list[dict[str, str]]:
    """Return export rows for contacts that Telegram asked to retry later."""
    deferred = set(phones)
    return [
        {
            "company_name": entry["company_name"],
            "company_link": entry["company_link"],
            "phone": entry["phone"],
            "status": "DEFERRED_NOT_CHECKED",
            "telegram_id": "",
            "username": "",
            "name": "",
            "telegram_link": "",
            "detail": "Telegram попросил повторить контакт позже.",
        }
        for entry in parse_company_phone_batch(raw)
        if entry["phone"] in deferred
    ]


async def _check_phone_batch(
    raw: str,
    progress: Callable[[int, int, str], None] | None = None,
    skip_deferred: Callable[[], bool] | None = None,
    deferred: Callable[[list[str]], None] | None = None,
    initial_results: dict[str, dict] | None = None,
    checkpoint: Callable[[dict[str, dict]], None] | None = None,
    schedule_later: Callable[[list[str]], None] | None = None,
) -> list[dict[str, str]]:
    missing = [name for name in ("API_ID", "API_HASH", "PHONE_NUMBER") if not os.getenv(name)]
    if missing:
        raise ConfigurationError(f"Не заданы {', '.join(missing)} в .env")

    entries = parse_company_phone_batch(raw)
    results_by_phone: dict[str, dict] = dict(initial_results or {})
    valid_phones = list(
        dict.fromkeys(
            entry["phone"]
            for entry in entries
            if is_valid_phone(entry["phone"]) and entry["phone"] not in results_by_phone
        )
    )

    def save_checkpoint() -> None:
        if checkpoint:
            checkpoint(dict(results_by_phone))

    def report(processed: int, message: str) -> None:
        if progress:
            progress(processed, len(entries), message)

    def report_deferred(active_phones: list[str] | None = None) -> None:
        if deferred:
            deferred(
                (active_phones or [])
                + [phone for batch, _ in deferred_batches for phone in batch]
            )

    report(
        sum(
            1
            for entry in entries
            if entry["phone"] in results_by_phone or not is_valid_phone(entry["phone"])
        ),
        "Задача принята. Подготавливаем проверку.",
    )

    if valid_phones:
        client = await upstream.login(
            os.getenv("API_ID"), os.getenv("API_HASH"), os.getenv("PHONE_NUMBER")
        )
        try:
            pending_batches = deque(
                (valid_phones[start : start + BATCH_SIZE], 0)
                for start in range(0, len(valid_phones), BATCH_SIZE)
            )
            deferred_batches = deque()

            async def wait_for_deferred(seconds: int) -> bool:
                """Wait for Telegram unless the user stops deferred contacts."""
                if not skip_deferred:
                    await asyncio.sleep(seconds)
                    return True
                remaining = seconds
                while remaining:
                    if skip_deferred():
                        return False
                    step = min(1, remaining)
                    await asyncio.sleep(step)
                    remaining -= step
                return True

            def skip_batches(batches, error: str = "Проверка отложена пользователем.") -> None:
                for deferred_phones, _ in batches:
                    results_by_phone.update(
                        {
                                    phone: {
                                        "skipped": True,
                                        "error": error,
                            }
                            for phone in deferred_phones
                        }
                    )

            async def resolve_deferred_batches(batches) -> None:
                """Resolve retry_contacts one by one without another contact import."""
                phones_to_resolve = [phone for batch, _ in batches for phone in batch]
                report_deferred(phones_to_resolve)
                report(
                    sum(
                        1
                        for entry in entries
                        if entry["phone"] in results_by_phone or not is_valid_phone(entry["phone"])
                    ),
                    (
                        f"Telegram отложил {len(phones_to_resolve)} контактов. "
                        "Проверяем их по одному без импорта контактов."
                    ),
                )
                for index, phone in enumerate(phones_to_resolve):
                    if skip_deferred and skip_deferred():
                        skip_batches(
                            [(phones_to_resolve[index:], 0)],
                            error="Проверка отложена пользователем.",
                        )
                        save_checkpoint()
                        return
                    while True:
                        try:
                            results_by_phone[phone] = await upstream.resolve_phone(client, phone)
                            save_checkpoint()
                            break
                        except FloodWaitError as error:
                            seconds = max(1, int(error.seconds))
                            report(
                                sum(
                                    1
                                    for entry in entries
                                    if entry["phone"] in results_by_phone
                                    or not is_valid_phone(entry["phone"])
                                ),
                                (
                                    "Telegram ограничил прямую проверку. "
                                    f"Ждём {seconds} сек. и продолжим с этого номера."
                                ),
                            )
                            await asyncio.sleep(seconds + 1)
                        except Exception as error:
                            results_by_phone[phone] = {"error": str(error)}
                            save_checkpoint()
                            break
                    report_deferred(phones_to_resolve[index + 1 :])
                    report(
                        sum(
                            1
                            for entry in entries
                            if entry["phone"] in results_by_phone or not is_valid_phone(entry["phone"])
                        ),
                        (
                            f"Прямая проверка отложенных: {index + 1} из "
                            f"{len(phones_to_resolve)}."
                        ),
                    )
                    if index + 1 < len(phones_to_resolve):
                        await asyncio.sleep(RESOLVE_PHONE_PAUSE_SECONDS)

            while pending_batches or deferred_batches:
                if pending_batches:
                    phones, retry_attempt = pending_batches.popleft()
                else:
                    if skip_deferred and skip_deferred():
                        skip_batches(deferred_batches)
                        save_checkpoint()
                        deferred_batches.clear()
                        report_deferred()
                        report(
                            sum(
                                1
                                for entry in entries
                                if entry["phone"] in results_by_phone
                                or not is_valid_phone(entry["phone"])
                            ),
                            "Отложенные контакты сняты с обработки.",
                        )
                        break
                    batches_to_retry = deque(deferred_batches)
                    deferred_batches.clear()
                    await resolve_deferred_batches(batches_to_retry)
                    report_deferred()
                    continue
                while True:
                    try:
                        batch_results = await upstream.get_names_batch(client, phones)
                        retry_phones = [
                            phone for phone, result in batch_results.items() if result.get("retry")
                        ]
                        results_by_phone.update(
                            {
                                phone: result
                                for phone, result in batch_results.items()
                                if phone not in retry_phones
                            }
                        )
                        save_checkpoint()
                        if retry_phones:
                            deferred_batches.append((retry_phones, retry_attempt + 1))
                            report_deferred()
                            report(
                                sum(
                                    1
                                    for entry in entries
                                    if entry["phone"] in results_by_phone
                                    or not is_valid_phone(entry["phone"])
                                ),
                                f"Telegram попросил повторить {len(retry_phones)} контактов. Продолжаем другие пакеты.",
                            )
                        break
                    except RetryLater:
                        raise
                    except FloodWaitError as error:
                        seconds = max(1, int(error.seconds))
                        report(
                            sum(
                                1
                                for entry in entries
                                if entry["phone"] in results_by_phone
                                or not is_valid_phone(entry["phone"])
                            ),
                            f"Telegram ограничил темп. Ждём {seconds} сек. и продолжим этот же пакет.",
                        )
                        await asyncio.sleep(seconds + 1)
                    except Exception as error:  # Other Telegram failures are reported per batch.
                        results_by_phone.update({phone: {"error": str(error)} for phone in phones})
                        save_checkpoint()
                        break
                report_deferred()
                report(
                    sum(
                        1
                        for entry in entries
                        if entry["phone"] in results_by_phone
                        or not is_valid_phone(entry["phone"])
                    ),
                    (
                        f"Проверяем следующий пакет. Отложено к повтору: "
                        f"{sum(len(batch[0]) for batch in deferred_batches)}."
                        if pending_batches
                        else "Собираем результаты."
                    ),
                )
                if pending_batches and BATCH_PAUSE_SECONDS:
                    await asyncio.sleep(BATCH_PAUSE_SECONDS)
        finally:
            disconnected = client.disconnect()
            if inspect.isawaitable(disconnected):
                await disconnected

    rows: list[dict[str, str]] = []
    for entry in entries:
        phone = entry["phone"]
        if is_valid_phone(phone):
            row = map_lookup_result(phone, results_by_phone[phone])
        else:
            row = {
                "phone": phone,
                "status": "INVALID_PHONE",
                "telegram_id": "",
                "username": "",
                "name": "",
                "telegram_link": "",
                "detail": "Введите номер в международном формате, например +79991234567.",
            }
        rows.append({"company_name": entry["company_name"], "company_link": entry["company_link"], **row})
    report(len(rows), "Готово.")
    return rows


def check_phone_batch(raw: str) -> list[dict[str, str]]:
    return asyncio.run(_check_phone_batch(raw))


def render_page(
    rows: list[dict[str, str]] | None = None,
    error: str = "",
    job: dict | None = None,
) -> str:
    rows = rows or []
    job_html = ""
    download_url = "/download.csv"
    refresh_html = ""
    input_text = ""
    if job:
        input_text = job.get("raw", "")
        if job["complete"]:
            rows = job["rows"]
            error = job["error"]
            download_url = f"/download.csv?job={job['id']}"
        elif job.get("waiting_until"):
            remaining = max(0, round(job["waiting_until"] - time.time()))
            job_html = (
                f"<p>Ожидание Telegram: следующая попытка через {remaining // 60} мин. {remaining % 60} сек.</p>"
                f"<p class=\"hint\">{html.escape(job['message'])}</p>"
                f'<p><a href="/deferred.csv?job={job["id"]}">Скачать отложенные CSV</a></p>'
            )
            refresh_html = '<meta http-equiv="refresh" content="10">'
        elif not job.get("started", False):
            job_html = (
                f"<p>В очереди: 0 из {job['total']}. Список сохранён и готов к проверке.</p>"
                f'<form method="post" action="/jobs/{job["id"]}/start">'
                '<button type="submit">Запустить обработку</button></form>'
            )
        else:
            deferred_html = ""
            if job.get("deferred_phones", []):
                phones = "".join(
                    f"<li>{html.escape(phone)}</li>" for phone in job["deferred_phones"]
                )
                deferred_html = (
                    f"<details><summary>Отложено к повтору: {len(job['deferred_phones'])}</summary>"
                    f"<ul>{phones}</ul></details>"
                    f'<p><a href="/deferred.csv?job={job["id"]}">Скачать отложенные CSV</a></p>'
                )
            eta_html = ""
            if job.get("eta_seconds") is not None:
                eta_html = (
                    f'<p class="hint">Оценка до завершения: примерно '
                    f'{job["eta_seconds"] // 60} мин. {job["eta_seconds"] % 60} сек.</p>'
                )
            job_html = (
                f"<p>В очереди и обработке: {job['processed']} из {job['total']}.</p>"
                f"<p class=\"hint\">{html.escape(job['message'])}</p>"
                f"{eta_html}"
                f"{deferred_html}"
                f'<form method="post" action="/jobs/{job["id"]}/stop-deferred">'
                '<button type="submit">Не проверять отложенные</button></form>'
            )
            refresh_html = '<meta http-equiv="refresh" content="2">'
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    result_html = ""
    if rows:
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{html.escape(row.get(field, ''))}</td>"
                if field not in {"company_link", "telegram_link"} or not row.get(field)
                else f'<td><a href="{html.escape(row[field], quote=True)}" target="_blank" rel="noreferrer">{html.escape(row[field])}</a></td>'
                for field in ROW_FIELDS
            )
            + "</tr>"
            for row in rows
        )
        headers = "".join(f"<th>{html.escape(field)}</th>" for field in ROW_FIELDS)
        result_html = (
            f"<p>Обработано номеров: {len(rows)}</p>"
            f'<p><a class="button" href="{download_url}">Скачать CSV</a></p>'
            f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>"
        )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Telegram validator</title>
{refresh_html}
<style>body{{font:16px system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#18212b}}textarea{{width:100%;min-height:220px;box-sizing:border-box;font:inherit}}button,.button{{display:inline-block;background:#276ef1;color:white;padding:10px 14px;border:0;border-radius:6px;text-decoration:none;font:inherit;cursor:pointer}}label{{display:block;margin:16px 0 8px}}.hint{{color:#586574}}.error{{color:#b42318}}table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:16px}}th,td{{border:1px solid #d0d5dd;padding:8px;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#f2f4f7}}</style>
</head><body><h1>Проверка номеров Telegram</h1>
<p class="hint">Вставьте номера по одному в строке, в международном формате. Перед группой можно указать название компании обычной строкой или в виде <code>[Название компании](https://example.com)</code>. Очередь соблюдает паузы Telegram и не пропускает пакет при ограничении.</p>
{error_html}<form id="queue-form" method="post" action="/check"><label for="phone_numbers">Компании и номера</label><textarea id="phone_numbers" name="phone_numbers" required placeholder="ООО Пример&#10;+79991234567&#10;+447911123456">{html.escape(input_text)}</textarea><input id="queue-confirmation" type="hidden" name="queue_confirmation" value=""><p><button id="queue-button" type="button">Поставить в очередь</button></p></form>{job_html}{result_html}
<script>const form=document.getElementById('queue-form');const confirmation=document.getElementById('queue-confirmation');document.getElementById('queue-button').addEventListener('click',()=>{{confirmation.value='yes';form.requestSubmit();}});form.addEventListener('submit',(event)=>{{if(confirmation.value!=='yes')event.preventDefault();}});</script>
</body></html>"""


class ValidatorApplication:
    def __init__(
        self,
        check_batch: Callable[[str], list[dict[str, str]]] = check_phone_batch,
        state_path: Path | None = None,
    ):
        self.check_batch = check_batch
        self.state_path = state_path
        self.last_rows: list[dict[str, str]] = []
        self.jobs: dict[str, dict] = {}
        self._state_lock = Lock()
        self._worker_lock = Lock()
        self._load_state()
        if self.state_path:
            Thread(target=self._scheduler_loop, daemon=True).start()

    def _scheduler_loop(self) -> None:
        while True:
            time.sleep(5)
            now = time.time()
            with self._state_lock:
                due = [
                    job_id
                    for job_id, job in self.jobs.items()
                    if not job["complete"]
                    and not job["started"]
                    and job.get("waiting_until")
                    and job["waiting_until"] <= now
                ]
            for job_id in due:
                self.start(job_id)

    def _save_locked(self) -> None:
        if not self.state_path:
            return
        temporary_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps({"jobs": self.jobs, "last_rows": self.last_rows}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(self.state_path)

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.jobs = state.get("jobs", {})
            self.last_rows = state.get("last_rows", [])
            for job in self.jobs.values():
                job.setdefault("results_by_phone", {})
                job.setdefault("deferred_phones", [])
                job.setdefault("skip_deferred", False)
                job.setdefault("started", False)
                job.setdefault("complete", False)
                job.setdefault("waiting_until", None)
                job.setdefault("retry_round", 0)
                if job["started"] and not job["complete"]:
                    job.update(
                        started=False,
                        message="Сервер был перезапущен. Нажмите «Запустить обработку» для продолжения.",
                    )
            self._save_locked()
        except (OSError, json.JSONDecodeError):
            self.jobs = {}
            self.last_rows = []

    def enqueue(self, raw: str) -> str:
        job_id = uuid4().hex
        with self._state_lock:
            self.jobs[job_id] = {
                "id": job_id,
                "raw": raw,
                "total": len(parse_company_phone_batch(raw)),
                "processed": 0,
                "message": "Задача поставлена в очередь.",
                "rows": [],
                "error": "",
                "complete": False,
                "started": False,
                "skip_deferred": False,
                "deferred_phones": [],
                "results_by_phone": {},
                "started_at": None,
                "eta_seconds": None,
                "waiting_until": None,
                "retry_round": 0,
            }
            self._save_locked()
        return job_id

    def start(self, job_id: str) -> bool:
        with self._state_lock:
            job = self.jobs.get(job_id)
            if not job or job["complete"] or job["started"]:
                return False
            job.update(
                started=True,
                skip_deferred=False,
                waiting_until=None,
                started_at=time.time(),
                eta_seconds=None,
                message="Задача ожидает свободный аккаунт Telegram.",
            )
            raw = job["raw"]
            self._save_locked()
        Thread(target=self._run_job, args=(job_id, raw), daemon=True).start()
        return True

    def stop_deferred(self, job_id: str) -> bool:
        with self._state_lock:
            job = self.jobs.get(job_id)
            if not job or job["complete"]:
                return False
            job.update(
                skip_deferred=True,
                message="Отложенные контакты будут сняты с обработки после текущего пакета.",
            )
            self._save_locked()
        return True

    def snapshot(self, job_id: str) -> dict | None:
        with self._state_lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def _update_job(self, job_id: str, **updates) -> None:
        with self._state_lock:
            job = self.jobs[job_id]
            job.update(updates)
            processed = job.get("processed", 0)
            if job.get("started_at") and processed:
                elapsed = time.time() - job["started_at"]
                job["eta_seconds"] = round(elapsed / processed * (job["total"] - processed))
            self._save_locked()

    def _run_job(self, job_id: str, raw: str) -> None:
        with self._worker_lock:
            self._update_job(job_id, message="Проверка запущена.")
            initial_results = self.snapshot(job_id)["results_by_phone"]
            def skip_deferred() -> bool:
                with self._state_lock:
                    return self.jobs[job_id]["skip_deferred"]

            try:
                if self.check_batch is check_phone_batch:
                    rows = asyncio.run(
                        _check_phone_batch(
                            raw,
                            progress=lambda processed, total, message: self._update_job(
                                job_id, processed=processed, total=total, message=message
                            ),
                            skip_deferred=skip_deferred,
                            deferred=lambda phones: self._update_job(
                                job_id, deferred_phones=phones
                            ),
                            initial_results=initial_results,
                            checkpoint=lambda results: self._update_job(
                                job_id, results_by_phone=results
                            ),
                            schedule_later=lambda phones: self._update_job(
                                job_id, deferred_phones=phones
                            ),
                        )
                    )
                else:
                    rows = self.check_batch(raw)
            except RetryLater:
                with self._state_lock:
                    job = self.jobs[job_id]
                    retry_round = job.get("retry_round", 0) + 1
                    delay = RETRY_WINDOWS_SECONDS[min(retry_round - 1, len(RETRY_WINDOWS_SECONDS) - 1)]
                    job.update(
                        started=False,
                        retry_round=retry_round,
                        waiting_until=time.time() + delay,
                        message=(
                            "Telegram временно отложил оставшиеся контакты. "
                            f"Следующая автоматическая попытка через {delay // 60} мин."
                        ),
                    )
                    self._save_locked()
                return
            except ConfigurationError as error:
                self._update_job(job_id, error=str(error), complete=True, message="Не настроено.")
                return
            except EOFError:
                self._update_job(
                    job_id,
                    error="Требуется авторизация Telegram в терминале. Перезапустите проверку после входа в аккаунт.",
                    complete=True,
                    message="Требуется авторизация.",
                )
                return
            except Exception as error:
                self._update_job(job_id, error=str(error), complete=True, message="Ошибка обработки.")
                return
            with self._state_lock:
                self.jobs[job_id].update(
                    rows=rows,
                    processed=len(rows),
                    complete=True,
                    eta_seconds=0,
                    message="Готово.",
                )
                self.last_rows = rows
                self._save_locked()

    def handler(self):
        application = self

        class RequestHandler(BaseHTTPRequestHandler):
            def send_content(self, content: str, content_type: str = "text/html; charset=utf-8"):
                encoded = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def send_redirect(self, location: str):
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/download.csv":
                    job_id = parse_qs(parsed.query).get("job", [""])[0]
                    job = application.snapshot(job_id) if job_id else None
                    rows = job["rows"] if job else application.last_rows
                    self.send_content(rows_to_csv(rows), "text/csv; charset=utf-8")
                    return
                if parsed.path == "/deferred.csv":
                    job_id = parse_qs(parsed.query).get("job", [""])[0]
                    job = application.snapshot(job_id)
                    if not job:
                        self.send_error(404)
                        return
                    self.send_content(
                        rows_to_csv(deferred_rows(job["raw"], job["deferred_phones"])),
                        "text/csv; charset=utf-8",
                    )
                    return
                if parsed.path.startswith("/jobs/"):
                    job = application.snapshot(parsed.path.removeprefix("/jobs/"))
                    if not job:
                        self.send_error(404)
                        return
                    self.send_content(render_page(job=job))
                    return
                self.send_content(render_page(application.last_rows))

            def do_POST(self):
                path = urlparse(self.path).path
                if path == "/check":
                    length = int(self.headers.get("Content-Length", "0"))
                    values = parse_qs(self.rfile.read(length).decode("utf-8"))
                    phone_numbers = values.get("phone_numbers", [""])[0]
                    if values.get("queue_confirmation", [""])[0] != "yes":
                        self.send_content(
                            render_page(
                                error="Нажмите «Поставить в очередь», чтобы сохранить список."
                            )
                        )
                        return
                    self.send_redirect(f"/jobs/{application.enqueue(phone_numbers)}")
                    return
                job_path = path.removeprefix("/jobs/").split("/")
                if len(job_path) != 2 or not job_path[0]:
                    self.send_error(404)
                    return
                job_id, action = job_path
                if action == "start" and application.start(job_id):
                    self.send_redirect(f"/jobs/{job_id}")
                    return
                if action == "stop-deferred" and application.stop_deferred(job_id):
                    self.send_redirect(f"/jobs/{job_id}")
                    return
                self.send_error(404)

            def log_message(self, format: str, *args):
                return

        return RequestHandler


def create_server(application: ValidatorApplication, host: str = "127.0.0.1", port: int = 8000):
    return ThreadingHTTPServer((host, port), application.handler())


def get_port() -> int:
    value = os.getenv("TELEGRAM_VALIDATOR_PORT", "8000")
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("TELEGRAM_VALIDATOR_PORT must be a number.") from error
    if not 1 <= port <= 65535:
        raise ValueError("TELEGRAM_VALIDATOR_PORT must be between 1 and 65535.")
    return port


def get_data_dir() -> Path:
    """Return the directory used for durable queue state."""
    return Path(os.getenv("TELEGRAM_VALIDATOR_DATA_DIR", "."))


def get_host() -> str:
    """Return the interface on which the local web server listens."""
    return os.getenv("TELEGRAM_VALIDATOR_HOST", "127.0.0.1")


def main() -> None:
    from telegram_phone_number_checker.durable_web import (
        DurableValidatorApplication,
        create_server as create_durable_server,
    )

    port = get_port()
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    application = DurableValidatorApplication(
        data_dir / ".validator.db",
        legacy_json_path=data_dir / ".validator-jobs.json",
    )
    server = create_durable_server(
        application,
        host=get_host(),
        port=port,
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_server(signum, frame):
        # shutdown() waits for serve_forever(), so it must run on another thread.
        Thread(target=server.shutdown, name="validator-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    print(f"Откройте http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            application.close()
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
