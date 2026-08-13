"""HTTP transport for Prism Central, plus an offline fixture-backed mock.

This module contains the low-level plumbing only: building URLs, HTTP Basic
auth, TLS verification policy, timeouts, retry-with-backoff for idempotent GETs,
and v4 page-based pagination. The knowledge of *which* endpoints exist and how
to parse their bodies lives in the entity collectors (clusters.py, hosts.py,
vms.py, efficiency.py), which call the small public surface exposed here:

    get_json(path, params)      -- one GET returning parsed JSON
    post_json(path, body)       -- one POST returning parsed JSON
    paginate_v4(path, params)   -- GET a v4 list endpoint across all pages

``MockClient`` implements the exact same three methods but serves canned
responses from ``fixtures/sample_data.json``, so ``--mock`` swaps the transport
without any collector code changing. This is what makes ``--mock --dry-run``
work fully offline.
"""

import json
import logging
import re
import time

import requests
from requests.auth import HTTPBasicAuth

LOG = logging.getLogger("ntnx.client")

# Per-call timeout in seconds (design document, section 5.1).
DEFAULT_TIMEOUT_SECONDS = 30

# Retry policy for idempotent GETs: 3 attempts, backoff 1s, 2s, 4s.
DEFAULT_RETRIES = 3
BACKOFF_SCHEDULE_SECONDS = [1, 2, 4]

# v4 list pagination page size (design document, section 5.1).
V4_PAGE_LIMIT = 100


class PrismApiError(Exception):
    """Raised when Prism Central returns an unrecoverable error response.

    A 4xx carries the response body so the operator can see the reason in the
    log (for example the CLU-10008 PC-cluster stats rejection). 5xx errors are
    retried first and only raised after retries are exhausted.
    """


