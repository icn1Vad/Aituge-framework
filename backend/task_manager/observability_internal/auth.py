from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from db.db_context import create_db_session
from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from .capabilities import canonical_hash
from .errors import InternalObservabilityError
from .event_registry import contains_sensitive_material
from .models import ScopeJtiClaimEntity, utc_now

ClientIdentityVerifier = Callable[[Request], str | None | Awaitable[str | None]]
ScopeClaimSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ScopeClaimClock = Callable[[], datetime]

TOKEN_MIN_LENGTH = 20
TOKEN_MAX_LENGTH = 4096
MAX_SCOPE_LIST_ITEMS = 500
CROSS_TENANT_PERMISSION = "monitor:observability:cross-tenant"
SYSTEM_SECURITY_PERMISSION = "monitor:observability:security:system"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


def normalize_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized != value or not REQUEST_ID_PATTERN.fullmatch(normalized):
        return None
    if contains_sensitive_material(normalized):
        return None
    return normalized


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def canonical_query_hash(request: Request) -> str:
    pairs = sorted(
        (str(key), str(value)) for key, value in request.query_params.multi_items()
    )
    return canonical_hash({"query": pairs})


def _audience_matches(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, list):
        return (
            bool(value)
            and all(isinstance(item, str) for item in value)
            and expected in value
        )
    return False


@dataclass(frozen=True, slots=True)
class InternalAuthContext:
    request_id: str
    environment: str
    service_subject: str
    actor_type: str
    actor_id: str | None
    tenant_scope: str
    current_tenant_id: str
    tenant_ids: frozenset[str]
    authorized_tenant_ids: frozenset[str]
    permissions: frozenset[str]
    audit_action_id: str | None
    parent_audit_event_id: str | None
    access_session_id: str | None
    access_reason_code: str | None
    cross_tenant: bool
    scope_jti: str
    max_stream_seconds: int | None = None

    @property
    def scope_fingerprint(self) -> str:
        return canonical_hash(
            {
                "environment": self.environment,
                "actorType": self.actor_type,
                "actorId": self.actor_id,
                "tenantScope": self.tenant_scope,
                "currentTenantId": self.current_tenant_id,
                "tenantIds": sorted(self.tenant_ids),
                "authorizedTenantIds": sorted(self.authorized_tenant_ids),
                "permissions": sorted(self.permissions),
                "auditActionId": self.audit_action_id,
                "parentAuditEventId": self.parent_audit_event_id,
                "accessSessionId": self.access_session_id,
                "accessReasonCode": self.access_reason_code,
            }
        )

    @property
    def can_view_system(self) -> bool:
        return SYSTEM_SECURITY_PERMISSION in self.permissions

    def permits_tenant(self, tenant_id: str | None) -> bool:
        if tenant_id is None:
            return self.can_view_system
        return self.tenant_scope == "ALL" or tenant_id in self.tenant_ids


