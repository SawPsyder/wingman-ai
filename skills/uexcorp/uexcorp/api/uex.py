import email.utils
import random
import threading
import time
from itertools import product
from typing import Callable
from typing import Optional
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from skills.uexcorp.uexcorp.helper import Helper


# Documented quota is 120 requests/minute. Stay under that with a margin.
REQUESTS_PER_MINUTE = 90
MAX_WORKERS = 4
MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 60.0
# A limit response has to wait out the per-minute window, not a few seconds.
RATE_LIMIT_WINDOW_SECONDS = 60.0
# Spread retries a few seconds past the window so workers do not line up.
LIMIT_JITTER_SECONDS = 5.0
OTHER_ERROR_BACKOFF_BASE = 0.5


def skill_user_agent() -> str:
    # The skill facade has no app version. Keep this a static client name.
    return "Wingman-AI UEX skill"


class FetchResult:
    """Outcome of one UEX read.

    ``ok`` with an empty ``data`` is a genuine empty payload (status ``ok``).
    ``ok`` false means the call failed; callers must not treat ``data`` as the
    new dataset.
    """

    def __init__(self, ok: bool, data=None, error: str | None = None):
        self.ok = ok
        self.data = [] if data is None else data
        self.error = error

    def __repr__(self) -> str:
        return f"FetchResult(ok={self.ok}, error={self.error!r})"


