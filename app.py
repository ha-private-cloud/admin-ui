import os

import requests
from flask import Flask, abort, g, redirect, render_template, request, url_for
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

app = Flask(__name__)

SESSION_COOKIE_NAME = "ck_sso"
VALID_ROLES = ("user", "admin")

app.config["AUTH_API_BASE_URL"] = os.environ.get(
    "AUTH_API_BASE_URL", "https://auth-dev.clusterkeep.dev.net"
).strip()
app.config["AUTH_API_INTERNAL_URL"] = os.environ.get(
    "AUTH_API_INTERNAL_URL", "http://auth-api.clusterkeep-dev-priv.svc.cluster.local"
).strip()
app.config["DATABASE_URL"] = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://admin_ui:admin_ui@postgres:5432/admin_ui"
).strip()
app.config["ADMIN_UI_BOOTSTRAP_USERNAME"] = os.environ.get("ADMIN_UI_BOOTSTRAP_USERNAME", "").strip()

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


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    role: Mapped[str] = mapped_column(String(16), default="user")


def _db_session():
    engine = create_engine(app.config["DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _upsert_user(db, sub, username, email):
    user = db.get(User, sub)
    if user is None:
        user = User(sub=sub, username=username, email=email)
        db.add(user)
    else:
        user.username = username
        user.email = email
    _bootstrap_admin(user)
    db.commit()
    return user


def _bootstrap_admin(user):
    bootstrap_usernames = {
        name.strip() for name in app.config["ADMIN_UI_BOOTSTRAP_USERNAME"].split(",") if name.strip()
    }
    if user.username in bootstrap_usernames:
        user.role = "admin"


def _current_session():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        response = requests.get(
            f"{app.config['AUTH_API_INTERNAL_URL'].rstrip('/')}/session",
            cookies={SESSION_COOKIE_NAME: token},
            timeout=3,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response.json()


@app.before_request
def require_admin():
    if request.path == "/healthz":
        return None

    session = _current_session()
    if session is None:
        abort(404)

    db = _db_session()
    try:
        user = _upsert_user(db, session["sub"], session["preferred_username"], session.get("email"))
        if user.role != "admin":
            abort(404)
        g.current_sub = user.sub
        g.current_username = user.username
    finally:
        db.close()


@app.get("/")
def index():
    db = _db_session()
    try:
        users = db.scalars(select(User).order_by(User.username)).all()
        return render_template("index.html", users=users, current_sub=g.current_sub)
    finally:
        db.close()


@app.post("/users/<sub>/role")
def set_role(sub):
    role = request.form.get("role", "")
    if role not in VALID_ROLES:
        abort(400)
    if sub == g.current_sub and role != "admin":
        abort(400, "cannot remove your own admin access")

    db = _db_session()
    try:
        user = db.get(User, sub)
        if user is None:
            abort(404)
        user.role = role
        db.commit()
    finally:
        db.close()
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
