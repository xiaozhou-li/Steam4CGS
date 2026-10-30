import pytest
from fakes import FAKE_KEY, FakeSteam, make_transport, reply

from crawler.credentials import ApiKey, MissingCredentialError
from crawler.steam_api import AppListEntry, SteamStoreAPI, SteamWebAPI
from crawler.transport import SteamAuthError, SteamResponseError


def web(fake, key=FAKE_KEY):
    return SteamWebAPI(make_transport(fake), ApiKey(key) if key else None)


def app_list_body(apps, more=False, last=None):
    response = {"apps": [{"appid": a, "name": f"Game {a}", "last_modified": 1, "price_change_number": 2} for a in apps]}
    if more:
        response["have_more_results"] = True
    if last is not None:
        response["last_appid"] = last
    return {"response": response}


# --- GetAppList -------------------------------------------------------------------

def test_get_app_list_request_shape():
    fake = FakeSteam(reply(body=app_list_body([10, 20], more=True, last=20)))
    page = web(fake).get_app_list(max_results=2, last_appid=5, if_modified_since=1700000000)
    (call,) = fake.calls
    assert call.host == "api.steampowered.com"
    assert call.path == "/IStoreService/GetAppList/v1/"
    assert call.params == {
        "key": FAKE_KEY, "last_appid": "5", "max_results": "2", "if_modified_since": "1700000000",
        "include_games": "true", "include_dlc": "false", "include_software": "false",
        "include_videos": "false", "include_hardware": "false",
    }
    assert page.apps == (AppListEntry(10, "Game 10", 1, 2), AppListEntry(20, "Game 20", 1, 2))
    assert page.have_more_results is True and page.last_appid == 20


def test_get_app_list_omits_unset_params():
    fake = FakeSteam(reply(body=app_list_body([1])))
    web(fake).get_app_list()
    assert "last_appid" not in fake.calls[0].params and "if_modified_since" not in fake.calls[0].params


def test_get_app_list_empty_response():
    fake = FakeSteam(reply(body={"response": {}}))
    page = web(fake).get_app_list()
    assert page.apps == () and page.have_more_results is False and page.last_appid is None


def test_get_app_list_requires_key_before_any_request():
    fake = FakeSteam()
    with pytest.raises(MissingCredentialError):
        web(fake, key=None).get_app_list()
    assert fake.calls == []


@pytest.mark.parametrize("n", [0, 50_001])
def test_get_app_list_validates_max_results(n):
    with pytest.raises(ValueError):
        web(FakeSteam()).get_app_list(max_results=n)


@pytest.mark.parametrize(
    "body",
    [[], {"nope": 1}, {"response": []}, {"response": {"apps": {}}}, {"response": {"apps": [{"appid": "1", "name": "x"}]}}],
)
def test_get_app_list_rejects_malformed(body):
    with pytest.raises(SteamResponseError):
        web(FakeSteam(reply(body=body))).get_app_list()


def test_iter_app_list_follows_pagination():
    fake = FakeSteam(
        reply(body=app_list_body([1, 2], more=True, last=2)),
        reply(body=app_list_body([3], more=False, last=3)),
    )
    assert [a.appid for a in web(fake).iter_app_list(max_results=2)] == [1, 2, 3]
    assert "last_appid" not in fake.calls[0].params
    assert fake.calls[1].params["last_appid"] == "2"


def test_iter_app_list_respects_max_pages():
    fake = FakeSteam(reply(body=app_list_body([1], more=True, last=1)))
    assert [a.appid for a in web(fake).iter_app_list(max_pages=1)] == [1]
    assert len(fake.calls) == 1


def test_iter_app_list_detects_stuck_cursor():
    fake = FakeSteam(
        reply(body=app_list_body([1], more=True, last=5)),
        reply(body=app_list_body([2], more=True, last=5)),
    )
    with pytest.raises(SteamResponseError, match="did not advance"):
        list(web(fake).iter_app_list())


