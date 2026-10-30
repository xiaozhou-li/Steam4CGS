"""Clients for the three Steam endpoints the crawler uses.

* ``IStoreService/GetAppList/v1``            game discovery (Web API key required)
* ``IUserReviewsService/GetAppReviews/v1``   review summaries (Web API)
* ``store.steampowered.com/api/appdetails`` detailed metadata (undocumented)

Keyed calls go to ``api.steampowered.com``, the public host for regular Web
API keys (``partner.steam-api.com`` is for publisher keys and is not used).
The key is sent only to that host; the Store client never receives it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from crawler.credentials import ApiKey, MissingCredentialError
from crawler.transport import SteamResponseError, SteamTransport

WEB_API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"

APP_LIST_MAX_RESULTS = 50_000
MAX_TAGS = 20  # the store page shows at most 20 user tags; the legacy CSV never exceeds 20


def public_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """The request URL as recorded in provenance: never includes the API key."""
    query = {k: v for k, v in (params or {}).items() if k != "key" and v is not None}
    return f"{url}?{urlencode(query)}" if query else url


def appdetails_request(appid: int, country: str, language: str) -> tuple[str, dict]:
    return f"{STORE_BASE}/api/appdetails", {"appids": appid, "cc": country, "l": language}


def store_items_request(appid: int, country: str, language: str, tag_count: int = MAX_TAGS) -> tuple[str, dict]:
    payload = {
        "ids": [{"appid": appid}],
        "context": {"language": language, "country_code": country.upper(), "steam_realm": 1},
        "data_request": {"include_tag_count": tag_count},
    }
    return f"{WEB_API_BASE}/IStoreBrowseService/GetItems/v1/", {"input_json": json.dumps(payload, separators=(",", ":"))}


def tag_list_request(language: str) -> tuple[str, dict]:
    return f"{WEB_API_BASE}/IStoreService/GetTagList/v1/", {"language": language}


def _param(value: Any) -> Any:
    # The Web API expects lowercase booleans.
    return ("true" if value else "false") if isinstance(value, bool) else value


def _check_appid(appid: int) -> int:
    if isinstance(appid, bool) or not isinstance(appid, int) or appid <= 0:
        raise ValueError(f"appid must be a positive int, got {appid!r}")
    return appid


def _expect_dict(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise SteamResponseError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return value


# --------------------------------------------------------------------------
# Steam Web API
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppListEntry:
    appid: int
    name: str
    last_modified: int | None = None
    price_change_number: int | None = None


@dataclass(frozen=True)
class AppListPage:
    apps: tuple[AppListEntry, ...]
    have_more_results: bool
    last_appid: int | None


@dataclass(frozen=True)
class ApiMethod:
    interface: str
    name: str
    version: int
    http_method: str
    parameters: tuple[str, ...]


class SteamWebAPI:
    def __init__(self, transport: SteamTransport, key: ApiKey | None):
        self._transport = transport
        self._key = key
        if key is not None:
            transport.register_secret(key.reveal())

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def call(
        self,
        interface: str,
        method: str,
        version: int = 1,
        params: Mapping[str, Any] | None = None,
        *,
        require_key: bool = True,
    ) -> Any:
        """GET ``/{interface}/{method}/v{version}/``. The key is added when available."""
        if require_key and self._key is None:
            raise MissingCredentialError(f"{interface}/{method} requires a Steam Web API key (STEAM_API_KEY)")
        query = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        if "key" in query:
            raise ValueError("pass the API key via SteamWebAPI(key=...), not as a parameter")
        if self._key is not None:
            query["key"] = self._key.reveal()
        return self._transport.get_json(
            f"{WEB_API_BASE}/{interface}/{method}/v{version}/", query, authenticated=self._key is not None
        )

    def _response(self, body: Any, what: str) -> dict:
        response = _expect_dict(body, what).get("response")
        return _expect_dict(response, f"{what} 'response'")

    # -- discovery ---------------------------------------------------------------

    def get_supported_methods(self) -> dict[tuple[str, str, int], ApiMethod]:
        """``ISteamWebAPIUtil/GetSupportedAPIList/v1``: methods visible to this key.

        Without a key only public methods are listed, so absence is not proof
        that a keyed method is unavailable.
        """
        body = _expect_dict(self.call("ISteamWebAPIUtil", "GetSupportedAPIList", 1, require_key=False), "GetSupportedAPIList")
        interfaces = _expect_dict(body.get("apilist"), "GetSupportedAPIList 'apilist'").get("interfaces", [])
        out: dict[tuple[str, str, int], ApiMethod] = {}
        for iface in interfaces if isinstance(interfaces, list) else []:
            if not isinstance(iface, dict):
                continue
            for m in iface.get("methods", []) or []:
                if not isinstance(m, dict):
                    continue
                method = ApiMethod(
                    interface=str(iface.get("name", "")),
                    name=str(m.get("name", "")),
                    version=int(m.get("version", 0) or 0),
                    http_method=str(m.get("httpmethod", "")),
                    parameters=tuple(
                        str(p.get("name", "")) for p in m.get("parameters", []) or [] if isinstance(p, dict)
                    ),
                )
                out[(method.interface, method.name, method.version)] = method
        return out

    def get_app_list(
        self,
        *,
        last_appid: int | None = None,
        max_results: int = 10_000,
        if_modified_since: int | None = None,
        include_games: bool = True,
        include_dlc: bool = False,
        include_software: bool = False,
        include_videos: bool = False,
        include_hardware: bool = False,
    ) -> AppListPage:
        """One page of ``IStoreService/GetAppList/v1``, ordered by appid."""
        if not 1 <= max_results <= APP_LIST_MAX_RESULTS:
            raise ValueError(f"max_results must be within 1..{APP_LIST_MAX_RESULTS}")
        params = {
            "last_appid": last_appid,
            "max_results": max_results,
            "if_modified_since": if_modified_since,
            "include_games": include_games,
            "include_dlc": include_dlc,
            "include_software": include_software,
            "include_videos": include_videos,
            "include_hardware": include_hardware,
        }
        response = self._response(self.call("IStoreService", "GetAppList", 1, params), "GetAppList")
        apps = response.get("apps", [])
        if not isinstance(apps, list):
            raise SteamResponseError("GetAppList 'apps': expected a list")
        entries = []
        for raw in apps:
            raw = _expect_dict(raw, "GetAppList app")
            appid, name = raw.get("appid"), raw.get("name")
            if not isinstance(appid, int) or not isinstance(name, str):
                raise SteamResponseError(f"GetAppList app has invalid appid/name: appid={appid!r}")
            entries.append(
                AppListEntry(appid, name, raw.get("last_modified"), raw.get("price_change_number"))
            )
        last = response.get("last_appid")
        return AppListPage(tuple(entries), bool(response.get("have_more_results", False)), last if isinstance(last, int) else None)

    def iter_app_list(self, *, max_pages: int | None = None, **filters: Any) -> Iterator[AppListEntry]:
        """Yield apps across pages, following ``last_appid`` until exhausted."""
        last_appid = filters.pop("last_appid", None)
        pages = 0
        while max_pages is None or pages < max_pages:
            page = self.get_app_list(last_appid=last_appid, **filters)
            pages += 1
            yield from page.apps
            if not page.have_more_results:
                return
            if page.last_appid is None or (last_appid is not None and page.last_appid <= last_appid):
                raise SteamResponseError("GetAppList pagination did not advance last_appid")
            last_appid = page.last_appid

    # -- reviews -----------------------------------------------------------------

    def get_app_reviews(self, appid: int, **params: Any) -> dict:
        """``IUserReviewsService/GetAppReviews/v1`` for one app; returns the raw ``response`` object.

        Parameter names beyond ``appid`` are passed through unchanged; run
        ``python -m crawler preflight --live`` to see the ones Steam declares.
        The key is sent when configured but not required client-side.
        """
        body = self.call(
            "IUserReviewsService", "GetAppReviews", 1, {"appid": _check_appid(appid), **params}, require_key=False
        )
        return self._response(body, "GetAppReviews")

    # -- tags ----------------------------------------------------------------------

    def get_store_item(self, appid: int, *, country: str = "us", language: str = "english") -> dict | None:
        """The ``IStoreBrowseService/GetItems/v1`` store item for one app, or ``None`` if none is visible."""
        _, params = store_items_request(_check_appid(appid), country, language)
        response = self._response(
            self.call("IStoreBrowseService", "GetItems", 1, params, require_key=False), "GetItems")
        items = response.get("store_items", [])
        if not isinstance(items, list):
            raise SteamResponseError("GetItems 'store_items': expected a list")
        item = next((i for i in items if isinstance(i, dict) and i.get("appid", i.get("id")) == appid), None)
        if item is None or item.get("success", 1) != 1:
            return None
        return item

    def get_store_item_tags(self, appid: int, *, country: str = "us", language: str = "english") -> list[int] | None:
        """User-tag ids for one app, most-voted first (``None``: no visible store item)."""
        item = self.get_store_item(appid, country=country, language=language)
        if item is None:
            return None
        return [tagid for tagid, _ in store_item_tag_weights(item)]

    def get_tag_names(self, *, language: str = "english") -> dict[int, str]:
        """``IStoreService/GetTagList/v1``: tag id -> localized tag name."""
        _, params = tag_list_request(language)
        response = self._response(self.call("IStoreService", "GetTagList", 1, params, require_key=False), "GetTagList")
        tags = response.get("tags", [])
        if not isinstance(tags, list):
            raise SteamResponseError("GetTagList 'tags': expected a list")
        return {t["tagid"]: t["name"] for t in tags
                if isinstance(t, dict) and isinstance(t.get("tagid"), int) and isinstance(t.get("name"), str)}


def store_item_tag_weights(item: Mapping[str, Any]) -> list[tuple[int, int | None]]:
    """(tagid, weight) pairs from a GetItems store item, heaviest first.

    Uses ``tags[].{tagid, weight}`` when present (stable sort keeps Steam's order
    for equal weights), else ``tagids`` with unknown weights.
    """
    weighted = item.get("tags")
    if isinstance(weighted, list) and weighted:
        pairs = [(t.get("tagid"), t.get("weight", 0)) for t in weighted if isinstance(t, dict)]
        if not all(isinstance(tid, int) and isinstance(w, int) for tid, w in pairs):
            raise SteamResponseError("GetItems 'tags': expected {tagid:int, weight:int} objects")
        return sorted(pairs, key=lambda p: -p[1])
    tagids = item.get("tagids", [])
    if not isinstance(tagids, list) or not all(isinstance(t, int) for t in tagids):
        raise SteamResponseError("GetItems 'tagids': expected a list of int")
    return [(t, None) for t in tagids]


# --------------------------------------------------------------------------
# Steam Store (undocumented)
# --------------------------------------------------------------------------


class SteamStoreAPI:
    """``store.steampowered.com/api/appdetails``.

    Undocumented and unversioned: fields can change or disappear without
    notice, so callers must treat every field in ``data`` as optional. The
    API key is never sent here. ``cc=us`` and ``l=english`` match the
    legacy CSV's USD prices and English genre names.
    """

    def __init__(self, transport: SteamTransport, *, country: str = "us", language: str = "english"):
        self._transport = transport
        self.country = country
        self.language = language

    def get_app_details(self, appid: int, *, country: str | None = None, language: str | None = None) -> dict | None:
        """Return the ``data`` object for one app, or ``None`` when Steam reports ``success: false``
        (unknown, removed or region-locked app)."""
        url, params = appdetails_request(_check_appid(appid), country or self.country, language or self.language)
        body = self._transport.get_json(url, params)
        if body is None:
            raise SteamResponseError("appdetails returned null (this often indicates throttling)")
        entry = _expect_dict(_expect_dict(body, "appdetails").get(str(appid)), f"appdetails[{appid}]")
        success = entry.get("success")
        if success is False:
            return None
        if success is not True:
            raise SteamResponseError(f"appdetails[{appid}] has no boolean 'success'")
        return _expect_dict(entry.get("data"), f"appdetails[{appid}] 'data'")
