from __future__ import annotations

import hmac
import os

import streamlit as st


def _secret_or_env(name: str) -> str | None:
    """Read a secret from the environment first, then Streamlit secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return str(value) if value else None


def _password_matches(candidate: str, expected: str) -> bool:
    """Use a constant-time comparison for the shared app password."""
    return hmac.compare_digest(
        candidate.encode("utf-8"),
        expected.encode("utf-8"),
    )


def require_password() -> None:
    """Require the shared app password before rendering any protected content."""
    expected = _secret_or_env("APP_PASSWORD")

    if not expected:
        st.error(
            "APP_PASSWORD is not configured. Add it to Streamlit Secrets "
            "or set the APP_PASSWORD environment variable."
        )
        st.code('APP_PASSWORD = "replace-with-a-strong-password"', language="toml")
        st.stop()

    if st.session_state.get("app_authenticated", False):
        return

    # Keep protected navigation hidden until authentication succeeds.
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {
            display: none;
        }
        .block-container {
            max-width: 34rem;
            padding-top: clamp(4rem, 12vh, 8rem);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("ERAU Energy Dashboard")
    st.caption("Authorized access only")

    with st.form("app_password_login", clear_on_submit=False):
        password = st.text_input(
            "Password",
            type="password",
            placeholder="Enter dashboard password",
            autocomplete="current-password",
        )
        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if _password_matches(password, expected):
            st.session_state["app_authenticated"] = True
            st.session_state.pop("app_login_error", None)
            st.rerun()
        else:
            st.session_state["app_login_error"] = True

    if st.session_state.get("app_login_error"):
        st.error("Incorrect password.")

    st.stop()


def render_logout_button() -> None:
    """Render a logout button in the sidebar for authenticated users."""
    if not st.session_state.get("app_authenticated", False):
        return

    st.sidebar.divider()
    if st.sidebar.button("Log out", use_container_width=True):
        for key in list(st.session_state.keys()):
            if key.startswith("app_") or key.startswith("admin_"):
                st.session_state.pop(key, None)
        st.rerun()