def test_invalid_key_raises_auth_error_without_leaking_key():
    fake = FakeSteam(reply(403, text="<html><body>Access is denied. Retrying will not help. Please verify your <pre>key=</pre> parameter.</body></html>"))
    with pytest.raises(SteamAuthError) as info:
        web(fake).get_app_list()
    assert info.value.status == 403
    assert FAKE_KEY not in str(info.value)


def test_key_cannot_be_passed_as_param():
    with pytest.raises(ValueError):
        web(FakeSteam()).call("IStoreService", "GetAppList", 1, {"key": "x"})


# --- GetAppReviews ----------------------------------------------------------------

def test_get_app_reviews_request_and_response():
    fake = FakeSteam(reply(body={"response": {"review_score": 9, "total_reviews": 3}}))
    out = web(fake).get_app_reviews(620, language="english", filter_offtopic=True)
    (call,) = fake.calls
    assert call.path == "/IUserReviewsService/GetAppReviews/v1/"
    assert call.params == {"appid": "620", "language": "english", "filter_offtopic": "true", "key": FAKE_KEY}
    assert out == {"review_score": 9, "total_reviews": 3}


def test_get_app_reviews_without_key_sends_no_key():
    fake = FakeSteam(reply(body={"response": {}}))
    web(fake, key=None).get_app_reviews(620)
    assert "key" not in fake.calls[0].params


@pytest.mark.parametrize("appid", [0, -1, "620", True])
def test_get_app_reviews_validates_appid(appid):
    with pytest.raises(ValueError):
        web(FakeSteam()).get_app_reviews(appid)


# --- GetSupportedAPIList ---------------------------------------------------------------

def test_get_supported_methods_parses_api_list():
    body = {"apilist": {"interfaces": [
        {"name": "IStoreService", "methods": [
            {"name": "GetAppList", "version": 1, "httpmethod": "GET",
             "parameters": [{"name": "key"}, {"name": "max_results"}]},
        ]},
        {"name": "Broken", "methods": ["junk"]},
    ]}}
    fake = FakeSteam(reply(body=body))
    methods = web(fake, key=None).get_supported_methods()
    assert fake.calls[0].path == "/ISteamWebAPIUtil/GetSupportedAPIList/v1/"
    assert "key" not in fake.calls[0].params
    m = methods[("IStoreService", "GetAppList", 1)]
    assert m.http_method == "GET" and m.parameters == ("key", "max_results")
    assert len(methods) == 1


# --- Store appdetails --------------------------------------------------------------------

def test_app_details_success():
    fake = FakeSteam(reply(body={"620": {"success": True, "data": {"type": "game", "name": "Portal 2"}}}))
    store = SteamStoreAPI(make_transport(fake))
    assert store.get_app_details(620) == {"type": "game", "name": "Portal 2"}
    (call,) = fake.calls
    assert call.host == "store.steampowered.com" and call.path == "/api/appdetails"
    assert call.params == {"appids": "620", "cc": "us", "l": "english"}


def test_app_details_never_sends_api_key():
    fake = FakeSteam(reply(body={"620": {"success": True, "data": {}}}))
    tr = make_transport(fake)
    SteamWebAPI(tr, ApiKey(FAKE_KEY))  # a keyed client sharing the transport
    SteamStoreAPI(tr).get_app_details(620)
    assert "key" not in fake.calls[0].params and FAKE_KEY not in fake.calls[0].url


def test_app_details_unsuccessful_is_none():
    fake = FakeSteam(reply(body={"999999999": {"success": False}}))
    assert SteamStoreAPI(make_transport(fake)).get_app_details(999999999) is None


def test_app_details_overrides_region_and_language():
    fake = FakeSteam(reply(body={"620": {"success": True, "data": {}}}))
    SteamStoreAPI(make_transport(fake)).get_app_details(620, country="gb", language="german")
    assert fake.calls[0].params == {"appids": "620", "cc": "gb", "l": "german"}


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"620": None}, {"620": {"data": {}}}, {"620": {"success": True}}, {"620": {"success": True, "data": []}}],
)
def test_app_details_rejects_unexpected_shapes(body):
    with pytest.raises(SteamResponseError):
        SteamStoreAPI(make_transport(FakeSteam(reply(body=body)))).get_app_details(620)