class Uex:

    CATEGORIES = 'categories'
    CATEGORIES_ATTRIBUTES = 'categories_attributes'
    CITIES = 'cities'
    COMMODITIES = 'commodities'
    COMMODITIES_ALERTS = 'commodities_alerts'
    COMMODITIES_PRICES = 'commodities_prices_all'
    COMMODITIES_RAW_PRICES = 'commodities_raw_prices_all'
    COMMODITIES_STATUS = 'commodities_status'
    COMMODITIES_ROUTES = 'commodities_routes'
    COMPANIES = 'companies'
    FACTIONS = 'factions'
    FUEL_PRICES = 'fuel_prices_all'
    GAME_VERSIONS = 'game_versions'
    ITEMS = 'items'
    ITEMS_ATTRIBUTES = 'items_attributes'
    ITEMS_PRICES = 'items_prices_all'
    JURISDICTIONS = 'jurisdictions'
    MOONS = 'moons'
    ORBITS = 'orbits'
    ORBITS_DISTANCES = 'orbits_distances'
    OUTPOSTS = 'outposts'
    PLANETS = 'planets'
    POI = 'poi'
    REFINERIES_AUDITS = 'refineries_audits'
    REFINERIES_METHODS = 'refineries_methods'
    SPACE_STATIONS = 'space_stations'
    GAME_VERSION = 'game_versions'
    VEHICLES = 'vehicles'
    VEHICLES_PURCHASES_PRICES = 'vehicles_purchases_prices_all'
    VEHICLES_RENTALS_PRICES = 'vehicles_rentals_prices_all'
    STAR_SYSTEMS = 'star_systems'
    TERMINALS = 'terminals'

    def __init__(
        self,
        helper: "Helper",
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
    ):
        self.helper = helper
        self.session = None
        self._sleeper = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._rng = rng or random.random
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._pause_until = 0.0
        self._blocked = False
        self._blocked_error: str | None = None
        self._min_interval = 60.0 / REQUESTS_PER_MINUTE

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": skill_user_agent(),
            "Connection": "keep-alive",
        }
        return headers

    def begin_run(self) -> None:
        """Open the circuit for a new import run.

        A blocked host stays blocked until this run ends. The next run tries again.
        """
        with self._rate_lock:
            self._blocked = False
            self._blocked_error = None
            self._pause_until = 0.0

    def _is_blocked(self) -> bool:
        with self._rate_lock:
            return self._blocked

    def _trip_breaker(self, error: str) -> None:
        with self._rate_lock:
            already = self._blocked
            self._blocked = True
            if not self._blocked_error:
                self._blocked_error = error
        if not already:
            from skills.uexcorp.uexcorp.log import warn

            warn(
                f"UEX requests stopped for this run ({error}). "
                "Queued requests are skipped. Previous data is kept."
            )

    def _pause_everyone(self, delay: float) -> None:
        """Hold every worker, not only the one that received the limit."""
        if delay <= 0:
            return
        with self._rate_lock:
            until = self._monotonic() + delay
            if until > self._pause_until:
                self._pause_until = until

    def _acquire_rate_limit(self) -> bool:
        """Wait for the shared pause and the spacing interval.

        Returns False when the circuit breaker is open. The caller must not
        send HTTP in that case.
        """
        while True:
            with self._rate_lock:
                if self._blocked:
                    return False
                now = self._monotonic()
                if now < self._pause_until:
                    delay = self._pause_until - now
                elif now < self._next_request_at:
                    delay = self._next_request_at - now
                else:
                    self._next_request_at = now + self._min_interval
                    return True
            self._sleeper(delay)

    def fetch(
        self,
        endpoint: str,
        get: Optional[dict[str, any]] = None,
        params: Optional[dict[str, any]] = None,
    ) -> FetchResult:
        if self._is_blocked():
            return FetchResult(False, error=self._blocked_error or "blocked")

        if get and any(isinstance(value, list) for value in get.values()):
            keys, values = zip(*((k, v if isinstance(v, list) else [v]) for k, v in get.items()))
            combinations = list(product(*values))
            if not combinations:
                return FetchResult(True, [])
            results = []
            errors = []
            with concurrent_futures_executor() as executor:
                futures = [
                    executor.submit(self.__actual_fetch, endpoint, dict(zip(keys, combination)), params)
                    for combination in combinations
                ]
                for future in futures:
                    if self._is_blocked() and future.cancel():
                        continue
                    result = future.result()
                    if not result.ok:
                        errors.append(result.error or "request failed")
                        if self._is_blocked():
                            for pending in futures:
                                pending.cancel()
                            return FetchResult(False, error=self._blocked_error or errors[-1])
                        continue
                    if isinstance(result.data, list):
                        results.extend(result.data)
                    elif isinstance(result.data, dict):
                        results.append(result.data)
            if errors:
                return FetchResult(False, error=errors[0])
            return FetchResult(True, results)
        return self.__actual_fetch(endpoint, get, params)

    def __actual_fetch(
        self,
        endpoint: str,
        get: Optional[dict[str, any]] = None,
        params: Optional[dict[str, any]] = None,
    ) -> FetchResult:
        url = f"{self.helper.get_handler_config().get_api_url()}/{endpoint}"
        if get:
            url += "?"
            for key, value in get.items():
                url += f"{key}={value}&"
            url = url[:-1]

        if not self.helper.get_handler_config().get_api_url() or not endpoint:
            self.helper.get_handler_debug().write(f"No API URL or endpoint provided -> {url}", True)
            self.helper.get_handler_error().write("Api.fetch", [endpoint, params], f"No API URL or endpoint provided -> {url}", False)
            return FetchResult(False, error="missing api url or endpoint")

        if self.session is None:
            self.helper.get_handler_debug().write(f"Init session for {self.helper.get_handler_config().get_api_url()} for future requests ...")
            self.session = requests.Session()

        last_error = "request failed"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self._acquire_rate_limit():
                return FetchResult(False, error=self._blocked_error or "blocked")
            self.helper.get_handler_debug().write(f"Fetching data (request {attempt}/{MAX_ATTEMPTS}) from {url} ...")
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.helper.get_handler_config().get_api_timeout(),
                    headers=self._get_headers(),
                    stream=True,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # DNS, connection refused, and timeouts. One failure is retried.
                # The breaker opens only after this request has used its attempts,
                # so the rest of the run does not keep calling a dead host.
                last_error = str(e) or "connection_error"
                self.helper.get_handler_debug().write(f"Error while retrieving data from {url}: {e}", attempt == MAX_ATTEMPTS)
                self.helper.get_handler_error().write("Api.fetch", [endpoint, params], e)
                if attempt >= MAX_ATTEMPTS:
                    self._trip_breaker(last_error)
                    return FetchResult(False, error=self._blocked_error or last_error)
                self._sleeper(self._backoff_seconds(attempt))
                continue
            except (requests.exceptions.RequestException, requests.exceptions.JSONDecodeError) as e:
                last_error = str(e)
                self.helper.get_handler_debug().write(f"Error while retrieving data from {url}: {e}", attempt == MAX_ATTEMPTS)
                self.helper.get_handler_error().write("Api.fetch", [endpoint, params], e)
                if attempt < MAX_ATTEMPTS:
                    self._sleeper(self._backoff_seconds(attempt))
                continue

            response.encoding = "utf-8"
            kind, reason = self._classify_response(response, None)
            if kind == "block":
                if self._give_up_or_retry(attempt, reason, url, pause_everyone=False, response=response):
                    return FetchResult(False, error=self._blocked_error or reason)
                continue

            if response.status_code >= 400:
                last_error = f"http_{response.status_code}"
                self.helper.get_handler_debug().write(
                    f"Error while retrieving data from {url}. HTTP {response.status_code}.",
                    attempt == MAX_ATTEMPTS,
                )
                if attempt < MAX_ATTEMPTS:
                    self._sleeper(self._backoff_seconds(attempt))
                continue

            try:
                response_json = response.json()
            except (requests.exceptions.JSONDecodeError, ValueError) as e:
                last_error = str(e)
                self.helper.get_handler_debug().write(f"Error while retrieving data from {url}: {e}", attempt == MAX_ATTEMPTS)
                self.helper.get_handler_error().write("Api.fetch", [endpoint, params], e)
                if attempt < MAX_ATTEMPTS:
                    self._sleeper(self._backoff_seconds(attempt))
                continue

            status = response_json.get("status") if isinstance(response_json, dict) else None
            kind, reason = self._classify_response(response, status)
            if kind == "limit":
                if self._give_up_or_retry(attempt, reason, url, pause_everyone=True, response=response):
                    return FetchResult(False, error=self._blocked_error or reason)
                continue
            if kind == "block":
                if self._give_up_or_retry(attempt, reason, url, pause_everyone=False, response=response):
                    return FetchResult(False, error=self._blocked_error or reason)
                continue
            if status != "ok":
                last_error = str(status or "status_not_ok")
                self.helper.get_handler_debug().write(
                    f"Error while retrieving data from {url}. Status: is not \"ok\".",
                    attempt == MAX_ATTEMPTS,
                )
                if attempt < MAX_ATTEMPTS:
                    self._sleeper(self._backoff_seconds(attempt))
                continue

            data = response_json.get("data", [])
            if data is None:
                data = []
            return FetchResult(True, data)

        return FetchResult(False, error=last_error)

    def _give_up_or_retry(self, attempt: int, reason: str, url: str, pause_everyone: bool, response) -> bool:
        """Pause or back off. Return True when the breaker is now open."""
        self.helper.get_handler_debug().write(
            f"UEX request stopped ({reason}) from {url}.",
            attempt == MAX_ATTEMPTS,
        )
        if pause_everyone:
            self._pause_everyone(self._limit_delay(response))
        if attempt >= MAX_ATTEMPTS:
            self._trip_breaker(reason)
            return True
        if not pause_everyone:
            self._sleeper(self._backoff_seconds(attempt))
        return False

    def _classify_response(self, response, body_status) -> tuple[str | None, str]:
        if response.status_code == 429 or body_status == "requests_limit_reached":
            reason = "requests_limit_reached" if body_status == "requests_limit_reached" else "http_429"
            return "limit", reason
        if response.status_code == 403:
            return "block", "http_403"
        mitigated = (response.headers.get("cf-mitigated") or "").lower()
        if "challenge" in mitigated:
            return "block", "cloudflare_challenge"
        content_type = (response.headers.get("content-type") or "").lower()
        if response.status_code != 200 and "html" in content_type:
            return "block", "cloudflare_challenge"
        return None, ""

    def _limit_delay(self, response) -> float:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None and retry_after > 0:
            return min(MAX_BACKOFF_SECONDS, retry_after)
        # Wait out the per-minute window, then 0–5s. rng 0 stays at 60s.
        # Do not clamp this back to MAX_BACKOFF_SECONDS: that made the jitter 0.
        jitter = min(1.0, max(0.0, self._rng())) * LIMIT_JITTER_SECONDS
        return RATE_LIMIT_WINDOW_SECONDS + jitter

    def _backoff_seconds(self, attempt: int) -> float:
        delay = OTHER_ERROR_BACKOFF_BASE * (2 ** max(attempt - 1, 0))
        delay = min(MAX_BACKOFF_SECONDS, delay)
        factor = 0.5 + (0.5 * self._rng())
        return min(MAX_BACKOFF_SECONDS, delay * factor)


def concurrent_futures_executor():
    import concurrent.futures
    return concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)


def _parse_retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    text = str(header).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parsed = email.utils.parsedate_to_datetime(text)
    if parsed is None:
        return None
    # HTTP-date is absolute. Compare with wall clock, not the monotonic test clock.
    return max(0.0, parsed.timestamp() - time.time())
