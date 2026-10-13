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
# Commodity-route import submits one task per commodity. List fan-out keeps
# MAX_WORKERS. Both go through concurrent_futures_executor, and every GET
# still takes the shared rate lock.
ROUTE_FETCH_WORKERS = 10
MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 60.0
# A limit response has to wait out the per-minute window, not a few seconds.
RATE_LIMIT_WINDOW_SECONDS = 60.0
# Spread retries a few seconds past the window so workers do not line up.
LIMIT_JITTER_SECONDS = 5.0
OTHER_ERROR_BACKOFF_BASE = 0.5
# Connect stays short. A slow body is a read timeout, not a dead host.
# DNS failure and connection refused stay on the fast path.
CONNECT_TIMEOUT_SECONDS = 10
# Every import read waits at least the large-list timeout. A body that
# answers in 25–40s is success on cities the same as on items_prices_all.
READ_TIMEOUT_SECONDS = 60
LARGE_READ_TIMEOUT_SECONDS = 60
CONNECT_TIMEOUT_MIN = 2
CONNECT_TIMEOUT_MAX = 30
READ_TIMEOUT_MIN = 10
READ_TIMEOUT_MAX = 60
LARGE_READ_TIMEOUT_MIN = 30
LARGE_READ_TIMEOUT_MAX = 90
# Read timeouts open the breaker only after this many different endpoints
# have each used every attempt. One slow list must not wipe the run.
TIMEOUT_ENDPOINTS_BEFORE_BREAK = 2
# Wall-clock time spent in attempts that timed out, plus their retry backoff.
# Overlapping waits share one interval. A single worker's waits do not overlap,
# so its total stays the sum. Healthy reads are not charged and are not refused.
# Once this is spent, only further retries stop. The next endpoint's first
# attempt still goes out. One large endpoint (4 x 60s + backoff) still fits,
# so it can use every retry.
IMPORT_DEADLINE_SECONDS = 270
# Healthy cold-import GETs measured 2026-10-11 (api.uexcorp.uk/2.0). The
# all-timeout worst case in the import log uses this count. The test pins it.
COLD_IMPORT_REQUESTS = 295
# Lists that the large-read setting was added for. commodities_routes counts
# as one endpoint even though its pages are separate GETs. Every endpoint
# waits at least this long; the set is what the timeout budget assumes.
LARGE_ENDPOINTS = frozenset({
    "commodities_prices_all",
    "commodities_raw_prices_all",
    "commodities_routes",
    "fuel_prices_all",
    "items",
    "items_attributes",
    "items_prices_all",
    "outposts",
    "poi",
    "terminals",
    "vehicles",
    "vehicles_purchases_prices_all",
    "vehicles_rentals_prices_all",
})


