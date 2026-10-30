"""Clients for the three Steam endpoints the crawler uses.

* ``IStoreService/GetAppList/v1``            game discovery (Web API key required)
* ``IUserReviewsService/GetAppReviews/v1``   review summaries (Web API)
* ``store.steampowered.com/api/appdetails`` detailed metadata (undocumented)

Keyed calls go to ``api.steampowered.com``, the public host for regular Web
API keys (``partner.steam-api.com`` is for publisher keys and is not used).
The key is sent only to that host; the Store client never receives it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from crawler.credentials import ApiKey, MissingCredentialError
from crawler.transport import SteamResponseError, SteamTransport

WEB_API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"

APP_LIST_MAX_RESULTS = 50_000


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
        appid = _check_appid(appid)
        body = self._transport.get_json(
            f"{STORE_BASE}/api/appdetails",
            {"appids": appid, "cc": country or self.country, "l": language or self.language},
        )
        if body is None:
            raise SteamResponseError("appdetails returned null (this often indicates throttling)")
        entry = _expect_dict(_expect_dict(body, "appdetails").get(str(appid)), f"appdetails[{appid}]")
        success = entry.get("success")
        if success is False:
            return None
        if success is not True:
            raise SteamResponseError(f"appdetails[{appid}] has no boolean 'success'")
        return _expect_dict(entry.get("data"), f"appdetails[{appid}] 'data'")
