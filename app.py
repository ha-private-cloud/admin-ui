import os
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, abort, g, redirect, render_template, request, url_for

app = Flask(__name__)

APP_TITLE = os.environ.get("APP_TITLE", "ClusterKeep")
SESSION_COOKIE_NAME = "ck_sso"
ADMIN_GROUPS = ("cluster-admins", "admin")
VALID_ROLES = ("user", "admin")
CLUSTER_ADMIN_GROUP = "cluster-admins"
CLUSTER_ROLE_GROUPS = {
    "admin": "cluster-admins",
    "edit": "cluster-editors",
    "view": "cluster-viewers",
}
CLUSTER_ROLES = ("none", *CLUSTER_ROLE_GROUPS)

app.config["AUTH_API_INTERNAL_URL"] = os.environ.get(
    "AUTH_API_INTERNAL_URL", "http://auth-api.auth-dev-pub.svc.cluster.local"
).strip()
app.config["CLUSTERKEEP_UI_URL"] = os.environ.get(
    "CLUSTERKEEP_UI_URL", "https://dev.clusterkeep.dev.net"
).strip()
app.config["STORAGE_UI_URL"] = os.environ.get(
    "STORAGE_UI_URL", "https://storage-dev.clusterkeep.dev.net"
).strip()
app.config["HEADLAMP_URL"] = os.environ.get("HEADLAMP_URL", "").strip()
app.config["STORAGE_API_URL"] = os.environ.get("STORAGE_API_URL", "").strip()
app.config["STORAGE_API_TOKEN"] = os.environ.get("STORAGE_API_TOKEN", "").strip()

GIB = 1024**3
MAX_QUOTA_GB = 1_000_000

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
}


@app.after_request
def set_security_headers(response):
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


def _internal_url(path):
    return f"{app.config['AUTH_API_INTERNAL_URL'].rstrip('/')}{path}"


def _current_session():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        response = requests.get(_internal_url("/session"), cookies={SESSION_COOKIE_NAME: token}, timeout=3)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response.json()


