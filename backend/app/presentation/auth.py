"""Same-origin signed operator sessions; authorization also covers image requests."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
_attempts: dict[str, list[float]] = defaultdict(list)
_lock = threading.Lock()


def _same_secret(left: object, right: object) -> bool:
    """compare_digest(str, str) rejects non-ASCII user-controlled input."""
    return isinstance(left, str) and isinstance(right, str) and secrets.compare_digest(
        left.encode("utf-8"), right.encode("utf-8"))


class LoginRequest(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(min_length=1, max_length=512)


def settings_for(request: Request):
    from app.presentation.dependencies import get_container
    resolver = request.app.dependency_overrides.get(get_container, get_container)
    return resolver().settings


def serializer(settings):
    if len(settings.session_secret) < 32 or not settings.operator_password_hash:
        raise HTTPException(503, "Configure las credenciales del operador y SESSION_SECRET")
    return URLSafeTimedSerializer(settings.session_secret, salt="aetherrag-v2-session")


def session(request: Request, *, mutate: bool = False) -> dict:
    settings = settings_for(request)
    if settings.environment == "test" and not settings.auth_enabled:
        return {"user": "test", "role": "admin", "csrf": "test"}
    if not settings.auth_enabled:
        if settings.environment == "production":
            raise HTTPException(503, "La autenticación es obligatoria en producción")
        cookie = request.cookies.get("aetherrag_session", "")
        if cookie and len(settings.session_secret) >= 32 and settings.operator_password_hash:
            try:
                value = serializer(settings).loads(cookie, max_age=settings.session_ttl_seconds)
                if (isinstance(value, dict)
                        and _same_secret(value.get("user"), settings.operator_username)
                        and value.get("role") == "admin"):
                    if mutate and (not value.get("csrf") or not _same_secret(
                            request.headers.get("X-CSRF-Token", ""), value.get("csrf"))):
                        raise HTTPException(403, "Token CSRF requerido")
                    return value
            except (BadSignature, SignatureExpired):
                pass
        return {"user": "development", "role": "admin", "csrf": ""}
    try:
        value = serializer(settings).loads(request.cookies.get("aetherrag_session", ""),
                                           max_age=settings.session_ttl_seconds)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Sesión requerida o vencida")
    if not isinstance(value, dict):
        raise HTTPException(401, "Sesión inválida")
    if not _same_secret(value.get("user"), settings.operator_username) or value.get("role") != "admin":
        raise HTTPException(403, "Sesión sin permiso")
    if mutate and (not value.get("csrf") or not _same_secret(
        request.headers.get("X-CSRF-Token", ""), value.get("csrf"))):
        raise HTTPException(403, "Token CSRF requerido")
    return value


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    settings = settings_for(request)
    signer = serializer(settings)
    peer = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _lock:
        # Bounded, per-process protection for the single-operator deployment.
        if len(_attempts) > 4096:
            _attempts.clear()
        _attempts[peer] = [t for t in _attempts[peer] if now - t < 60]
        if len(_attempts[peer]) >= 5:
            raise HTTPException(429, "Demasiados intentos; espere un minuto")
        _attempts[peer].append(now)
    try:
        valid = PasswordHasher().verify(settings.operator_password_hash, payload.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not _same_secret(payload.username, settings.operator_username) or not valid:
        raise HTTPException(401, "Credenciales inválidas")
    value = {"user": settings.operator_username, "role": "admin", "csrf": secrets.token_urlsafe(32)}
    response = JSONResponse({"user": value["user"], "csrf_token": value["csrf"]})
    response.set_cookie("aetherrag_session", signer.dumps(value), httponly=True,
                        secure=settings.session_secure, samesite="strict",
                        max_age=settings.session_ttl_seconds, path="/")
    return response


@router.get("/session")
def get_session(request: Request):
    value = session(request)
    return {"user": value["user"], "csrf_token": value["csrf"]}


@router.post("/logout")
def logout(request: Request):
    session(request, mutate=True)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("aetherrag_session", path="/")
    return response

