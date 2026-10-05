"""Store-backed local web application for the durable validator queue."""

from __future__ import annotations

import html
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from telegram_phone_number_checker import web
from telegram_phone_number_checker.queue_store import QueueStore
from telegram_phone_number_checker.validator_worker import ValidatorWorker
from telegram_phone_number_checker.user_session import UserSessionService


class DurableValidatorApplication:
    def __init__(
        self, database_path: Path, legacy_json_path: Path | None = None, gateway=None, authorization=None
    ):
        self.store = QueueStore(database_path)
        session = UserSessionService() if gateway is None or authorization is None else None
        self.gateway = gateway if gateway is not None else session
        self.authorization = authorization if authorization is not None else session
        self._session = session
        self._stop = Event()
        self._worker_thread = None
        self.owner = uuid4().hex
        self._worker = ValidatorWorker(self.store, self.gateway, self.owner)
        self.startup_error = ""
        self.worker_started = False
        if legacy_json_path and legacy_json_path.exists():
            try:
                self.store.migrate_json(legacy_json_path)
            except Exception as error:
                self.startup_error = (
                    "Не удалось перенести сохранённую очередь из JSON: "
                    f"{error}. Исправьте или восстановите файл и перезапустите сервис."
                )
            else:
                diagnostic = self.store.migration_diagnostic()
                if diagnostic["active"]:
                    self.startup_error = str(diagnostic["message"])
        if not self.startup_error:
            self._worker_thread = Thread(target=self._run_worker, name="validator-worker", daemon=True)
            self._worker_thread.start()
            self.worker_started = True

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = self._worker.run_once()
                self.store.finalize_ready_jobs()
            except Exception:
                did_work = False
            self._stop.wait(0.2 if did_work else 1)

    def close(self) -> None:
        self._stop.set()
        resources = [self._session, self.gateway, self.authorization]
        closed = set()
        for resource in resources:
            if resource is not None and id(resource) not in closed:
                closed.add(id(resource))
                close = getattr(resource, "close", None)
                if close:
                    close()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)

    def enqueue(self, raw: str) -> str:
        return self.store.create_job(raw)

    def snapshot(self, job_id: str) -> dict | None:
        job = self.store.snapshot(job_id)
        if not job:
            return None
        state = job["state"]
        return {
            **job,
            "raw": job["raw_input"],
            "complete": state in {"COMPLETED", "CANCELLED", "FAILED"},
            "started": state in {"QUEUED", "RUNNING", "WAITING_RATE_LIMIT"},
            "waiting_until": job["effective_wait_until"],
            "error": job["last_error"],
            "rows": self.store.rows(job_id),
            "deferred_phones": [],
        }

    def render_home_page(self) -> str:
        return _render_page(
            error=self.startup_error,
            accept_jobs=not self.startup_error,
            jobs=self.store.list_jobs(),
            authorization=self.authorization.snapshot(),
        )

    def render_job_page(self, job_id: str) -> str:
        job = self.snapshot(job_id)
        return (
            _render_page(
                job=job,
                error=self.startup_error,
                accept_jobs=not self.startup_error,
                authorization=self.authorization.snapshot(),
            )
            if job
            else ""
        )

    def start(self, job_id: str) -> bool:
        return (
            not self.startup_error
            and self.authorization.snapshot()["state"] == "AUTHORIZED"
            and self.store.start(job_id, authorization_confirmed=True)
        )

    def pause(self, job_id: str) -> bool:
        return self.store.pause(job_id)

    def cancel(self, job_id: str) -> bool:
        self.store.cancel(job_id)
        return True

    def begin_authorization(self, phone_number: str) -> bool:
        start = getattr(self.authorization, "start_authorization", None) or self.authorization.begin
        return start(phone_number)

    def submit_authorization_code(self, code: str) -> bool:
        return self.authorization.submit_code(code)

    def submit_authorization_password(self, password: str) -> bool:
        return self.authorization.submit_password(password)

    def confirm_manual_chat(self, job_id: str, item_id: str) -> bool:
        return self.store.confirm_manual_chat(job_id, item_id)

    def handler(self):
        application = self

        class RequestHandler(BaseHTTPRequestHandler):
            def send_content(self, content: str, content_type: str = "text/html; charset=utf-8", status: int = 200):
                encoded = content.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def redirect(self, location: str):
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/download.csv":
                    job_id = parse_qs(parsed.query).get("job", [""])[0]
                    job = application.snapshot(job_id)
                    if not job:
                        self.send_error(404)
                        return
                    self.send_content(web.rows_to_csv(job["rows"]), "text/csv; charset=utf-8")
                    return
                if parsed.path.startswith("/jobs/"):
                    job = application.snapshot(parsed.path.removeprefix("/jobs/"))
                    if not job:
                        self.send_error(404)
                        return
                    self.send_content(application.render_job_page(job["id"]))
                    return
                self.send_content(application.render_home_page())

            def do_POST(self):
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                values = parse_qs(self.rfile.read(length).decode("utf-8"))
                if path == "/auth/begin":
                    application.begin_authorization(values.get("phone_number", [""])[0])
                    self.redirect("/")
                    return
                if path == "/auth/code":
                    application.submit_authorization_code(values.get("code", [""])[0])
                    self.redirect("/")
                    return
                if path == "/auth/password":
                    application.submit_authorization_password(values.get("password", [""])[0])
                    self.redirect("/")
                    return
                if path == "/check":
                    if application.startup_error:
                        self.send_content(application.render_home_page())
                        return
                    if values.get("queue_confirmation", [""])[0] != "yes":
                        self.send_content(_render_page(error="Нажмите «Поставить в очередь», чтобы сохранить список."))
                        return
                    self.redirect(f"/jobs/{application.enqueue(values.get('phone_numbers', [''])[0])}")
                    return
                parts = path.removeprefix("/jobs/").split("/")
                if len(parts) == 3 and parts[1] == "confirm":
                    if application.confirm_manual_chat(parts[0], parts[2]):
                        self.redirect(f"/jobs/{parts[0]}")
                        return
                    self.send_error(404)
                    return
                if len(parts) != 2 or not parts[0]:
                    self.send_error(404)
                    return
                job_id, action = parts
                review_actions = {
                    'requeue-legacy-direct': application.store.requeue_legacy_direct_rows,
                    'requeue-unknown-outcomes': application.store.requeue_unknown_outcomes,
                }
                if action in review_actions:
                    try:
                        review_actions[action](job_id)
                    except KeyError:
                        self.send_error(404)
                    except ValueError as error:
                        self.send_content(_render_page(job=application.snapshot(job_id), error=str(error),
                                                       authorization=application.authorization.snapshot()), status=409)
                    else:
                        self.redirect(f'/jobs/{job_id}')
                    return
                actions = {"start": application.start, "pause": application.pause, "resume": application.start, "cancel": application.cancel}
                if action in actions and actions[action](job_id):
                    self.redirect(f"/jobs/{job_id}")
                    return
                self.send_error(404)

            def log_message(self, format: str, *args):
                return

        return RequestHandler