def _mint_service_token(session_cookie_value):
    try:
        response = requests.post(
            _internal_url("/api/v1/service-tokens"),
            cookies={SESSION_COOKIE_NAME: session_cookie_value},
            json={"name": f"admin-ui-{uuid.uuid4().hex}", "audience": "auth-api", "scopes": []},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if response.status_code != 201:
        return None
    return response.json()["token"]


def _auth_headers():
    token = _mint_service_token(g.session_cookie)
    if token is None:
        abort(502, "could not authenticate to auth-api")
    return {"Authorization": f"Bearer {token}"}


def _is_admin(groups):
    return any(group in ADMIN_GROUPS for group in groups)


def _cluster_role(groups):
    for role, group in CLUSTER_ROLE_GROUPS.items():
        if group in groups:
            return role
    return "none"


def _fetch_user(headers, user_id):
    response = requests.get(_internal_url("/api/v1/users"), headers=headers, timeout=5)
    if response.status_code != 200:
        abort(502, "could not list users")
    target = next((u for u in response.json() if u["id"] == user_id), None)
    if target is None:
        abort(404)
    return target


def _patch_groups(headers, user_id, groups):
    response = requests.patch(
        _internal_url(f"/api/v1/users/{user_id}/groups"),
        json={"groups": sorted(groups)},
        headers=headers,
        timeout=5,
    )
    if response.status_code != 200:
        abort(502, "could not update groups")


def _storage(method, path, **kwargs):
    base = app.config["STORAGE_API_URL"]
    token = app.config["STORAGE_API_TOKEN"]
    if not (base and token):
        return None
    try:
        return requests.request(
            method,
            f"{base.rstrip('/')}/api/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
            cookies={SESSION_COOKIE_NAME: g.session_cookie},
            timeout=5,
            **kwargs,
        )
    except requests.RequestException:
        return None


def _storage_by_user():
    """None when storage-api isn't configured, False when it couldn't be reached."""
    if not (app.config["STORAGE_API_URL"] and app.config["STORAGE_API_TOKEN"]):
        return None
    response = _storage("GET", "admin/vaults")
    if response is None or response.status_code != 200:
        return False
    return {v["user_id"]: v for v in response.json().get("items", [])}


def _invites(headers):
    try:
        response = requests.get(_internal_url("/api/v1/invites"), headers=headers, timeout=5)
    except requests.RequestException:
        return None
    return response.json() if response.status_code == 200 else None


def _gib(value):
    amount = value / GIB
    return f"{amount:.0f}" if amount >= 10 or amount == int(amount) else f"{amount:.1f}"


def _render_index(new_invite_link=None):
    headers = _auth_headers()
    response = requests.get(_internal_url("/api/v1/users"), headers=headers, timeout=5)
    if response.status_code != 200:
        abort(502, "could not list users")
    storage = _storage_by_user()

    users = []
    for user in response.json():
        vault = storage.get(user["id"]) if storage else None
        users.append(
            {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "role": "admin" if "admin" in user["groups"] else "user",
                "cluster_role": _cluster_role(user["groups"]),
                "is_active": user.get("is_active", True),
                "storage": {
                    "used": _gib(vault["used_bytes"]),
                    "quota": _gib(vault["quota_bytes"]),
                    "quota_gb": _gib(vault["quota_bytes"]),
                } if vault else None,
            }
        )
    return render_template(
        "index.html",
        users=users,
        storage_state="off" if storage is None else "down" if storage is False else "ok",
        invites=_invites(headers),
        new_invite_link=new_invite_link,
        current_sub=g.current_sub,
        can_manage_cluster=g.can_manage_cluster,
        cluster_roles=CLUSTER_ROLES,
    )


def logout_url():
    return f"{app.config['CLUSTERKEEP_UI_URL'].rstrip('/')}/logout"


@app.context_processor
def inject_globals():
    return {
        "title": APP_TITLE,
        "year": datetime.now(timezone.utc).year,
        "headlamp_url": app.config["HEADLAMP_URL"],
        "storage_ui_url": app.config["STORAGE_UI_URL"],
        "logout_url": logout_url(),
    }


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html", home_url=app.config["CLUSTERKEEP_UI_URL"]), 404


@app.before_request
def require_admin():
    if request.path == "/healthz" or request.path.startswith("/static/"):
        return None

    session = _current_session()
    if session is None or not _is_admin(session.get("groups", [])):
        abort(404)

    g.current_sub = session["sub"]
    g.can_manage_cluster = CLUSTER_ADMIN_GROUP in session.get("groups", [])
    g.current_username = session["preferred_username"]
    g.session_cookie = request.cookies.get(SESSION_COOKIE_NAME)


@app.get("/")
def index():
    return _render_index()


@app.post("/users/<user_id>/role")
def set_role(user_id):
    role = request.form.get("role", "")
    if role not in VALID_ROLES:
        abort(400)
    if user_id == g.current_sub and role != "admin":
        abort(400, "cannot remove your own admin access")

    headers = _auth_headers()
    groups = set(_fetch_user(headers, user_id)["groups"])
    if role == "admin":
        groups.add("admin")
    else:
        groups.discard("admin")

    _patch_groups(headers, user_id, groups)
    return redirect(url_for("index"))


@app.post("/users/<user_id>/cluster-role")
def set_cluster_role(user_id):
    role = request.form.get("cluster_role", "")
    if role not in CLUSTER_ROLES:
        abort(400)
    if not g.can_manage_cluster:
        abort(403)
    if user_id == g.current_sub:
        abort(400, "cannot change your own cluster access")

    headers = _auth_headers()
    groups = set(_fetch_user(headers, user_id)["groups"]) - set(CLUSTER_ROLE_GROUPS.values())
    if role != "none":
        groups.add(CLUSTER_ROLE_GROUPS[role])

    _patch_groups(headers, user_id, groups)
    return redirect(url_for("index"))


@app.post("/users/<user_id>/quota")
def set_quota(user_id):
    raw = request.form.get("quota_gb", "").strip()
    try:
        quota_gb = float(raw)
    except ValueError:
        abort(400)
    if not 0 <= quota_gb <= MAX_QUOTA_GB:
        abort(400)
    response = _storage("PUT", f"admin/vaults/{user_id}/quota", json={"quota_bytes": int(quota_gb * GIB)})
    if response is None or response.status_code != 200:
        abort(502, "could not update the quota")
    return redirect(url_for("index"))


@app.post("/users/<user_id>/active")
def set_active(user_id):
    value = request.form.get("active", "")
    if value not in ("true", "false"):
        abort(400)
    if user_id == g.current_sub:
        abort(400, "cannot disable your own account")
    response = requests.patch(
        _internal_url(f"/api/v1/users/{user_id}/active"),
        json={"is_active": value == "true"},
        headers=_auth_headers(),
        timeout=5,
    )
    if response.status_code == 403:
        abort(403)
    if response.status_code != 200:
        abort(502, "could not change the account")
    return redirect(url_for("index"))


@app.post("/invites")
def create_invite():
    note = request.form.get("note", "").strip()[:255]
    response = requests.post(
        _internal_url("/api/v1/invites"), json={"note": note} if note else {}, headers=_auth_headers(), timeout=5
    )
    if response.status_code != 201:
        abort(502, "could not create the invite")
    link = f"{app.config['CLUSTERKEEP_UI_URL'].rstrip('/')}/join?invite={response.json()['token']}"
    page = app.make_response(_render_index(new_invite_link=link))
    page.headers["Cache-Control"] = "no-store"
    return page


@app.post("/invites/<invite_id>/revoke")
def revoke_invite(invite_id):
    response = requests.delete(_internal_url(f"/api/v1/invites/{invite_id}"), headers=_auth_headers(), timeout=5)
    if response.status_code not in (204, 409):
        abort(502, "could not withdraw the invite")
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
