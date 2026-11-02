"""Authentication and role checks for protected routes."""

from functools import wraps

from flask import abort, flash, g, redirect, request, url_for


def _login_redirect():
    flash("Please log in to continue.", "warning")
    target = request.path if request.method == "GET" else ""
    return redirect(url_for("auth.login", next=target))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        return view(*args, **kwargs)

    return wrapped


def client_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user.role != "client":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user.role != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def approved_planner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user.role != "planner":
            abort(403)
        profile = g.user.planner_profile
        if profile is None or profile.approval_status != "approved":
            flash("Your planner account is not approved for this action.", "warning")
            return redirect(url_for("planner.pending"))
        return view(*args, **kwargs)

    return wrapped


def attendant_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user.role != "attendant" or not g.user.is_active_attendant:
            abort(403)
        if g.user.gate_profile.must_change_password:
            flash("Choose a new password before you start scanning tickets.", "warning")
            return redirect(url_for("auth.change_password"))
        return view(*args, **kwargs)

    return wrapped


def planner_account_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _login_redirect()
        if g.user.role != "planner":
            abort(403)
        return view(*args, **kwargs)

    return wrapped
