import re
from pathlib import Path

import pytest
import responses as responses_lib

from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
STATIC = REPO_ROOT / "static"

AUTH_API_INTERNAL_URL = "http://auth-api.clusterkeep-dev-priv.svc.cluster.local"

ADMIN_SESSION = {
    "sub": "user-1",
    "preferred_username": "admin",
    "email": "admin@example.com",
    "groups": ["admin"],
}

CLUSTER_ADMIN_SESSION = {
    "sub": "user-3",
    "preferred_username": "clusteradmin",
    "email": "clusteradmin@example.com",
    "groups": ["cluster-admins"],
}

REGULAR_SESSION = {
    "sub": "user-2",
    "preferred_username": "regular",
    "email": "regular@example.com",
    "groups": [],
}

USERS_LIST = [
    {
        "id": "user-1",
        "username": "admin",
        "email": "admin@example.com",
        "groups": ["admin"],
        "is_active": True,
        "must_change_password": False,
    },
    {
        "id": "user-2",
        "username": "regular",
        "email": "regular@example.com",
        "groups": [],
        "is_active": True,
        "must_change_password": False,
    },
]


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def mock_session(rsps, session):
    rsps.add(responses_lib.GET, f"{AUTH_API_INTERNAL_URL}/session", json=session, status=200)


def mock_mint(rsps, token="service-token"):
    rsps.add(
        responses_lib.POST,
        f"{AUTH_API_INTERNAL_URL}/api/v1/service-tokens",
        json={"token": token, "name": "admin-ui", "audience": "auth-api", "scopes": [],
              "algorithm": "ML-DSA-65", "expires_in": 3600},
        status=201,
    )


def mock_users_list(rsps, users=None):
    rsps.add(responses_lib.GET, f"{AUTH_API_INTERNAL_URL}/api/v1/users", json=users or USERS_LIST, status=200)


def as_admin(client, rsps):
    client.set_cookie("ck_sso", "admin-token")
    mock_session(rsps, ADMIN_SESSION)


def as_regular_user(client, rsps):
    client.set_cookie("ck_sso", "regular-token")
    mock_session(rsps, REGULAR_SESSION)


def test_healthz_is_reachable_without_a_session(client):
    assert client.get("/healthz").status_code == 200


def test_favicon_is_served_without_a_session(client):
    resp = client.get("/static/images/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["Content-Type"]


def test_security_headers_on_every_response(client):
    for path in ["/healthz", "/static/css/style.css"]:
        headers = client.get(path).headers
        assert headers["X-Content-Type-Options"] == "nosniff", path
        assert headers["X-Frame-Options"] == "DENY", path
        assert headers["Referrer-Policy"] == "no-referrer", path
        assert "Content-Security-Policy" in headers, path


def test_content_security_policy_stays_strict(client):
    """Relaxing any of these is a real change in exposure - do it knowingly."""
    csp = client.get("/healthz").headers["Content-Security-Policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


def test_no_inline_scripts_or_handlers():
    """CSP blocks these at runtime; catch them at author time instead."""
    for template in TEMPLATES.rglob("*.html"):
        markup = template.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", markup), (
            f"{template.name} has an inline <script>; CSP blocks it - put the "
            "code in static/js/ and load it with src="
        )
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", markup), (
            f"{template.name} has an inline event handler; CSP blocks it"
        )


def test_logo_and_favicon_geometry_match():
    """Nothing else would catch the two copies of the mark drifting apart."""
    inline = (TEMPLATES / "partials" / "logo.html").read_text()
    favicon = (STATIC / "images" / "favicon.svg").read_text()
    assert re.findall(r'd="([^"]+)"', inline) == re.findall(r'd="([^"]+)"', favicon)


FETCHING_TAGS = r"link|script|img|iframe|source|video|audio|embed|object|track"


def test_templates_request_no_third_party_assets():
    tags = re.compile(rf"<(?:{FETCHING_TAGS})\b[^>]*>", re.I | re.S)
    urls = re.compile(r'(?:href|src)\s*=\s*"(https?://[^"]+)"', re.I)
    for template in TEMPLATES.rglob("*.html"):
        for tag in tags.findall(template.read_text()):
            for url in urls.findall(tag):
                assert False, f"{template.name} loads a remote asset: {url}"


def test_fonts_are_self_hosted():
    """@font-face sources must be local and must actually exist."""
    css = (STATIC / "css" / "style.css").read_text()
    sources = re.findall(r'src:\s*url\("([^"]+)"\)', css)
    assert sources, "no @font-face rules found in the built stylesheet"
    for src in sources:
        assert not src.startswith("http"), f"remote font source: {src}"
        resolved = (STATIC / "css" / src).resolve()
        assert resolved.is_file(), f"missing font file: {resolved}"