def create_server(
    application: DurableValidatorApplication, port: int, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), application.handler())


def _render_page(
    job: dict | None = None, error: str = "", accept_jobs: bool = True, jobs: list[dict] | None = None,
    authorization: dict[str, str] | None = None,
) -> str:
    """Render the durable-queue UI without inheriting the legacy job controls."""
    raw = job.get("raw", "") if job else ""
    job_html = ""
    results_html = ""
    refresh_html = ""
    queues_html = ""
    authorization = authorization or {"state": "NOT_AUTHORIZED", "message": ""}
    authorization_html = _render_authorization(authorization)

    if job:
        state = job["state"]
        processed = job["processed"]
        total = job["total"]
        message = html.escape(job.get("message", ""))
        if job.get('account_wait_until') or job.get('cleanup_waits'):
            if job.get('message') == 'Задача ожидает worker.':
                message = ''
        controls = ""
        if state == "STAGED":
            controls = (
                _action_form(job["id"], "start", "Запустить обработку")
                if authorization["state"] == "AUTHORIZED"
                else "<p class=\"hint\">Авторизуйте Telegram перед запуском обработки.</p>"
            )
        elif state == "PAUSED":
            controls = (
                _action_form(job["id"], "resume", "Возобновить обработку")
                + _action_form(job["id"], "cancel", "Отменить обработку", secondary=True)
            )
        elif state == 'WAITING_AUTHORIZATION':
            controls = (
                _action_form(job['id'], 'resume', 'Возобновить после авторизации')
                if authorization['state'] == 'AUTHORIZED'
                else '<p class="hint">Возобновить после авторизации: сначала подключите Telegram.</p>'
            )
        elif state in {"QUEUED", "RUNNING", "WAITING_RATE_LIMIT"}:
            controls = (
                _action_form(job["id"], "pause", "Приостановить")
                + _action_form(job["id"], "cancel", "Отменить обработку", secondary=True)
            )
            refresh_html = '<meta http-equiv="refresh" content="2">'
        legacy_count = job.get('legacy_direct_count', 0)
        unknown_count = job.get('counts', {}).get('UNKNOWN_OUTCOME_REVIEW', 0)
        if legacy_count or unknown_count or (state == 'PAUSED' and job.get('active_claims', 0)):
            reason = job.get('requeue_block_reason', '')
            if reason:
                controls += f'<p class="hint">{html.escape(reason)}</p>'
            else:
                if legacy_count:
                    controls += _action_form(job['id'], 'requeue-legacy-direct', 'Вернуть старые прямые проверки в очередь импорта', secondary=True)
                if unknown_count:
                    controls += (
                        '<p class="hint">Результат предыдущего запроса неизвестен. Повтор может снова отправить запрос Telegram.</p>'
                        + _action_form(job['id'], 'requeue-unknown-outcomes', 'Разрешить повтор проверок с неизвестным результатом', secondary=True)
                    )
        waiting = ""
        if job.get('account_wait_until'):
            waiting += '<p>Telegram ограничил запросы для всего аккаунта; ожидание сохранено.</p>'
        if job.get("waiting_until"):
            remaining = max(0, round(job["waiting_until"] - time.time()))
            waiting += (
                f"<p>Ожидание Telegram: следующая попытка через {remaining // 60} мин. "
                f"{remaining % 60} сек.</p>"
            )
        for cleanup in job.get('cleanup_waits', []):
            reason = html.escape(cleanup['reason'])
            waiting += f'<p>Очистка контактов ({cleanup["count"]}): {reason}</p>'
            if cleanup['next_attempt_at'] is None:
                waiting += '<p>Автоматический повтор очистки не назначен.</p>'
            else:
                deadline = cleanup['next_attempt_at']
                remaining = max(0, round(deadline - time.time()))
                waiting += (f'<p data-next-attempt-at="{deadline:g}">Следующая попытка очистки через {remaining // 60} мин. '
                            f'{remaining % 60} сек.; запуск зависит от состояния задачи и лимита Telegram.</p>')
        retry_times = [row['next_attempt_at'] for row in job['rows']
                       if row['status'] in {'PENDING_RETRY', 'RETRYABLE_ERROR'} and row.get('next_attempt_at') is not None]
        if retry_times:
            remaining = max(0, round(min(retry_times) - time.time()))
            waiting += f'<p>Следующая попытка импорта через {remaining // 60} мин. {remaining % 60} сек.; запуск зависит от состояния задачи и лимита Telegram.</p>'
        summary_labels = {
            'api_confirmed': 'Подтверждено API',
            'privacy_ambiguous': 'API не определил из-за приватности',
            'pending_retry': 'Повторный импорт ожидают',
            'pending_import': 'Ожидают импорта',
            'waiting_authorization': 'Требуется повторная авторизация',
            'unknown_outcome_review': 'Неизвестный результат — требуется решение оператора',
        }
        summary_html = ''.join(
            f'<li>{label}: {job.get("result_summary", {}).get(key, 0)}</li>'
            for key, label in summary_labels.items()
        )
        phase_labels = {
            "IMPORT": "импорт контактов",
            "DIRECT_LOOKUP": "прямая проверка",
            "WAITING_RATE_LIMIT": "ожидание лимита Telegram",
        }
        status_lines = "".join(
            f"<li>{html.escape(status)}: {count}</li>"
            for status, count in sorted(job.get("counts", {}).items())
        ) or "<li>Ожидает запуска</li>"
        eta = ""
        if job.get("eta_seconds") is not None and state not in {"COMPLETED", "CANCELLED"}:
            remaining = job["eta_seconds"]
            eta = f"<p>Оценка до завершения: примерно {remaining // 60} мин. {remaining % 60} сек.</p>"
        speed = ""
        if job.get("items_per_minute") is not None:
            speed = f"<p>Скорость: {job['items_per_minute']} ном./мин.</p>"
        job_html = (
            f"<p>Обработано номеров: {processed} из {total}.</p>"
            f'<ul>{summary_html}</ul>'
            '<p class="hint">API может не раскрыть номер из-за приватности; это не доказывает отсутствие аккаунта. Ручное подтверждение чата учитывается отдельно от ответа API.</p>'
            f"<p>Этап: {html.escape(phase_labels.get(job.get('phase', ''), job.get('phase', 'ожидание')))}; "
            f"обработано в этапе: {job.get('phase_processed', 0)} из {job.get('phase_total', 0)}; "
            f"осталось: {job.get('phase_remaining', 0)}.</p>"
            f"{speed}{eta}{waiting}<details><summary>Статусы строк</summary><ul>{status_lines}</ul></details>"
            f"<p class=\"hint\">{message}</p>{controls}"
        )
        results_html = _render_results(job["rows"], processed, total, job["id"])

    if jobs:
        rows = "".join(
            f'<li><a href="/jobs/{html.escape(item["id"], quote=True)}">'
            f'Задача {html.escape(item["id"][:8])}</a>: {html.escape(item["state"])} — '
            f'{item["processed"]} из {item["total"]}.</li>'
            for item in jobs
        )
        queues_html = f"<h2>Сохранённые очереди</h2><ul>{rows}</ul>"

    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    disabled = " disabled" if not accept_jobs else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Telegram validator</title>
{refresh_html}
<style>body{{font:16px system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;color:#18212b}}textarea{{width:100%;min-height:220px;box-sizing:border-box;font:inherit}}button,.button{{display:inline-block;background:#276ef1;color:white;padding:10px 14px;border:0;border-radius:6px;text-decoration:none;font:inherit;cursor:pointer;margin-right:8px}}.secondary{{background:#667085}}label{{display:block;margin:16px 0 8px}}.hint{{color:#586574}}.error{{color:#b42318}}table{{border-collapse:collapse;width:100%;font-size:14px;margin-top:16px}}th,td{{border:1px solid #d0d5dd;padding:8px;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#f2f4f7}}.action{{display:inline}}</style>
</head><body><h1>Проверка номеров Telegram</h1>
<p class="hint">Вставьте номера по одному в строке. Перед группой укажите компанию обычной строкой или как <code>[Название](https://example.com)</code>.</p>
   {error_html}{authorization_html}<form id="queue-form" method="post" action="/check"><label for="phone_numbers">Компании и номера</label><textarea id="phone_numbers" name="phone_numbers" required placeholder="ООО Пример&#10;+79991234567&#10;+447911123456"{disabled}>{html.escape(raw)}</textarea><input id="queue-confirmation" type="hidden" name="queue_confirmation" value=""><p><button id="queue-button" type="button"{disabled}>Поставить в очередь</button></p></form>{queues_html}{job_html}{results_html}
<script>const form=document.getElementById('queue-form');const confirmation=document.getElementById('queue-confirmation');document.getElementById('queue-button').addEventListener('click',()=>{{confirmation.value='yes';form.requestSubmit();}});form.addEventListener('submit',(event)=>{{if(confirmation.value!=='yes')event.preventDefault();}});</script>
</body></html>"""


def _render_authorization(authorization: dict[str, str]) -> str:
    state = authorization.get("state", "NOT_AUTHORIZED")
    message = html.escape(authorization.get("message", ""))
    if state == "AUTHORIZED":
        return f'<p class="hint">{message}</p>'
    if state == "CODE_SENT":
        return (
            f'<p class="hint">{message}</p><form method="post" action="/auth/code">'
            '<label for="telegram-code">Введите код Telegram</label>'
            '<input id="telegram-code" name="code" inputmode="numeric" autocomplete="one-time-code" required>'
            '<p><button type="submit">Подтвердить код</button></p></form>'
        )
    if state == "PASSWORD_REQUIRED":
        return (
            f'<p class="hint">{message}</p><form method="post" action="/auth/password">'
            '<label for="telegram-password">Введите пароль 2FA</label>'
            '<input id="telegram-password" name="password" type="password" autocomplete="current-password" required>'
            '<p><button type="submit">Подтвердить пароль</button></p></form>'
        )
    if state in {"REQUESTING_CODE", "VERIFYING_CODE", "VERIFYING_PASSWORD"}:
        return f'<p class="hint">{message}</p><meta http-equiv="refresh" content="2">'
    return (
        f'<p class="hint">{message}</p><form method="post" action="/auth/begin">'
        '<label for="telegram-phone">Номер Telegram</label>'
        '<input id="telegram-phone" name="phone_number" inputmode="tel" autocomplete="tel" placeholder="+79991234567" required>'
        '<p><button type="submit">Получить код Telegram</button></p></form>'
    )


def _action_form(job_id: str, action: str, label: str, secondary: bool = False) -> str:
    css_class = " secondary" if secondary else ""
    return (
        f'<form class="action" method="post" action="/jobs/{html.escape(job_id, quote=True)}/{action}">'
        f'<button class="{css_class.strip()}" type="submit">{html.escape(label)}</button></form>'
    )


def _render_results(rows: list[dict[str, str]], processed: int, total: int, job_id: str) -> str:
    if not rows:
        return ""
    headers = "".join(f"<th>{html.escape(field)}</th>" for field in web.ROW_FIELDS)
    def cell(row: dict[str, str], field: str) -> str:
        value = row.get(field, "")
        if field in {"company_link", "telegram_link"} and value:
            return f'<td><a href="{html.escape(value, quote=True)}" target="_blank" rel="noreferrer">{html.escape(value)}</a></td>'
        if field == "manual_confirmation" and row.get("status") == "UNRESOLVABLE_OR_PRIVACY":
            if value == "MANUAL_CHAT_CONFIRMED":
                return f"<td>{html.escape(value)}</td>"
            return (
                f"<td>{html.escape(value)} "
                f'<form class="action" method="post" action="/jobs/{html.escape(job_id, quote=True)}/confirm/{html.escape(row["_item_id"], quote=True)}">'
                '<button type="submit">Подтвердить вручную</button></form></td>'
            )
        return f"<td>{html.escape(value)}</td>"

    body = "".join(
        "<tr>" + "".join(cell(row, field) for field in web.ROW_FIELDS) + "</tr>" for row in rows
    )
    return (
        f"<p class=\"hint\">Показаны текущие строки: {processed} завершено из {total}.</p>"
        f'<p><a class="button" href="/download.csv?job={html.escape(job_id, quote=True)}">Скачать текущий CSV</a></p>'
        f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>"
    )