class ScopeJtiClaimRepository:
    """Atomically claim short-lived Scope JWTs without persisting raw identifiers."""

    def __init__(
        self,
        session_factory: ScopeClaimSessionFactory = create_db_session,
        *,
        clock: ScopeClaimClock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def claim(
        self,
        *,
        service_subject: str,
        jti: str,
        accepted_until_epoch: int,
        request_id: str,
    ) -> None:
        now = self._clock()
        expires_at = datetime.fromtimestamp(accepted_until_epoch, UTC).replace(
            tzinfo=None
        )
        # A Scope JWT jti is globally one-time. Including the service subject in
        # the primary-key digest would allow replay after an identity swap.
        claim_hash = hashlib.sha256(jti.encode("utf-8")).hexdigest()
        service_subject_hash = hashlib.sha256(
            service_subject.encode("utf-8")
        ).hexdigest()
        row = ScopeJtiClaimEntity(
            claim_hash=claim_hash,
            service_subject_hash=service_subject_hash,
            expires_at=expires_at,
            claimed_at=now,
        )
        try:
            async with self._session_factory() as session:
                await session.execute(
                    delete(ScopeJtiClaimEntity).where(
                        ScopeJtiClaimEntity.claim_hash == claim_hash,
                        ScopeJtiClaimEntity.expires_at <= now,
                    )
                )
                session.add(row)
                await session.commit()
        except IntegrityError:
            raise InternalObservabilityError(
                401,
                "INTERNAL_SCOPE_TOKEN_REPLAYED",
                request_id,
            ) from None
        except DBAPIError:
            raise InternalObservabilityError(
                503,
                "INTERNAL_SCOPE_REPLAY_GUARD_UNAVAILABLE",
                request_id,
            ) from None

    async def cleanup_expired(
        self,
        *,
        limit: int = 1000,
        now: datetime | None = None,
    ) -> int:
        """Bounded maintenance cleanup; authentication only touches its own digest."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("scope claim cleanup limit must be positive")
        cutoff = now or self._clock()
        async with self._session_factory() as session:
            claim_hashes = list(
                (
                    await session.exec(
                        select(ScopeJtiClaimEntity.claim_hash)
                        .where(ScopeJtiClaimEntity.expires_at <= cutoff)
                        .order_by(
                            ScopeJtiClaimEntity.expires_at,
                            ScopeJtiClaimEntity.claim_hash,
                        )
                        .limit(limit)
                    )
                ).all()
            )
            if not claim_hashes:
                return 0
            result = await session.execute(
                delete(ScopeJtiClaimEntity).where(
                    ScopeJtiClaimEntity.claim_hash.in_(tuple(claim_hashes))
                )
            )
            deleted = int(result.rowcount or 0)
            await session.commit()
            return deleted


class HmacInternalAuthenticator:
    """Verify trusted mTLS identity plus short-lived Service/Scope HS256 JWTs."""

    def __init__(
        self,
        *,
        service_secret: bytes,
        scope_secret: bytes,
        service_issuer: str,
        service_audience: str,
        scope_issuer: str,
        scope_audience: str,
        environment: str,
        client_identity_verifier: ClientIdentityVerifier,
        scope_claim_repository: ScopeJtiClaimRepository | None = None,
        clock_skew_seconds: int = 5,
        service_max_ttl_seconds: int = 300,
        scope_max_ttl_seconds: int = 120,
    ) -> None:
        if len(service_secret) < 32 or len(scope_secret) < 32:
            raise ValueError("Internal JWT secrets must contain at least 32 bytes.")
        self._service_secret = bytes(service_secret)
        self._scope_secret = bytes(scope_secret)
        self._service_issuer = service_issuer
        self._service_audience = service_audience
        self._scope_issuer = scope_issuer
        self._scope_audience = scope_audience
        self._environment = _required_string(environment, maximum=32)
        self._verify_client_identity = client_identity_verifier
        self._scope_claims = scope_claim_repository or ScopeJtiClaimRepository()
        self._clock_skew = max(0, int(clock_skew_seconds))
        self._service_max_ttl = max(30, int(service_max_ttl_seconds))
        self._scope_max_ttl = max(15, int(scope_max_ttl_seconds))

    async def authenticate(self, request: Request) -> InternalAuthContext:
        raw_request_id = request.headers.get("X-Request-ID")
        request_id = normalize_request_id(raw_request_id)
        if request_id is None:
            missing = not isinstance(raw_request_id, str) or not raw_request_id.strip()
            raise InternalObservabilityError(
                400,
                "OBSERVABILITY_REQUEST_ID_REQUIRED"
                if missing
                else "OBSERVABILITY_REQUEST_ID_INVALID",
                "req_missing" if missing else "req_invalid",
            )

        client_identity = self._verify_client_identity(request)
        if inspect.isawaitable(client_identity):
            client_identity = await client_identity
        try:
            client_identity = _required_string(client_identity, maximum=120)
        except (TypeError, ValueError):
            raise InternalObservabilityError(
                401, "INTERNAL_MTLS_REQUIRED", request_id
            ) from None

        authorization = str(request.headers.get("Authorization") or "")
        if not authorization.startswith("Bearer "):
            raise InternalObservabilityError(
                401, "INTERNAL_SERVICE_TOKEN_INVALID", request_id
            )
        service_claims = self._verify_jwt(
            authorization[7:],
            secret=self._service_secret,
            issuer=self._service_issuer,
            audience=self._service_audience,
            request_id=request_id,
            error_code="INTERNAL_SERVICE_TOKEN_INVALID",
            max_ttl_seconds=self._service_max_ttl,
        )
        try:
            service_subject = _required_string(service_claims.get("sub"), maximum=120)
        except (TypeError, ValueError):
            raise InternalObservabilityError(
                401, "INTERNAL_SERVICE_TOKEN_INVALID", request_id
            ) from None
        if client_identity != service_subject:
            raise InternalObservabilityError(
                401, "INTERNAL_MTLS_IDENTITY_MISMATCH", request_id
            )

        raw_scope = str(request.headers.get("X-Observability-Scope") or "")
        if not TOKEN_MIN_LENGTH <= len(raw_scope) <= TOKEN_MAX_LENGTH:
            raise InternalObservabilityError(
                401, "INTERNAL_SCOPE_TOKEN_INVALID", request_id
            )
        scope_claims = self._verify_jwt(
            raw_scope,
            secret=self._scope_secret,
            issuer=self._scope_issuer,
            audience=self._scope_audience,
            request_id=request_id,
            error_code="INTERNAL_SCOPE_TOKEN_INVALID",
            max_ttl_seconds=self._scope_max_ttl,
        )
        self._validate_scope_binding(
            scope_claims,
            request,
            request_id,
            service_subject=service_subject,
        )
        context = self._context_from_scope(
            scope_claims, request, request_id, service_subject
        )
        await self._scope_claims.claim(
            service_subject=service_subject,
            jti=context.scope_jti,
            accepted_until_epoch=(
                _integer_claim(scope_claims, "exp") + self._clock_skew
            ),
            request_id=request_id,
        )
        return context

    def _context_from_scope(
        self,
        claims: Mapping[str, object],
        request: Request,
        request_id: str,
        service_subject: str,
    ) -> InternalAuthContext:
        try:
            tenant_scope = _required_string(
                claims.get("tenantScope"), maximum=16
            ).upper()
            if tenant_scope not in {"CURRENT", "SELECTED", "ALL"}:
                raise ValueError
            tenant_ids = frozenset(
                _strict_string_list(claims.get("tenantIds"), maximum=64)
            )
            selected_tenant_ids = frozenset(
                _strict_string_list(claims.get("selectedTenantIds", []), maximum=64)
            )
            authorized_tenant_ids = frozenset(
                _strict_string_list(claims.get("authorizedTenantIds", []), maximum=64)
            )
            current_tenant_id = _required_string(
                claims.get("currentTenantId"), maximum=64
            )
            explicit_cross_tenant = claims.get("crossTenant")
            if not isinstance(explicit_cross_tenant, bool):
                raise TypeError
            permissions = frozenset(
                _strict_string_list(claims.get("permissions"), maximum=120)
            )

            if tenant_scope == "CURRENT":
                valid_scope = (
                    explicit_cross_tenant is False
                    and tenant_ids == {current_tenant_id}
                    and not selected_tenant_ids
                    and (
                        not authorized_tenant_ids
                        or current_tenant_id in authorized_tenant_ids
                    )
                )
            elif tenant_scope == "SELECTED":
                valid_scope = (
                    explicit_cross_tenant is True
                    and bool(selected_tenant_ids)
                    and tenant_ids == selected_tenant_ids
                    and bool(authorized_tenant_ids)
                    and selected_tenant_ids.issubset(authorized_tenant_ids)
                    and CROSS_TENANT_PERMISSION in permissions
                )
            else:
                valid_scope = (
                    explicit_cross_tenant is True
                    and not tenant_ids
                    and not selected_tenant_ids
                    and CROSS_TENANT_PERMISSION in permissions
                )
            if not valid_scope:
                raise ValueError

            actor = claims.get("actor")
            if not isinstance(actor, Mapping):
                raise TypeError
            actor_type = _required_string(actor.get("type"), maximum=16).upper()
            if actor_type not in {
                "USER",
                "SERVICE",
                "SYSTEM",
                "ANONYMOUS",
                "UNKNOWN",
            }:
                raise ValueError
            actor_id = _optional_string(actor.get("id"), maximum=120)
            audit_action_id = _optional_string(claims.get("auditActionId"), maximum=96)
            parent_audit_event_id = _optional_string(
                claims.get("parentAuditEventId"), maximum=96
            )
            access_session_id = _optional_string(
                claims.get("accessSessionId"), maximum=96
            )
            access_reason_code = _optional_string(
                claims.get("accessReasonCode"), maximum=80
            )
            if explicit_cross_tenant and (
                not audit_action_id
                or not parent_audit_event_id
                or not access_reason_code
            ):
                raise InternalObservabilityError(
                    403, "INTERNAL_AUDIT_CONTEXT_REQUIRED", request_id
                )

            max_stream_seconds: int | None = None
            if request.url.path.endswith("/events/stream"):
                max_stream_seconds = _integer_claim(claims, "maxStreamSeconds")
                if max_stream_seconds < 1 or max_stream_seconds > 1800:
                    raise ValueError

            return InternalAuthContext(
                request_id=request_id,
                environment=self._environment,
                service_subject=service_subject,
                actor_type=actor_type,
                actor_id=actor_id,
                tenant_scope=tenant_scope,
                current_tenant_id=current_tenant_id,
                tenant_ids=tenant_ids,
                authorized_tenant_ids=authorized_tenant_ids,
                permissions=permissions,
                audit_action_id=audit_action_id,
                parent_audit_event_id=parent_audit_event_id,
                access_session_id=access_session_id,
                access_reason_code=access_reason_code,
                cross_tenant=explicit_cross_tenant,
                scope_jti=_required_string(claims.get("jti"), maximum=128),
                max_stream_seconds=max_stream_seconds,
            )
        except InternalObservabilityError:
            raise
        except (TypeError, ValueError):
            raise InternalObservabilityError(
                403, "INTERNAL_SCOPE_FORBIDDEN", request_id
            ) from None

    def _validate_scope_binding(
        self,
        claims: Mapping[str, object],
        request: Request,
        request_id: str,
        *,
        service_subject: str,
    ) -> None:
        expected = {
            "method": request.method.upper(),
            "path": request.url.path,
            "queryHash": canonical_query_hash(request),
            "requestId": request_id,
            "environment": self._environment,
        }
        try:
            for key, value in expected.items():
                if _required_string(claims.get(key), maximum=256) != value:
                    raise ValueError
            claimed_service_subject = _required_string(
                claims.get("serviceSubject"), maximum=120
            )
            if not hmac.compare_digest(
                claimed_service_subject.encode("utf-8"),
                service_subject.encode("utf-8"),
            ):
                raise ValueError
            if request.url.path.endswith("/events/stream"):
                expected_run_id = _required_string(
                    request.path_params.get("runId"), maximum=80
                )
                if _required_string(claims.get("runId"), maximum=80) != expected_run_id:
                    raise ValueError
            _required_string(claims.get("jti"), maximum=128)
        except (TypeError, ValueError):
            raise InternalObservabilityError(
                403, "INTERNAL_SCOPE_BINDING_INVALID", request_id
            ) from None

    def _verify_jwt(
        self,
        raw: str,
        *,
        secret: bytes,
        issuer: str,
        audience: str,
        request_id: str,
        error_code: str,
        max_ttl_seconds: int,
    ) -> Mapping[str, object]:
        try:
            if not TOKEN_MIN_LENGTH <= len(
                raw
            ) <= TOKEN_MAX_LENGTH or _has_control_character(raw):
                raise ValueError
            header_segment, payload_segment, signature_segment = raw.split(".", 2)
            if len(header_segment) > 1024 or len(payload_segment) > 3072:
                raise ValueError
            header = json.loads(_decode_segment(header_segment))
            if not isinstance(header, Mapping):
                raise TypeError
            if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
                raise ValueError
            signed = f"{header_segment}.{payload_segment}".encode("ascii")
            expected_signature = hmac.new(secret, signed, hashlib.sha256).digest()
            if not hmac.compare_digest(
                expected_signature, _decode_segment(signature_segment)
            ):
                raise ValueError
            claims = json.loads(_decode_segment(payload_segment))
            if not isinstance(claims, Mapping):
                raise TypeError
            now = int(time.time())
            if claims.get("iss") != issuer:
                raise ValueError
            if not _audience_matches(claims.get("aud"), audience):
                raise ValueError
            issued_at = _integer_claim(claims, "iat")
            expires_at = _integer_claim(claims, "exp")
            not_before = _integer_claim(claims, "nbf", default=issued_at)
            if expires_at <= now - self._clock_skew:
                raise ValueError
            if issued_at > now + self._clock_skew:
                raise ValueError
            if not_before > now + self._clock_skew:
                raise ValueError
            if not_before < issued_at - self._clock_skew:
                raise ValueError
            if expires_at <= issued_at or expires_at - issued_at > max_ttl_seconds:
                raise ValueError
            _required_string(claims.get("jti"), maximum=128)
            return claims
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            raise InternalObservabilityError(401, error_code, request_id) from None


def _strict_string_list(value: object, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_SCOPE_LIST_ITEMS:
        raise ValueError("claim must be a bounded string list")
    result = tuple(_required_string(item, maximum=maximum) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("claim list contains duplicates")
    return result


def _optional_string(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_string(value, maximum=maximum)


def _required_string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError("claim must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or _has_control_character(normalized)
    ):
        raise ValueError("claim string is invalid")
    return normalized


def _integer_claim(
    claims: Mapping[str, object],
    key: str,
    *,
    default: int | None = None,
) -> int:
    value = claims.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("JWT numeric date must be an integer")
    return value


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