def test_stylesheet_is_built_from_current_source():
    """Marker classes the templates rely on must be in the build."""
    css = (STATIC / "css" / "style.css").read_text()
    for marker in [".mark-lg .brand-mark", "Cinzel", "Inter"]:
        assert marker in css, f"{marker!r} missing - run `npm run build:css`"


LOCAL_CLASSES = {"brand-mark", "mark-lg", "group", "antialiased"}


def tailwind_selector(cls):
    """Keeps the variant prefix (`hover:bg-x` -> `.hover\\:bg-x:hover`) - do not strip it."""
    return "." + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", cls)


def tailwind_classes_used():
    used = set()
    for template in TEMPLATES.rglob("*.html"):
        for value in re.findall(r'class="([^"]*)"', template.read_text()):
            used.update(c for c in value.split() if c not in LOCAL_CLASSES)
    return used


def test_every_utility_used_by_a_template_is_in_the_build():
    """style.css is committed and CI never rebuilds it, so it can silently go stale after a template edit."""
    css = (STATIC / "css" / "style.css").read_text()
    missing = sorted(
        cls for cls in tailwind_classes_used() if tailwind_selector(cls) not in css
    )
    assert not missing, (
        f"utilities used in templates but absent from style.css: {missing}\n"
        "run `npm run build:css` and commit the result"
    )


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


def test_user_without_an_admin_group_gets_404(client):
    with responses_lib.RequestsMock() as rsps:
        as_regular_user(client, rsps)
        assert client.get("/").status_code == 404


def assert_not_found_page(response):
    body = response.get_data(as_text=True)
    assert response.status_code == 404
    assert "Page not found" in body
    for leaked in ("Sign out", "Open Headlamp", "Back to Storage", "Users", "Cluster access"):
        assert leaked not in body


def test_signed_out_visitor_sees_the_not_found_page(client):
    assert_not_found_page(client.get("/"))


def test_non_admin_sees_the_not_found_page(client):
    with responses_lib.RequestsMock() as rsps:
        as_regular_user(client, rsps)
        assert_not_found_page(client.get("/"))


