"""
OIDC/JWT auth for VMS Intelligence FastAPI services.

AUTH_MODE=keycloak  — verify RS256 access tokens (default in compose).
AUTH_MODE=off       — lab escape hatch; request is treated as local admin.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterable

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

log = logging.getLogger("vms-auth")

AUTH_MODE = os.getenv("AUTH_MODE", "off").strip().lower()
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "http://localhost:8080/realms/vms").rstrip("/")
OIDC_JWKS_URL = os.getenv(
    "OIDC_JWKS_URL",
    "http://keycloak:8080/realms/vms/protocol/openid-connect/certs",
)
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "vms-dashboard")
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
    if o.strip()
]

# Highest matching realm role wins.
PERMS = {
    "cameras:read":  {"admin", "operator", "investigator", "viewer"},
    "cameras:write": {"admin"},
    "cameras:test":  {"admin", "operator"},
    "alerts:read":   {"admin", "operator", "investigator", "viewer"},
    "alerts:act":    {"admin", "operator", "investigator"},
    "rules:read":    {"admin", "investigator"},
    "rules:write":   {"admin"},
    "draft:write":   {"admin", "investigator"},
    "admin:read":    {"admin"},
    "faces:view":    {"admin", "investigator"},
    "evidence:read": {"admin", "operator", "investigator"},
}

PUBLIC_EXACT = {
    "/health", "/docs", "/openapi.json", "/redoc", "/metrics",
}
PUBLIC_PREFIX = ("/docs",)

_jwks: PyJWKClient | None = None


@dataclass
class Principal:
    sub: str
    username: str
    name: str
    role: str
    tenant_id: str | None = None
    site_ids: list[str] = field(default_factory=list)
    officer_id: str | None = None
    raw: dict = field(default_factory=dict)

    def can(self, perm: str) -> bool:
        if self.role == "admin":
            return True
        return self.role in PERMS.get(perm, set())


def cors_kwargs() -> dict:
    origins = CORS_ORIGINS or ["http://localhost"]
    return {
        "allow_origins": origins,
        "allow_methods": ["*"],
        "allow_headers": ["Authorization", "Content-Type", "X-Access-Token"],
        "allow_credentials": False,
    }


def _jwks_client() -> PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(OIDC_JWKS_URL, cache_jwk_set=True, lifespan=300)
    return _jwks


def roles_from_claims(claims: dict) -> list[str]:
    roles: list[str] = []
    realm = claims.get("realm_access") or {}
    roles.extend(realm.get("roles") or [])
    res = claims.get("resource_access") or {}
    for client in (OIDC_CLIENT_ID, "account"):
        roles.extend((res.get(client) or {}).get("roles") or [])
    if isinstance(claims.get("roles"), list):
        roles.extend(claims["roles"])
    return [str(r).lower() for r in roles]


def canonical_role(claims: dict) -> str:
    mapped: list[tuple[str, int]] = []
    for r in roles_from_claims(claims):
        if r in ("admin", "igp", "dig", "station_commander"):
            mapped.append(("admin", 50))
        elif r in ("investigator", "supervisor"):
            mapped.append(("investigator", 40))
        elif r == "operator":
            mapped.append(("operator", 20))
        elif r == "viewer":
            mapped.append(("viewer", 10))
    if not mapped:
        return "viewer"
    return max(mapped, key=lambda item: item[1])[0]


def _allowed_issuers() -> list[str]:
    base = OIDC_ISSUER.rstrip("/")
    alts = {base}
    alts.add(base.replace("://localhost", "://127.0.0.1"))
    alts.add(base.replace("://127.0.0.1", "://localhost"))
    return list(alts)


def verify_token(token: str) -> Principal:
    if not token:
        raise HTTPException(401, "Missing access token")
    if AUTH_MODE in ("off", "disabled", "false", "0"):
        return Principal(sub="local", username="local", name="Local operator", role="admin")
    try:
        key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=_allowed_issuers(),
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(401, "Token expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise HTTPException(401, "Invalid token issuer") from exc
    except Exception as exc:
        log.warning("jwt_verify_failed: %s", exc)
        raise HTTPException(401, "Invalid access token") from exc
    azp = claims.get("azp") or claims.get("client_id")
    if azp and azp != OIDC_CLIENT_ID:
        log.debug("jwt_azp=%s client=%s", azp, OIDC_CLIENT_ID)
    tenant = claims.get("tenant_id") or claims.get("tid")
    return Principal(
        sub=str(claims.get("sub") or ""),
        username=str(claims.get("preferred_username") or claims.get("email") or "user"),
        name=str(claims.get("name") or claims.get("preferred_username") or "User"),
        role=canonical_role(claims),
        tenant_id=str(tenant) if tenant else None,
        raw=claims,
    )


def extract_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    header = request.headers.get("x-access-token") or ""
    if header:
        return header.strip()
    return (request.query_params.get("access_token") or "").strip()


def is_public(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if path in PUBLIC_EXACT or path == "/":
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIX)


def require_perm(principal: Principal, perm: str) -> None:
    if not principal.can(perm):
        raise HTTPException(403, f"Forbidden: {perm}")


def principal_of(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "Not authenticated")
    return p


async def load_site_ids(pool, sub: str) -> list[str]:
    if not pool or not sub or sub == "local":
        return []
    try:
        rows = await pool.fetch(
            "SELECT site_id::text AS site_id FROM user_site_access WHERE keycloak_sub=$1",
            sub,
        )
        return [r["site_id"] for r in rows]
    except Exception as exc:
        log.debug("user_site_access_skip: %s", exc)
        return []


async def allowed_camera_ids(pool, principal: Principal) -> list[str] | None:
    """None = unrestricted (single-tenant default). Empty list = no cameras."""
    if principal.role == "admin":
        return None
    sites = await load_site_ids(pool, principal.sub)
    principal.site_ids = sites
    if not sites:
        return None
    try:
        rows = await pool.fetch(
            "SELECT camera_id FROM cameras WHERE site_id = ANY($1::uuid[]) AND active=TRUE",
            sites,
        )
        return [r["camera_id"] for r in rows]
    except Exception as exc:
        log.warning("camera_acl_failed: %s", exc)
        return None


def redact_alert(payload: dict, principal: Principal) -> dict:
    out = dict(payload)
    if principal.can("faces:view"):
        return out
    meta = dict(out.get("metadata") or {})
    for key in ("nid", "matched_name", "name", "embedding"):
        meta.pop(key, None)
    out["metadata"] = meta
    if out.get("alert_type") == "face_match" or not principal.can("evidence:read"):
        out["snapshot_b64"] = ""
        out["has_snapshot"] = False
    return out


async def upsert_officer(pool, principal: Principal) -> str | None:
    if not pool or principal.sub in ("", "local"):
        return principal.officer_id
    badge = ("kc-" + principal.sub.replace("-", ""))[:20]
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO officers (badge_no, full_name, role, keycloak_id, tenant_id, active)
            VALUES ($1, $2, $3, $4, $5::uuid, TRUE)
            ON CONFLICT (keycloak_id) DO UPDATE
              SET full_name = EXCLUDED.full_name,
                  role = EXCLUDED.role,
                  tenant_id = COALESCE(EXCLUDED.tenant_id, officers.tenant_id)
            RETURNING id
            """,
            badge,
            (principal.name or principal.username)[:100],
            principal.role if principal.role in (
                "operator", "supervisor", "station_commander", "dig", "igp",
                "admin", "investigator", "viewer",
            ) else "operator",
            principal.sub[:100],
            principal.tenant_id,
        )
        oid = str(row["id"])
        principal.officer_id = oid
        return oid
    except Exception:
        try:
            row = await pool.fetchrow(
                "SELECT id FROM officers WHERE keycloak_id=$1", principal.sub[:100]
            )
            if row:
                principal.officer_id = str(row["id"])
                return principal.officer_id
        except Exception as exc:
            log.warning("officer_upsert_failed: %s", exc)
        return None


