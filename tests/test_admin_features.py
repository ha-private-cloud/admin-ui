import pytest
import responses as responses_lib

from app import app
from tests.test_app import AUTH_API_INTERNAL_URL, USERS_LIST, as_admin, client, mock_mint, mock_users_list

STORAGE = "http://storage-api.storage-dev-priv.svc.cluster.local/api/v1"
GIB = 1024**3
INVITES = [
    {"id": "inv-open", "note": "Sam", "status": "open", "created_by": "admin", "used_by": None,
     "created_at": "2026-11-17T10:00:00+00:00", "expires_at": "2026-11-24T10:00:00+00:00"},
    {"id": "inv-used", "note": None, "status": "used", "created_by": "admin", "used_by": "regular",
     "created_at": "2026-11-10T10:00:00+00:00", "expires_at": "2026-11-17T10:00:00+00:00"},
]


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setitem(app.config, "STORAGE_API_URL", STORAGE.removesuffix("/api/v1"))
    monkeypatch.setitem(app.config, "STORAGE_API_TOKEN", "storage-token")


def mock_index(rsps, vaults=None, invites=None):
    mock_mint(rsps)
    mock_users_list(rsps)
    rsps.add(responses_lib.GET, f"{AUTH_API_INTERNAL_URL}/api/v1/invites", json=INVITES if invites is None else invites)
    if vaults is not None:
        rsps.add(responses_lib.GET, f"{STORAGE}/admin/vaults", json={"items": vaults},
                 match=[responses_lib.matchers.header_matcher({"Authorization": "Bearer storage-token"})])


def test_users_show_quota_usage_and_status(client, storage):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_index(rsps, vaults=[{"user_id": "user-2", "quota_bytes": 10 * GIB, "used_bytes": 3 * GIB // 2}])
        body = client.get("/").get_data(as_text=True)
    assert "1.5 GB of 10 GB" in body
    assert 'name="quota_gb" value="10"' in body
    assert "Not set up yet" in body
    assert "Active" in body
    assert "Disable" in body


def test_storage_column_says_when_storage_is_unavailable(client, storage):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_index(rsps)
        rsps.add(responses_lib.GET, f"{STORAGE}/admin/vaults", status=503)
        body = client.get("/").get_data(as_text=True)
    assert "Storage details are unavailable" in body


def test_setting_a_quota_sends_bytes_to_storage_api(client, storage):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        rsps.add(responses_lib.PUT, f"{STORAGE}/admin/vaults/user-2/quota", json={},
                 match=[responses_lib.matchers.json_params_matcher({"quota_bytes": 20 * GIB})])
        response = client.post("/users/user-2/quota", data={"quota_gb": "20"})
    assert response.status_code == 302


@pytest.mark.parametrize("value", ["", "-1", "abc", "1000001"])
def test_bad_quotas_are_refused(client, storage, value):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        assert client.post("/users/user-2/quota", data={"quota_gb": value}).status_code == 400


def test_disabling_a_user_calls_auth_api(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        rsps.add(responses_lib.PATCH, f"{AUTH_API_INTERNAL_URL}/api/v1/users/user-2/active", json=USERS_LIST[1],
                 match=[responses_lib.matchers.json_params_matcher({"is_active": False})])
        assert client.post("/users/user-2/active", data={"active": "false"}).status_code == 302


def test_admins_cannot_disable_themselves(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        assert client.post("/users/user-1/active", data={"active": "false"}).status_code == 400


def test_creating_an_invite_shows_its_link_once(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        rsps.add(responses_lib.POST, f"{AUTH_API_INTERNAL_URL}/api/v1/invites",
                 json={"id": "inv-new", "token": "secret-token", "note": "Kim", "expires_at": "2026-11-24T10:00:00+00:00"},
                 status=201, match=[responses_lib.matchers.json_params_matcher({"note": "Kim"})])
        mock_index(rsps)
        response = client.post("/invites", data={"note": "Kim"})
    body = response.get_data(as_text=True)
    assert "https://dev.clusterkeep.dev.net/join?invite=secret-token" in body
    assert response.headers["Cache-Control"] == "no-store"


def test_invites_are_listed_and_open_ones_can_be_revoked(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_index(rsps)
        body = client.get("/").get_data(as_text=True)
    assert "Sam" in body
    assert 'action="/invites/inv-open/revoke"' in body
    assert 'action="/invites/inv-used/revoke"' not in body
    assert "Used by regular" in body


def test_revoking_an_invite_calls_auth_api(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        rsps.add(responses_lib.DELETE, f"{AUTH_API_INTERNAL_URL}/api/v1/invites/inv-open", status=204)
        assert client.post("/invites/inv-open/revoke").status_code == 302