class PrismClient:
    """A thin, retrying HTTP client bound to one Prism Central for one run."""

    def __init__(
        self,
        host,
        port,
        username,
        password,
        verify_ssl=False,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        retries=DEFAULT_RETRIES,
    ):
        """Create a client and its underlying requests session.

        Args:
            host: Prism Central hostname or IP.
            port: HTTPS port (normally 9440).
            username: Read-only service account name.
            password: Service account password (from the environment, never
                from config on disk).
            verify_ssl: ``True`` to verify, ``False`` to disable verification
                (self-signed certs), or a string path to a CA bundle.
            timeout: Per-call timeout in seconds.
            retries: Number of attempts for idempotent GETs.
        """
        self.base_url = "https://{host}:{port}".format(host=host, port=port)
        self.timeout = timeout
        self.retries = retries
        # Rate-limit handling: how many times a single call may wait out a 429,
        # and the cap on any single wait (seconds).
        self.max_rate_limit_waits = 5
        self.max_rate_limit_sleep = 30.0
        self.verify = self._resolve_verify(verify_ssl)

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    @staticmethod
    def _resolve_verify(verify_ssl):
        """Translate the config ``verify_ssl`` value into a requests verify arg.

        Suppresses urllib3's InsecureRequestWarning ONLY when verification is
        explicitly disabled, per the design document (section 2).

        Args:
            verify_ssl: ``True``, ``False`` or a CA bundle path string.

        Returns:
            The value to pass as ``verify=`` to requests (bool or path string).
        """
        if verify_ssl is False:
            # Only silence the warning when the operator has chosen to disable
            # verification on purpose.
            try:
                from urllib3.exceptions import InsecureRequestWarning

                requests.packages.urllib3.disable_warnings(
                    InsecureRequestWarning
                )
            except Exception:
                # If urllib3 internals change, failing to silence the warning
                # is harmless — never let it break the run.
                LOG.debug("Could not disable urllib3 InsecureRequestWarning")
            return False
        return verify_ssl

    def _url(self, path):
        """Join the base URL with an API path."""
        return self.base_url + path

    def get_json(self, path, params=None):
        """Perform one GET and return the parsed JSON body.

        Retries on 5xx and connection errors with exponential backoff. Does not
        retry 4xx: those fail loudly with the response body logged.

        Args:
            path: API path beginning with ``/``.
            params: Optional dict of query parameters.

        Returns:
            The parsed JSON body as a dict.

        Raises:
            PrismApiError: On a 4xx, or after retries are exhausted on 5xx /
                connection errors.
        """
        url = self._url(path)
        last_error = None

        attempt = 0
        # Rate-limit (429) waits get their own budget so a busy PC does not
        # burn the small error-retry budget meant for 5xx/connection failures.
        rate_limit_waits = 0
        while attempt < self.retries:
            started = time.time()
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout, verify=self.verify
                )
            except requests.RequestException as exc:
                # Connection-level failure: retry with backoff.
                last_error = exc
                self._log_call("GET", url, "conn-error", started)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            self._log_call("GET", url, response.status_code, started)

            if response.status_code < 400:
                return self._parse_json(response)

            if response.status_code == 429:
                # Rate limited (PLAT-10003). The Adonis gateway returns the wait
                # in milliseconds via X-(Api-)RateLimit-Reset rather than a
                # standard Retry-After header. Honor it, capped, then retry
                # without consuming the error-retry budget.
                if rate_limit_waits >= self.max_rate_limit_waits:
                    raise PrismApiError(
                        "GET {url} -> 429 rate limited after {n} waits".format(
                            url=url, n=rate_limit_waits
                        )
                    )
                wait_seconds = self._rate_limit_wait_seconds(
                    response, rate_limit_waits
                )
                LOG.warning(
                    "Rate limited on %s; waiting %.1fs before retry",
                    url,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                rate_limit_waits += 1
                continue

            if 400 <= response.status_code < 500:
                # Other client error: do not retry; surface the body.
                raise PrismApiError(
                    "GET {url} -> {code}: {body}".format(
                        url=url,
                        code=response.status_code,
                        body=response.text[:1000],
                    )
                )

            # 5xx: retry.
            last_error = PrismApiError(
                "GET {url} -> {code}".format(
                    url=url, code=response.status_code
                )
            )
            self._sleep_backoff(attempt)
            attempt += 1

        raise PrismApiError(
            "GET {url} failed after {n} attempts: {err}".format(
                url=url, n=self.retries, err=last_error
            )
        )

    def _rate_limit_wait_seconds(self, response, wait_index):
        """Compute how long to wait after a 429, from the response headers.

        Prefers the API-specific reset header, then the global one, both in
        milliseconds. Falls back to exponential backoff (1/2/4 s) when no header
        is present. The wait is capped so a misreported header cannot stall the
        run indefinitely.

        Args:
            response: The 429 ``requests`` response.
            wait_index: How many rate-limit waits have already happened (for the
                fallback backoff).

        Returns:
            Seconds to sleep as a float.
        """
        for header in ("X-Api-RateLimit-Reset", "X-RateLimit-Reset"):
            raw = response.headers.get(header)
            if raw:
                try:
                    return min(float(raw) / 1000.0, self.max_rate_limit_sleep)
                except (TypeError, ValueError):
                    pass
        # No usable header: exponential backoff, capped.
        fallback = BACKOFF_SCHEDULE_SECONDS[
            min(wait_index, len(BACKOFF_SCHEDULE_SECONDS) - 1)
        ]
        return float(min(fallback, self.max_rate_limit_sleep))

    def post_json(self, path, body):
        """Perform one POST with a JSON body and return the parsed JSON body.

        POST is used only for read-only v3 query endpoints (groups / list). A
        single attempt is made; a 5xx or connection error raises so the caller's
        per-section try/except can degrade that section gracefully.

        Args:
            path: API path beginning with ``/``.
            body: The request body (a dict, serialized to JSON).

        Returns:
            The parsed JSON body as a dict.

        Raises:
            PrismApiError: On any non-2xx response or a connection failure.
        """
        url = self._url(path)
        started = time.time()
        try:
            response = self.session.post(
                url, json=body, timeout=self.timeout, verify=self.verify
            )
        except requests.RequestException as exc:
            self._log_call("POST", url, "conn-error", started)
            raise PrismApiError(
                "POST {url} connection error: {err}".format(url=url, err=exc)
            )

        self._log_call("POST", url, response.status_code, started)
        if response.status_code >= 400:
            raise PrismApiError(
                "POST {url} -> {code}: {body}".format(
                    url=url,
                    code=response.status_code,
                    body=response.text[:1000],
                )
            )
        return self._parse_json(response)

    def paginate_v4(self, path, params=None):
        """Fetch every page of a v4 list endpoint and return the combined data.

        v4 list endpoints use ``$page`` (0-based) and ``$limit`` query params
        and report the total in ``metadata.totalAvailableResults``. This loops
        until every entity has been retrieved.

        Args:
            path: The v4 list endpoint path.
            params: Optional extra query parameters.

        Returns:
            A list of entity dicts drawn from each page's ``data`` array.
        """
        collected = []
        page = 0
        while True:
            page_params = dict(params or {})
            page_params["$page"] = page
            page_params["$limit"] = V4_PAGE_LIMIT

            payload = self.get_json(path, params=page_params)
            data = payload.get("data") or []
            collected.extend(data)

            # Stop when this page was not full — there is nothing after it.
            if len(data) < V4_PAGE_LIMIT:
                break
            page += 1

        return collected

    def _sleep_backoff(self, attempt):
        """Sleep according to the backoff schedule before the next attempt."""
        if attempt < len(BACKOFF_SCHEDULE_SECONDS):
            time.sleep(BACKOFF_SCHEDULE_SECONDS[attempt])

    @staticmethod
    def _parse_json(response):
        """Parse a response body as JSON, raising PrismApiError on bad JSON."""
        try:
            return response.json()
        except ValueError as exc:
            raise PrismApiError(
                "Non-JSON response from {url}: {err}".format(
                    url=response.url, err=exc
                )
            )

    @staticmethod
    def _log_call(method, url, status, started):
        """DEBUG-log one API call (method, URL, status, elapsed).

        Credentials are never logged: the session's Authorization header is not
        included here, and the URL carries no secrets.
        """
        elapsed_ms = int((time.time() - started) * 1000)
        LOG.debug("%s %s -> %s (%d ms)", method, url, status, elapsed_ms)


class MockClient:
    """Offline drop-in for PrismClient backed by ``fixtures/sample_data.json``.

    It exposes the same three methods the collectors use (``get_json``,
    ``post_json``, ``paginate_v4``) and routes requests to the matching canned
    response by inspecting the path. One VM's stats entry is deliberately absent
    from the fixture; requesting it raises, which exercises the report's
    per-VM graceful-degradation path.
    """

    # Regexes identifying each routable path.
    _RE_CLUSTER_STATS = re.compile(
        r"^/api/clustermgmt/v4\.0/stats/clusters/([^/]+)$"
    )
    _RE_HOST_STATS = re.compile(
        r"^/api/clustermgmt/v4\.0/stats/clusters/([^/]+)/hosts/([^/]+)$"
    )
    _RE_HOSTS_LIST = re.compile(
        r"^/api/clustermgmt/v4\.0/config/clusters/([^/]+)/hosts$"
    )
    _RE_VM_STATS = re.compile(r"^/api/vmm/v4\.0/ahv/stats/vms/([^/]+)$")

    def __init__(self, fixture_path):
        """Load the fixture file into memory.

        Args:
            fixture_path: Path to ``sample_data.json``.
        """
        with open(fixture_path, "r", encoding="utf-8") as handle:
            self._data = json.load(handle)
        LOG.info("MockClient loaded fixtures from %s", fixture_path)

    def get_json(self, path, params=None):
        """Return the canned JSON for a GET path.

        Args:
            path: API path (query string is supplied separately as ``params``
                and is ignored by the mock).
            params: Ignored; present for signature parity with PrismClient.

        Returns:
            The canned response dict.

        Raises:
            PrismApiError: When the path is a VM-stats path whose entity is
                intentionally missing (simulated collection failure), or when
                the path is unknown.
        """
        # Stats responses: the fixture stores each entity's stats as a list of
        # per-timestamp sample objects, matching the real v4 response, so it is
        # returned directly under "data".
        match = self._RE_CLUSTER_STATS.match(path)
        if match:
            return {"data": self._data["cluster_stats"][match.group(1)]}

        match = self._RE_HOST_STATS.match(path)
        if match:
            host_ext_id = match.group(2)
            return {"data": self._data["host_stats"][host_ext_id]}

        match = self._RE_VM_STATS.match(path)
        if match:
            vm_ext_id = match.group(1)
            vm_stats = self._data["vm_stats"]
            if vm_ext_id not in vm_stats:
                # Intentional: this VM's stats are unavailable in the fixture.
                raise PrismApiError(
                    "Simulated stats failure for VM {id}".format(id=vm_ext_id)
                )
            return {"data": vm_stats[vm_ext_id]}

        raise PrismApiError("MockClient: no canned GET for path " + path)

    def post_json(self, path, body):
        """Return the canned JSON for a POST path (the v3 groups endpoint).

        Dispatches by the ``entity_type`` in the request body so the same
        endpoint can serve efficiency, alert and runway queries.

        Args:
            path: API path.
            body: The request body dict.

        Returns:
            The canned response dict.

        Raises:
            PrismApiError: When the path/entity is unknown.
        """
        if path.endswith("/api/nutanix/v3/groups"):
            entity_type = body.get("entity_type")
            if entity_type == "mh_vm":
                # The efficiency and guest-storage queries share the mh_vm
                # entity type, so distinguish them by the attributes requested.
                attrs = self._requested_attributes(body)
                if any("guest.disk" in attr for attr in attrs):
                    return self._data["guest_storage_groups"]
                return self._data["efficiency_groups"]
            if entity_type == "alert":
                return self._data["alerts_groups"]
            if entity_type == "cluster":
                return self._data["runway_groups"]
        raise PrismApiError("MockClient: no canned POST for path " + path)

    @staticmethod
    def _requested_attributes(body):
        """Return the list of attribute names requested in a groups body."""
        return [
            item.get("attribute")
            for item in (body.get("group_member_attributes") or [])
        ]

    def paginate_v4(self, path, params=None):
        """Return the full canned list for a v4 list endpoint.

        The fixture already contains every entity on a single page, so this
        returns the whole list without looping.

        Args:
            path: The v4 list endpoint path.
            params: Ignored; present for signature parity.

        Returns:
            A list of entity dicts.
        """
        if path == "/api/clustermgmt/v4.0/config/clusters":
            return list(self._data["clusters_inventory"]["data"])

        match = self._RE_HOSTS_LIST.match(path)
        if match:
            cluster_ext_id = match.group(1)
            return list(
                self._data["hosts_inventory"][cluster_ext_id]["data"]
            )

        if path == "/api/vmm/v4.0/ahv/config/vms":
            return list(self._data["vms_inventory"]["data"])

        raise PrismApiError("MockClient: no canned list for path " + path)