def install_auth(app: FastAPI, extra_public: Iterable[str] = ()) -> None:
    extra = set(extra_public)

    @app.middleware("http")
    async def _auth_mw(request: Request, call_next):
        path = request.url.path
        if is_public(path, request.method) or path in extra:
            if AUTH_MODE in ("off", "disabled", "false", "0"):
                request.state.principal = Principal(
                    sub="local", username="local", name="Local operator", role="admin"
                )
            return await call_next(request)
        if AUTH_MODE in ("off", "disabled", "false", "0"):
            request.state.principal = Principal(
                sub="local", username="local", name="Local operator", role="admin"
            )
            return await call_next(request)
        try:
            request.state.principal = verify_token(extract_token(request))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)


_AUTH_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sites (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID,
        name VARCHAR(120) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS user_site_access (
        keycloak_sub VARCHAR(100) NOT NULL,
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        PRIMARY KEY (keycloak_sub, site_id)
    )""",
    "ALTER TABLE officers ADD COLUMN IF NOT EXISTS tenant_id UUID",
    "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS tenant_id UUID",
    "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS site_id UUID",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS tenant_id UUID",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id UUID",
    """DO $$
    DECLARE r RECORD;
    BEGIN
      FOR r IN
        SELECT c.conname FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'officers' AND n.nspname = 'public'
          AND c.contype = 'c' AND pg_get_constraintdef(c.oid) ILIKE '%role%'
      LOOP
        EXECUTE format('ALTER TABLE officers DROP CONSTRAINT %I', r.conname);
      END LOOP;
    END $$""",
    """ALTER TABLE officers ADD CONSTRAINT officers_role_check
       CHECK (role IN (
         'operator','supervisor','station_commander','dig','igp','admin',
         'investigator','viewer'
       ))""",
]


async def ensure_auth_schema(pool) -> None:
    for sql in _AUTH_SCHEMA:
        try:
            await pool.execute(sql)
        except Exception as exc:
            log.debug("auth_schema_stmt: %s", exc)