def clamp_timeout(value, default: int, low: int, high: int) -> int:
    """Integer seconds inside ``low``..``high``. Bad values use ``default``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    if number < low:
        return int(low)
    if number > high:
        return int(high)
    return number


def _backoff_delay(attempt: int) -> float:
    """Retry backoff before the jitter factor. Attempt 1 is the first try."""
    delay = OTHER_ERROR_BACKOFF_BASE * (2 ** max(attempt - 1, 0))
    return min(MAX_BACKOFF_SECONDS, delay)


def _union_interval(
    intervals: list[tuple[float, float]], start: float, end: float
) -> list[tuple[float, float]]:
    """Add ``[start, end]`` to sorted, non-overlapping intervals.

    Touching or overlapping spans become one span. The covered length is
    wall-clock time, so waits that run together are not added twice.
    """
    if end <= start:
        return list(intervals)
    merged: list[tuple[float, float]] = []
    pending = True
    for left, right in intervals:
        if right < start:
            merged.append((left, right))
            continue
        if pending and end < left:
            merged.append((start, end))
            pending = False
        if not pending:
            merged.append((left, right))
            continue
        start = min(start, left)
        end = max(end, right)
    if pending:
        merged.append((start, end))
    return merged


def _retry_backoff_worst() -> float:
    """Backoff after attempts 1..MAX_ATTEMPTS-1 when the jitter factor is 1."""
    return sum(_backoff_delay(attempt) for attempt in range(1, MAX_ATTEMPTS))


def timeout_budget(large_read: float | None = None) -> dict:
    """What two endpoints cost if each burns every read attempt.

    ``uncapped`` adds throttle on top of the reads. A slow read already covers
    the spacing interval, so that sum is an upper bound on the breaker path,
    not the timeout budget. The budget itself is ``deadline`` seconds of
    timed-out waits and retry backoff.
    """
    read = float(LARGE_READ_TIMEOUT_SECONDS if large_read is None else large_read)
    endpoints = TIMEOUT_ENDPOINTS_BEFORE_BREAK
    backoff_each = _retry_backoff_worst()
    backoff_total = endpoints * backoff_each
    gaps = endpoints * MAX_ATTEMPTS - 1
    throttle = gaps * (60.0 / REQUESTS_PER_MINUTE)
    one_endpoint = MAX_ATTEMPTS * read + backoff_each
    uncapped = endpoints * one_endpoint + throttle
    return {
        "uncapped": uncapped,
        "one_endpoint": one_endpoint,
        "deadline": float(IMPORT_DEADLINE_SECONDS),
        "backoff_total": backoff_total,
        "throttle": throttle,
        "large_read": read,
        "attempts": MAX_ATTEMPTS,
        "endpoints": endpoints,
    }


def all_timeouts_worst_case(
    request_count: int,
    read: float | None = None,
    backoff_factor: float = 1.0,
) -> dict:
    """Time if every read times out.

    The first endpoint uses every attempt that still fits in the budget.
    The next endpoint's timed-out read counts as the second exhausted
    endpoint once the budget is spent, and the breaker opens. Later
    requests are not sent. ``backoff_factor`` 1 is the worst jitter.
    Charged time is timed-out reads plus the backoff of retries that run.
    A slow read already covers the throttle gap, so throttle is not added.
    """
    read = float(LARGE_READ_TIMEOUT_SECONDS if read is None else read)
    factor = min(1.0, max(0.0, backoff_factor))
    spent = 0.0
    wait = 0.0
    attempts = 0
    exhausted = 0
    for _ in range(max(int(request_count), 0)):
        if exhausted >= TIMEOUT_ENDPOINTS_BEFORE_BREAK:
            break
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1 and spent >= IMPORT_DEADLINE_SECONDS:
                break
            wait += read
            spent += read
            attempts += 1
            if attempt < MAX_ATTEMPTS and spent < IMPORT_DEADLINE_SECONDS:
                backoff = _backoff_delay(attempt) * factor
                wait += backoff
                spent += backoff
                continue
            exhausted += 1
            break
    return {
        "wait": wait,
        "charged": spent,
        "attempts": attempts,
        "budget": float(IMPORT_DEADLINE_SECONDS),
        "requests": max(int(request_count), 0),
        "read": read,
        "endpoints": exhausted,
    }


def healthy_run_seconds(request_count: int) -> float:
    """Throttle wait for a cold import whose responses are instantaneous.

    The first request does not wait. Real bodies add time on top. That time
    is a healthy read, not part of the timeout deadline. The spacing is
    ``REQUESTS_PER_MINUTE`` (90), under the documented 120/minute quota.
    """
    count = max(int(request_count), 0)
    if count <= 1:
        return 0.0
    return (count - 1) * (60.0 / REQUESTS_PER_MINUTE)


def timeout_budget_message(large_read: float | None = None) -> str:
    budget = timeout_budget(large_read)
    worst = all_timeouts_worst_case(COLD_IMPORT_REQUESTS, budget["large_read"])
    return (
        "UEX import timeout budget: "
        f"{budget['deadline']:.0f}s of timed-out reads and retry backoff. "
        "Healthy requests are not counted and are not refused. "
        "Retries stop once that is spent. An endpoint that then fails counts "
        "toward the breaker, and two timed-out endpoints open it. "
        f"If every request times out, the worst case is {worst['wait']:.1f}s "
        f"({worst['attempts']} attempts). "
        f"Two endpoints that each use every attempt are uncapped {budget['uncapped']:.1f}s "
        "before the breaker."
    )


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
        self._timeout_endpoints: set[str] = set()
        # Union of timed-out waits and the retry backoff that follows them.
        self._timeout_intervals: list[tuple[float, float]] = []
        self._timeout_spent: float = 0.0
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
            self._timeout_endpoints = set()
            self._timeout_intervals = []
            self._timeout_spent = 0.0
        self.helper.get_handler_debug().write(timeout_budget_message(self._read_timeout("items_prices_all")))

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

            cause = "timeout budget exhausted" if error == "timeout_budget" else error
            warn(
                f"UEX requests stopped for this run ({cause}). "
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
        connect, read = self._timeouts(endpoint)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # The budget refuses retries only. A first attempt always goes out
            # until two timed-out endpoints have opened the breaker.
            if attempt > 1 and self._timeout_budget_exhausted():
                return self._note_timeout_endpoint(endpoint, "timeout_budget")
            if not self._acquire_rate_limit():
                return FetchResult(False, error=self._blocked_error or "blocked")
            self.helper.get_handler_debug().write(f"Fetching data (request {attempt}/{MAX_ATTEMPTS}) from {url} ...")
            started_attempt = self._monotonic()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=(connect, read),
                    headers=self._get_headers(),
                    stream=True,
                )
            except requests.exceptions.Timeout as e:
                # ReadTimeout, and ConnectTimeout (it subclasses Timeout too).
                # DNS failure and connection refused are ConnectionError only
                # and take the fast path below.
                failed = self._exhausted_read_timeout(
                    attempt, e, url, endpoint, started_attempt
                )
                if failed is not None:
                    return failed
                continue
            except requests.exceptions.ConnectionError as e:
                # DNS and connection refused. One endpoint that burns its
                # attempts opens the breaker. A read timeout does not.
                failed = self._exhausted_connection(attempt, e, url)
                if failed is not None:
                    return failed
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
            except requests.exceptions.Timeout as e:
                # A body ReadTimeout is the slow path. It is not a dead host.
                try:
                    response.close()
                except Exception:
                    pass
                failed = self._exhausted_read_timeout(
                    attempt, e, url, endpoint, started_attempt
                )
                if failed is not None:
                    return failed
                continue
            except requests.exceptions.RequestException as e:
                # stream=True defers the body read until json(). A truncated
                # chunk stays on the fast path. So does ConnectionError
                # raised while reading the body.
                try:
                    response.close()
                except Exception:
                    pass
                failed = self._exhausted_connection(attempt, e, url)
                if failed is not None:
                    return failed
                continue
            except ValueError as e:
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

    def _configured_seconds(self, method: str, default: int, low: int, high: int) -> int:
        config = self.helper.get_handler_config()
        getter = getattr(config, method, None)
        raw = default
        if callable(getter):
            try:
                raw = getter()
            except Exception:
                raw = default
        return clamp_timeout(raw, default, low, high)

    def _timeouts(self, endpoint: str) -> tuple[int, int]:
        """Connect stays short. Every read waits at least the large-list timeout.

        A 35s answer on a small endpoint has to succeed. ``endpoint`` selects
        nothing: ``LARGE_ENDPOINTS`` names the lists, and the wait is the
        longer of the two configured reads.
        """
        connect = self._configured_seconds(
            "get_api_connect_timeout",
            CONNECT_TIMEOUT_SECONDS,
            CONNECT_TIMEOUT_MIN,
            CONNECT_TIMEOUT_MAX,
        )
        read = self._configured_seconds(
            "get_api_read_timeout",
            READ_TIMEOUT_SECONDS,
            READ_TIMEOUT_MIN,
            READ_TIMEOUT_MAX,
        )
        large = self._configured_seconds(
            "get_api_large_read_timeout",
            LARGE_READ_TIMEOUT_SECONDS,
            LARGE_READ_TIMEOUT_MIN,
            LARGE_READ_TIMEOUT_MAX,
        )
        # LARGE_ENDPOINTS names the lists this wait was added for. A slow
        # answer on any other endpoint gets the same window.
        return connect, max(read, large)

    def _read_timeout(self, endpoint: str) -> int:
        return self._timeouts(endpoint)[1]

    def _charge_timeout_interval(self, start: float, end: float) -> None:
        """Charge the wall-clock span ``[start, end]``.

        Ten workers stalled across the same window add that window once.
        One worker's later wait starts after the earlier one ends, so the
        union is the sum.
        """
        if end <= start:
            return
        with self._rate_lock:
            self._timeout_intervals = _union_interval(self._timeout_intervals, start, end)
            self._timeout_spent = sum(right - left for left, right in self._timeout_intervals)

    def _timeout_budget_exhausted(self) -> bool:
        with self._rate_lock:
            return self._timeout_spent >= IMPORT_DEADLINE_SECONDS

    def _exhausted_read_timeout(
        self,
        attempt: int,
        error: BaseException,
        url: str,
        endpoint: str,
        started_attempt: float,
    ) -> FetchResult | None:
        """Retry a read timeout like a 5xx, until the timeout budget is spent.

        Charged time is the wall-clock union of this timed-out wait and the
        backoff of a retry that will actually run. A spent budget fails this
        endpoint and counts it as exhausted, the same as using every attempt.
        The breaker opens after two different endpoints. Until then, the next
        endpoint's first attempt still goes out. Return None when the caller
        should try again.
        """
        message = str(error) or "read_timeout"
        finished = self._monotonic()
        self._charge_timeout_interval(started_attempt, finished)
        self.helper.get_handler_debug().write(
            f"Error while retrieving data from {url}: {error}",
            attempt == MAX_ATTEMPTS,
        )
        self.helper.get_handler_error().write("Api.fetch", [url], error)
        if attempt < MAX_ATTEMPTS and not self._timeout_budget_exhausted():
            backoff = self._backoff_seconds(attempt)
            now = self._monotonic()
            self._charge_timeout_interval(now, now + backoff)
            self._sleeper(backoff)
            return None
        if attempt < MAX_ATTEMPTS:
            return self._note_timeout_endpoint(endpoint, "timeout_budget")
        return self._note_timeout_endpoint(endpoint, message)

    def _note_timeout_endpoint(self, endpoint: str, error: str) -> FetchResult:
        """Count one exhausted timeout endpoint. Open the breaker at two."""
        with self._rate_lock:
            self._timeout_endpoints.add(endpoint)
            opened = len(self._timeout_endpoints) >= TIMEOUT_ENDPOINTS_BEFORE_BREAK
        if opened:
            self._trip_breaker(error)
            return FetchResult(False, error=self._blocked_error or error)
        return FetchResult(False, error=error)

    def _exhausted_connection(self, attempt: int, error: BaseException, url: str) -> FetchResult | None:
        """Retry a connection or body-read failure.

        Return a failed result once this request has used its attempts and the
        breaker is open. Return None when the caller should try again.
        """
        message = str(error) or "connection_error"
        self.helper.get_handler_debug().write(
            f"Error while retrieving data from {url}: {error}",
            attempt == MAX_ATTEMPTS,
        )
        self.helper.get_handler_error().write("Api.fetch", [url], error)
        if attempt >= MAX_ATTEMPTS:
            self._trip_breaker(message)
            return FetchResult(False, error=self._blocked_error or message)
        self._sleeper(self._backoff_seconds(attempt))
        return None

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


def concurrent_futures_executor(max_workers: int | None = None):
    import concurrent.futures

    workers = MAX_WORKERS if max_workers is None else max_workers
    return concurrent.futures.ThreadPoolExecutor(max_workers=workers)


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
