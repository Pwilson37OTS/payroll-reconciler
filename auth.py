"""Lightweight authentication gate for the reconciler.

A shared app password plus an optional email allowlist. Credentials live in
Streamlit secrets (``.streamlit/secrets.toml`` locally, or the app's Secrets
settings on Community Cloud), never in code.

Example secrets.toml
--------------------
    app_password = "change-me"
    allowed_emails = ["pwil@oaktreestaffing.com"]

If ``allowed_emails`` is omitted or empty, any email is accepted as a label and
only the password is enforced.
"""

from __future__ import annotations

import hmac

import streamlit as st

# Label used for the "user" when the login gate is turned off (internal host).
_ANON_USER = "internal"


def login_required() -> bool:
    """Whether the login gate is enforced.

    Defaults to True. Set ``require_login = false`` in secrets to turn the gate
    OFF — intended only for an internal host whose network reach is tightly
    restricted (e.g. a Windows Firewall allowlist on the app's port), where
    access is controlled by the network rather than a password.
    """
    val = st.secrets.get("require_login", True)
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(val)


def _password_correct(entered: str) -> bool:
    expected = st.secrets.get("app_password", "")
    if not expected:
        st.error(
            "No app password is configured. Set `app_password` in the app's "
            "secrets before using the reconciler."
        )
        return False
    # Constant-time comparison to avoid leaking length/prefix via timing.
    return hmac.compare_digest(str(entered), str(expected))


def _email_allowed(email: str) -> bool:
    allowed = st.secrets.get("allowed_emails", [])
    if not allowed:
        return True  # allowlist not configured -> email is a free-text label
    return email.strip().lower() in {str(e).strip().lower() for e in allowed}


def require_login() -> str:
    """Render the login gate. Returns the signed-in user's email.

    Halts the script (``st.stop``) until the user is authenticated. When the
    gate is disabled (``require_login = false``), returns a fixed label and
    renders nothing.
    """
    if not login_required():
        return _ANON_USER
    if st.session_state.get("authenticated"):
        return st.session_state.get("user_email", "")

    st.title("🔒 Cost Reconciler")
    st.caption("Bullhorn vs. payroll monthly cost reconciliation")

    with st.form("login"):
        email = st.text_input("Your work email", placeholder="you@oaktreestaffing.com")
        password = st.text_input("App password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if not _email_allowed(email):
            st.error("That email is not on the access list for this app.")
        elif not _password_correct(password):
            st.error("Incorrect password.")
        else:
            st.session_state["authenticated"] = True
            st.session_state["user_email"] = email.strip()
            st.rerun()

    st.stop()


def logout_button() -> None:
    """Render a sign-out control in the sidebar (nothing when the gate is off)."""
    if not login_required():
        return
    with st.sidebar:
        st.write(f"Signed in as **{st.session_state.get('user_email','')}**")
        if st.button("Sign out"):
            for k in ("authenticated", "user_email"):
                st.session_state.pop(k, None)
            st.rerun()