def test_unknown_path_for_an_admin_shows_the_not_found_page(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        assert_not_found_page(client.get("/does-not-exist"))


def test_not_found_page_links_back_to_clusterkeep(client):
    body = client.get("/").get_data(as_text=True)
    assert f'href="{app.config["CLUSTERKEEP_UI_URL"]}"' in body
    assert "Back to ClusterKeep" in body


def test_admin_group_grants_access_and_lists_users(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        response = client.get("/")
        assert response.status_code == 200
        assert b"regular" in response.data


def test_each_mint_uses_a_distinct_token_name(client):
    """auth-api rejects a second token with the same (user, name) pair."""
    import json

    names = []
    for _ in range(2):
        with responses_lib.RequestsMock() as rsps:
            as_admin(client, rsps)
            mock_mint(rsps)
            mock_users_list(rsps)
            client.get("/")
            mint_call = [c for c in rsps.calls if c.request.url.endswith("/api/v1/service-tokens")][0]
            names.append(json.loads(mint_call.request.body)["name"])

    assert len(set(names)) == 2


def test_cluster_admins_group_also_grants_access(client):
    with responses_lib.RequestsMock() as rsps:
        client.set_cookie("ck_sso", "cluster-admin-token")
        mock_session(rsps, CLUSTER_ADMIN_SESSION)
        mock_mint(rsps)
        mock_users_list(rsps)
        assert client.get("/").status_code == 200


def test_promoting_a_user_adds_the_admin_group_without_dropping_others(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps, users=[{**USERS_LIST[1], "groups": ["viewers"]}])
        rsps.add(
            responses_lib.PATCH,
            f"{AUTH_API_INTERNAL_URL}/api/v1/users/user-2/groups",
            json={**USERS_LIST[1], "groups": ["admin", "viewers"]},
            status=200,
        )
        response = client.post("/users/user-2/role", data={"role": "admin"})
        assert response.status_code == 302

        import json

        patch_call = [c for c in rsps.calls if c.request.method == "PATCH"][0]
        assert sorted(json.loads(patch_call.request.body)["groups"]) == ["admin", "viewers"]


def test_admin_cannot_demote_themselves(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        response = client.post("/users/user-1/role", data={"role": "user"})
        assert response.status_code == 400


def test_set_role_rejects_unknown_role(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        response = client.post("/users/user-2/role", data={"role": "superadmin"})
        assert response.status_code == 400


def test_unknown_target_user_returns_404(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        response = client.post("/users/does-not-exist/role", data={"role": "admin"})
        assert response.status_code == 404


def test_headlamp_link_shown_when_configured(client, monkeypatch):
    monkeypatch.setitem(app.config, "HEADLAMP_URL", "https://headlamp.clusterkeep.dev.net")
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        body = client.get("/").get_data(as_text=True)
    assert "https://headlamp.clusterkeep.dev.net" in body


def test_headlamp_link_hidden_when_not_configured(client, monkeypatch):
    monkeypatch.setitem(app.config, "HEADLAMP_URL", "")
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        body = client.get("/").get_data(as_text=True)
    assert "Open Headlamp" not in body


def test_storage_ui_link_shown_when_configured(client, monkeypatch):
    monkeypatch.setitem(app.config, "STORAGE_UI_URL", "https://storage-dev.clusterkeep.dev.net")
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        body = client.get("/").get_data(as_text=True)
    assert "https://storage-dev.clusterkeep.dev.net" in body


def test_storage_ui_link_hidden_when_not_configured(client, monkeypatch):
    monkeypatch.setitem(app.config, "STORAGE_UI_URL", "")
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        body = client.get("/").get_data(as_text=True)
    assert "Back to Storage" not in body


def test_sign_out_link_points_to_auth_apis_logout(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        body = client.get("/").get_data(as_text=True)
    assert (
        "https://auth-dev.clusterkeep.dev.net/logout"
        "?post_logout_redirect_uri=https%3A%2F%2Fdev.clusterkeep.dev.net"
    ) in body


def test_failed_token_mint_returns_502(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        rsps.add(responses_lib.POST, f"{AUTH_API_INTERNAL_URL}/api/v1/service-tokens", status=401)
        assert client.get("/").status_code == 502


def as_cluster_admin(client, rsps):
    client.set_cookie("ck_sso", "cluster-admin-token")
    mock_session(rsps, CLUSTER_ADMIN_SESSION)


def test_cluster_admin_sets_cluster_access_replacing_the_previous_level(client):
    import json

    with responses_lib.RequestsMock() as rsps:
        as_cluster_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps, users=[{**USERS_LIST[1], "groups": ["admin", "cluster-viewers"]}])
        rsps.add(
            responses_lib.PATCH,
            f"{AUTH_API_INTERNAL_URL}/api/v1/users/user-2/groups",
            json={**USERS_LIST[1], "groups": ["admin", "cluster-editors"]},
            status=200,
        )
        response = client.post("/users/user-2/cluster-role", data={"cluster_role": "edit"})
        assert response.status_code == 302

        patch_call = [c for c in rsps.calls if c.request.method == "PATCH"][0]
        assert json.loads(patch_call.request.body)["groups"] == ["admin", "cluster-editors"]


def test_removing_cluster_access_drops_every_cluster_group(client):
    import json

    with responses_lib.RequestsMock() as rsps:
        as_cluster_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps, users=[{**USERS_LIST[1], "groups": ["cluster-admins", "viewers"]}])
        rsps.add(
            responses_lib.PATCH,
            f"{AUTH_API_INTERNAL_URL}/api/v1/users/user-2/groups",
            json={**USERS_LIST[1], "groups": ["viewers"]},
            status=200,
        )
        client.post("/users/user-2/cluster-role", data={"cluster_role": "none"})

        patch_call = [c for c in rsps.calls if c.request.method == "PATCH"][0]
        assert json.loads(patch_call.request.body)["groups"] == ["viewers"]


def test_app_admin_cannot_change_cluster_access(client):
    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        assert client.post("/users/user-2/cluster-role", data={"cluster_role": "admin"}).status_code == 403


def test_cluster_admin_cannot_change_their_own_cluster_access(client):
    with responses_lib.RequestsMock() as rsps:
        as_cluster_admin(client, rsps)
        assert client.post("/users/user-3/cluster-role", data={"cluster_role": "none"}).status_code == 400


def test_set_cluster_role_rejects_unknown_levels(client):
    with responses_lib.RequestsMock() as rsps:
        as_cluster_admin(client, rsps)
        assert client.post("/users/user-2/cluster-role", data={"cluster_role": "root"}).status_code == 400


def test_cluster_access_is_editable_only_for_cluster_admins(client):
    with responses_lib.RequestsMock() as rsps:
        as_cluster_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        assert b'name="cluster_role"' in client.get("/").data

    with responses_lib.RequestsMock() as rsps:
        as_admin(client, rsps)
        mock_mint(rsps)
        mock_users_list(rsps)
        assert b'name="cluster_role"' not in client.get("/").data
