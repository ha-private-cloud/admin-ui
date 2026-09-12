import pytest
import responses as responses_lib

from app import app

AUTH_API_INTERNAL_URL = "http://auth-api.clusterkeep-dev-priv.svc.cluster.local"

BOOTSTRAP_SESSION = {
    "sub": "user-1",
    "preferred_username": "admin",
    "email": "admin@example.com",
    "groups": [],
}

OTHER_SESSION = {
    "sub": "user-2",
    "preferred_username": "regular",
    "email": "regular@example.com",
    "groups": [],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    app.config["TESTING"] = True
    monkeypatch.setitem(app.config, "DATABASE_URL", f"sqlite:///{tmp_path}/admin_ui.db")
    monkeypatch.setitem(app.config, "ADMIN_UI_BOOTSTRAP_USERNAME", "admin")
    with app.test_client() as c:
        yield c


def mock_session(rsps, session):
    rsps.add(responses_lib.GET, f"{AUTH_API_INTERNAL_URL}/session", json=session, status=200)


def as_bootstrap_admin(client, rsps):
    client.set_cookie("ck_sso", "bootstrap-token")
    mock_session(rsps, BOOTSTRAP_SESSION)


def as_other_user(client, rsps):
    client.set_cookie("ck_sso", "other-token")
    mock_session(rsps, OTHER_SESSION)


def test_healthz_is_reachable_without_a_session(client):
    assert client.get("/healthz").status_code == 200


def test_no_cookie_returns_404(client):
    assert client.get("/").status_code == 404


def test_invalid_session_returns_404(client):
    client.set_cookie("ck_sso", "bad-token")
    with responses_lib.RequestsMock() as rsps:
        rsps.add(responses_lib.GET, f"{AUTH_API_INTERNAL_URL}/session", status=401)
        assert client.get("/").status_code == 404


def test_auth_api_unreachable_fails_closed(client):
    client.set_cookie("ck_sso", "some-token")
    with responses_lib.RequestsMock() as rsps:
        import requests

        rsps.add(
            responses_lib.GET,
            f"{AUTH_API_INTERNAL_URL}/session",
            body=requests.exceptions.ConnectionError("unreachable"),
        )
        assert client.get("/").status_code == 404


def test_non_admin_user_gets_404(client):
    with responses_lib.RequestsMock() as rsps:
        as_other_user(client, rsps)
        assert client.get("/").status_code == 404


def test_bootstrap_username_gets_admin_access(client):
    with responses_lib.RequestsMock() as rsps:
        as_bootstrap_admin(client, rsps)
        response = client.get("/")
        assert response.status_code == 200
        assert b"admin" in response.data


def test_admin_can_promote_another_user(client):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        as_other_user(client, rsps)
        client.get("/")  # just-in-time provisions user-2 as role "user"

    with responses_lib.RequestsMock() as rsps:
        as_bootstrap_admin(client, rsps)
        response = client.post("/users/user-2/role", data={"role": "admin"}, follow_redirects=True)
        assert response.status_code == 200
        assert b"regular" in response.data

    with responses_lib.RequestsMock() as rsps:
        as_other_user(client, rsps)
        assert client.get("/").status_code == 200


def test_admin_cannot_demote_themselves(client):
    with responses_lib.RequestsMock() as rsps:
        as_bootstrap_admin(client, rsps)
        response = client.post("/users/user-1/role", data={"role": "user"})
        assert response.status_code == 400


def test_set_role_rejects_unknown_role(client):
    with responses_lib.RequestsMock() as rsps:
        as_bootstrap_admin(client, rsps)
        response = client.post("/users/user-1/role", data={"role": "superadmin"})
        assert response.status_code == 400


def test_bootstrap_username_accepts_a_comma_separated_list(client, monkeypatch):
    monkeypatch.setitem(app.config, "ADMIN_UI_BOOTSTRAP_USERNAME", "admin, regular")
    with responses_lib.RequestsMock() as rsps:
        as_other_user(client, rsps)
        assert client.get("/").status_code == 200
