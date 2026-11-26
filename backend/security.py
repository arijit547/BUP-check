import hashlib
import hmac
import os
import secrets
from typing import Literal

from fastapi import Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthStatus(BaseModel):
    auth_required: bool
    authenticated: bool
    username: str | None = None
    role: Literal["operator", "viewer"] | None = None


def verify_pbkdf2_password(password: str, stored_hash: str) -> bool:
    """Verify salted PBKDF2-HMAC-SHA256 hash formatted as 'pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>'."""
    try:
        scheme, iter_s, salt_hex, expected_hex = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(derived.hex(), expected_hex)
    except Exception:
        return False


def create_pbkdf2_hash(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


class OperatorAuth:
    """Env-configured operator authorization supporting session cookie or Bearer token when enabled."""

    def __init__(self) -> None:
        self._ephemeral_secret = secrets.token_hex(32)
        self.cookie_name = "bup_operator_session"

    @property
    def mode(self) -> str:
        return os.environ.get("OPERATOR_AUTH_MODE", "local_open").strip().lower()

    @property
    def auth_required(self) -> bool:
        return self.mode in ("required", "strict", "session") or bool(
            os.environ.get("OPERATOR_PASSWORD_HASH") or os.environ.get("OPERATOR_TOKEN_SHA256")
        )

    @property
    def signing_secret(self) -> str:
        return os.environ.get("SESSION_SECRET", "") or self._ephemeral_secret

    @property
    def expected_username(self) -> str:
        return os.environ.get("OPERATOR_USERNAME", "operator")

    def verify_password(self, username: str, password: str) -> bool:
        pw_hash = os.environ.get("OPERATOR_PASSWORD_HASH", "")
        if not pw_hash:
            return False
        return hmac.compare_digest(username, self.expected_username) and verify_pbkdf2_password(password, pw_hash)

    def verify_bearer(self, authorization: str | None) -> bool:
        token_sha = os.environ.get("OPERATOR_TOKEN_SHA256", "").strip().lower()
        if not token_sha or not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[7:].strip()
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest().lower()
        return hmac.compare_digest(actual, token_sha)

    def sign_session(self, username: str) -> str:
        nonce = secrets.token_hex(12)
        payload = f"{username}:{nonce}"
        sig = hmac.new(self.signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def verify_session(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            username, nonce, sig = token.rsplit(":", 2)
            payload = f"{username}:{nonce}"
            expected = hmac.new(self.signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected):
                return username
        except ValueError:
            return None
        return None

    def status(self, request: Request) -> AuthStatus:
        if not self.auth_required:
            return AuthStatus(auth_required=False, authenticated=True, username="local-operator", role="operator")
        auth_hdr = request.headers.get("authorization")
        if self.verify_bearer(auth_hdr):
            return AuthStatus(auth_required=True, authenticated=True, username=self.expected_username, role="operator")
        user = self.verify_session(request.cookies.get(self.cookie_name))
        if user:
            return AuthStatus(auth_required=True, authenticated=True, username=user, role="operator")
        return AuthStatus(auth_required=True, authenticated=False, username=None, role=None)

    def require_operator(self, request: Request) -> str:
        st = self.status(request)
        if not st.authenticated:
            raise HTTPException(401, "Operator authentication required for consequential action.")
        return st.username or "operator"

    def set_cookie(self, response: Response, username: str) -> None:
        value = self.sign_session(username)
        secure = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
        response.set_cookie(
            key=self.cookie_name,
            value=value,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
