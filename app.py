import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

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

app.config["AUTH_API_BASE_URL"] = os.environ.get(
    "AUTH_API_BASE_URL", "https://auth-dev.clusterkeep.dev.net"
).strip()
app.config["AUTH_API_INTERNAL_URL"] = os.environ.get(
    "AUTH_API_INTERNAL_URL", "http://auth-api.clusterkeep-dev-priv.svc.cluster.local"
).strip()
app.config["CLUSTERKEEP_UI_URL"] = os.environ.get(
    "CLUSTERKEEP_UI_URL", "https://dev.clusterkeep.dev.net"
).strip()
app.config["STORAGE_UI_URL"] = os.environ.get(
    "STORAGE_UI_URL", "https://storage-dev.clusterkeep.dev.net"
).strip()
app.config["HEADLAMP_URL"] = os.environ.get("HEADLAMP_URL", "").strip()

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


def logout_url():
    query = urlencode({"post_logout_redirect_uri": app.config["CLUSTERKEEP_UI_URL"]})
    return f"{app.config['AUTH_API_BASE_URL'].rstrip('/')}/logout?{query}"


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
    response = requests.get(_internal_url("/api/v1/users"), headers=_auth_headers(), timeout=5)
    if response.status_code != 200:
        abort(502, "could not list users")

    users = [
        {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": "admin" if "admin" in user["groups"] else "user",
            "cluster_role": _cluster_role(user["groups"]),
        }
        for user in response.json()
    ]
    return render_template(
        "index.html",
        users=users,
        current_sub=g.current_sub,
        can_manage_cluster=g.can_manage_cluster,
        cluster_roles=CLUSTER_ROLES,
    )


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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
