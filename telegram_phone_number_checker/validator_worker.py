"""Single-account worker for durable validator jobs.

The worker has no Telegram state of its own: claims, rate limits and cleanup
work are persisted by the store before or after every network request.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

from telethon.errors import FloodWaitError
from telegram_phone_number_checker.user_session import (
    UserSessionUnavailableError, RequestNotDispatchedError, RequestOutcomeUnknownError,
)


MAX_IMPORT_BATCH_SIZE = 10


class TelegramGateway(Protocol):
    def import_phones(self, phones: list[str]) -> dict[str, dict]: ...
    def resolve_phone(self, phone: str) -> dict: ...
    def cleanup_contacts(self, user_ids: list[int]) -> None: ...


class ValidatorWorker:
    def __init__(self, store, gateway: TelegramGateway, owner: str, sleep=time.sleep):
        self.store = store
        self.gateway = gateway
        self.owner = owner
        self.sleep = sleep
        # Avoid a second request for a duplicate that appears in a later claimed
        # batch. The store also deduplicates durable work across restarts.
        self._results_by_phone: dict[tuple[str, str, str], dict] = {}
        self._import_batches_since_cleanup = 0

    def run_once(self) -> bool:
        cleanup_due = getattr(self.store, 'cleanup_due', None)
        due = cleanup_due() if cleanup_due else self._import_batches_since_cleanup >= 5
        if due and self._run_cleanup_once():
            self._import_batches_since_cleanup = 0
            return True

        claims = self._claim_batch()
        if not claims:
            return self._run_cleanup_once()
        if claims[0]["method"] == "DIRECT_LOOKUP":
            return self._run_direct_lookup(claims)
        return self._run_import_batch(claims)

    def _claim_batch(self) -> list[dict]:
        """Claim a store-selected import batch, or retain legacy single claims."""
        claim_batch = getattr(self.store, "claim_batch", None)
        if claim_batch:
            # The store applies the job's persisted adaptive batch size. A direct
            # lookup claim is always returned alone.
            return claim_batch(self.owner, MAX_IMPORT_BATCH_SIZE)
        claim = self.store.claim_next(self.owner)
        return [claim] if claim else []

    def _run_import_batch(self, claims: list[dict]) -> bool:
        cached, pending = self._split_cached_claims(claims)
        if cached:
            self._finish(cached, self._results_for(cached))
        if not pending:
            return True

        unique_phones = list(dict.fromkeys(claim["phone"] for claim in pending))
        try:
            results = self._with_claim_lease(claims, lambda: self.gateway.import_phones(unique_phones))
        except FloodWaitError as error:
            self.store.defer_account_floodwait(pending, error.seconds)
            return True
        except UserSessionUnavailableError:
            self.store.pause_for_authorization(pending)
            return True
        except RequestOutcomeUnknownError:
            self.store.defer_unknown_outcome(pending, 'IMPORT')
            return True
        except RequestNotDispatchedError as error:
            self._retry(pending, error)
            return True
        except Exception as error:
            self._retry(pending, error)
            return True

        normalized_results = {
            phone: results.get(phone, {"error": "Telegram returned no result for this phone."})
            for phone in unique_phones
        }
        # The store owns one transaction for terminal results, retry rows,
        # adaptive size/streak, and the durable cleanup budget.
        self.store.finish_import_batch(pending, normalized_results)
        completed_claims = [claim for claim in pending if not normalized_results[claim['phone']].get('retry')]
        self._remember(completed_claims, normalized_results)
        self._import_batches_since_cleanup += 1
        return True

    def _run_direct_lookup(self, claims: list[dict]) -> bool:
        # `claim_batch` deliberately returns one direct lookup claim, keeping the
        # persisted 3.1-second account limiter in the store authoritative.
        claim = claims[0]
        cached, pending = self._split_cached_claims([claim])
        if cached:
            self._finish(cached, self._results_for(cached))
            return True
        try:
            result = self._with_claim_lease([claim], lambda: self.gateway.resolve_phone(claim["phone"]))
        except FloodWaitError as error:
            self.store.defer_account_floodwait([claim], error.seconds)
            return True
        except UserSessionUnavailableError:
            self.store.pause_for_authorization([claim])
            return True
        except RequestOutcomeUnknownError:
            self.store.defer_unknown_outcome([claim], 'DIRECT_LOOKUP')
            return True
        except Exception as error:
            self.store.retry_claim(claim, error)
            return True
        self._finish([claim], {claim["phone"]: result})
        self._remember([claim], {claim["phone"]: result})
        return True

    def _split_cached_claims(self, claims: list[dict]) -> tuple[list[dict], list[dict]]:
        cached = [
            claim
            for claim in claims
            if (claim["job_id"], claim["phone"], claim["method"]) in self._results_by_phone
        ]
        return cached, [claim for claim in claims if claim not in cached]

    def _results_for(self, claims: list[dict], source: dict[str, dict] | None = None) -> dict[str, dict]:
        if source is not None:
            return {claim["phone"]: source[claim["phone"]] for claim in claims}
        return {
            claim["phone"]: self._results_by_phone[(claim["job_id"], claim["phone"], claim["method"])]
            for claim in claims
        }

    def _remember(self, claims: list[dict], results: dict[str, dict]) -> None:
        # Cache is filled by the claims that received a result, preserving job
        # and method isolation while coalescing duplicate phones within one
        # import batch. Legacy direct results cannot satisfy migrated imports.
        for phone, result in results.items():
            if not result.get("retry"):
                for claim in claims:
                    if claim["phone"] == phone:
                        self._results_by_phone[(claim["job_id"], phone, claim["method"])] = result

    def _finish(self, claims: list[dict], results: dict[str, dict]) -> None:
        finish_batch = getattr(self.store, "finish_batch", None)
        if finish_batch:
            finish_batch(claims, results)
            return
        for claim in claims:
            self.store.finish_claim(claim, results[claim["phone"]])

    def _retry(self, claims: list[dict], error: Exception) -> None:
        retry_batch = getattr(self.store, 'retry_batch', None)
        if retry_batch:
            retry_batch(claims, error)
        else:
            for claim in claims:
                self.store.retry_claim(claim, error)

    def _run_cleanup_once(self) -> bool:
        claim_cleanup = getattr(self.store, "claim_cleanup_batch", None)
        if not claim_cleanup:
            return False
        claim = claim_cleanup(self.owner, 100)
        if not claim:
            return False
        try:
            self._with_cleanup_lease(claim, lambda: self.gateway.cleanup_contacts(claim["telegram_user_ids"]))
        except FloodWaitError as error:
            self.store.defer_cleanup_floodwait(claim, error.seconds)
        except UserSessionUnavailableError:
            self.store.pause_cleanup_for_authorization(claim)
        except RequestOutcomeUnknownError:
            self.store.defer_cleanup_unknown_outcome(claim)
        except Exception as error:
            self.store.retry_cleanup(claim, error)
        else:
            self.store.finish_cleanup(claim)
        return True

    def _with_claim_lease(self, claims: list[dict], request):
        renew = getattr(self.store, "renew_claims", None)
        return self._with_lease(renew and (lambda: renew(claims)), request)

    def _with_cleanup_lease(self, claim: dict, request):
        renew = getattr(self.store, "renew_cleanup_claim", None)
        return self._with_lease(renew and (lambda: renew(claim)), request)

    @staticmethod
    def _with_lease(renew, request):
        """Renew the persisted lease while a blocking Telethon call is in flight."""
        if not renew:
            return request()
        done = threading.Event()

        def heartbeat():
            while not done.wait(20):
                try:
                    renew()
                except Exception:
                    # Ownership remains valid until the original lease expires; the
                    # request outcome is still persisted by the normal error path.
                    pass

        thread = threading.Thread(target=heartbeat, name="validator-lease", daemon=True)
        thread.start()
        try:
            return request()
        finally:
            done.set()
            thread.join(timeout=0.1)
