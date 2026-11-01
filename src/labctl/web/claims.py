"""
Claim checks for the web UI and REST API (D013).

A change to a board that another user has claimed is refused: REST with
409 ``sbc_claimed``, the web UI with a page naming the holder. Operators
may override explicitly (``"override_claim": true`` in a REST body, or the
button on that page); the override is recorded in the activity log.
"""

from typing import Optional

from flask import g, render_template, request, session, url_for

from labctl.core.models import (
    ClaimActor,
    ClaimConflict,
    ClaimRequiredError,
    NotOperatorError,
    UnknownSBCError,
)

# A refusal: the JSON error body and its HTTP status.
Refusal = tuple[dict, int]

_TRUE_VALUES = ("1", "true", "yes", "on")


def port_number(value) -> Optional[int]:
    """``value`` (an int, or text of decimal digits) when it is a whole
    number from 1 to 65535, else None. Text is length-checked before
    ``int()``, which refuses very long digit strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdecimal() or len(value) > 5:
            return None
        value = int(value)
    if isinstance(value, int) and 1 <= value <= 65535:
        return value
    return None


def is_api_request() -> bool:
    return bool(request.endpoint and request.endpoint.startswith("api."))


def web_actor() -> ClaimActor:
    """Who is acting, for claim checks.

    With auth, the logged-in or API-key user: their claims from any
    session (REST, the web UI, MCP over HTTP) count as theirs, and their
    role decides whether they may override. Without auth there is one
    shared identity, which is an operator (as unauthenticated MCP is).
    """
    if not g.config.auth.enabled:
        return ClaimActor(session_id="web:anonymous", operator=True)
    user = getattr(g, "auth_user", None)
    if user is not None:
        return ClaimActor(
            session_id=f"web:{user.username}",
            owner=user.username,
            operator=user.is_operator,
        )
    # A login whose user has since left the config: theirs, but no
    # operator rights.
    name = session.get("user") or "unknown"
    return ClaimActor(session_id=f"web:{name}", owner=name, operator=False)


def web_user_name() -> str:
    """The name recorded for this caller's claims, releases and requests."""
    actor = web_actor()
    return actor.owner or "web-operator"


def override_requested() -> bool:
    """``override_claim`` in the JSON body (must be ``true``), the form or
    the query string (``1``, ``true``, ``yes`` or ``on``)."""
    data = request.get_json(silent=True) if request.is_json else None
    if isinstance(data, dict) and "override_claim" in data:
        return data["override_claim"] is True
    value = request.values.get("override_claim", "")
    return str(value).strip().lower() in _TRUE_VALUES


def claimed_error(sbc_name: str, claim, *, override_hint: bool = True) -> dict:
    """``sbc_claimed`` for a change to ``sbc_name``; ``claim`` may be on
    another board that shares its hardware. Every caller refuses the
    request with it, so the refusal is recorded here (#60)."""
    record_refusal(sbc_name, "claimed", claim)
    holder_board = claim.sbc_name or sbc_name
    holder = claim.agent_name or "unknown"
    shared = (
        f" (it shares hardware with '{sbc_name}')" if holder_board != sbc_name else ""
    )
    if claim.is_active:
        until = f"until {claim.expires_at.isoformat()}" if claim.expires_at else ""
        hints = [
            "Wait for the claim to expire or be released",
            f"Ask for it: POST /api/claims/{holder_board}/request-release",
        ]
        if web_actor().operator:
            if override_hint:
                hints.append(
                    'Operator override: repeat with "override_claim": true '
                    "(recorded in the activity log)"
                )
            else:
                hints.append(
                    f"Operator override: POST /api/claims/{holder_board}"
                    "/force-release"
                )
    else:
        # Past its deadline, inside the grace period: only its holder may
        # re-claim it, and release requests only act on active claims.
        until = "(expired, in its grace period)"
        hints = ["Retry once the grace period ends"]
    return {
        "error": "sbc_claimed",
        "sbc_name": holder_board,
        "message": f"SBC '{holder_board}' is claimed by '{holder}' {until}{shared}",
        "claim": claim.to_dict(),
        "hints": hints,
    }


def record_refusal(sbc_name: str, reason: str, claim=None) -> None:
    """Record that this request was refused over a claim (#60)."""
    g.manager.record_claim_refusal(
        sbc_name, reason, claim=claim, attempted=f"{request.method} {request.path}"
    )


def _refusal(exc: Exception, sbc_name: str, for_sbc: Optional[str]) -> Refusal:
    if isinstance(exc, ClaimConflict):
        return claimed_error(for_sbc or sbc_name, exc.claim), 409
    if isinstance(exc, NotOperatorError):
        record_refusal(sbc_name, "not_operator")
        return (
            {
                "error": "not_operator",
                "sbc_name": sbc_name,
                "message": f"{exc}; your user has role 'agent'",
                "hints": [f"Ask for it: POST /api/claims/{sbc_name}/request-release"],
            },
            403,
        )
    assert isinstance(exc, ClaimRequiredError)
    record_refusal(sbc_name, "claim_required")
    return (
        {
            "error": "claim_required",
            "sbc_name": sbc_name,
            "message": str(exc),
            "hints": [f"Claim it first: POST /api/claims/{sbc_name}"],
        },
        409,
    )


def check(
    sbc_name: str,
    *,
    shared: bool = True,
    sharers: Optional[dict] = None,
) -> Optional[Refusal]:
    """Refusal for a change to ``sbc_name`` by this caller, or None to go
    ahead (the caller's own claim is heartbeated; an override is logged).

    ``shared`` also checks boards sharing ``sbc_name``'s hardware; pass
    False for changes to the board's own records. ``sharers`` is hardware
    about to be wired to ``sbc_name`` (keyword arguments for
    ResourceManager.sbcs_sharing_hardware): the boards that already have
    it are checked first, so a claimed board's plug, SDWire or console
    can't be driven through another board. ``claims.require_claim``
    applies to REST calls, not to the web UI. An override is what the
    request asks for (see override_requested).
    """
    config = g.config
    if not config.claims.enabled:
        return None
    override = override_requested()
    manager = g.manager
    actor = web_actor()
    if sharers:
        others = set(manager.sbcs_sharing_hardware(**sharers)) - {sbc_name}
        try:
            manager.check_claims_of(others, actor, override=override)
        except (ClaimConflict, NotOperatorError) as exc:
            return _refusal(exc, sbc_name, sbc_name)
    try:
        manager.check_claim(
            sbc_name,
            actor,
            override=override,
            require_claim=is_api_request() and config.claims.require_claim,
            include_shared=shared,
        )
    except UnknownSBCError:
        # Callers load the board first, so it was renamed or deleted
        # meanwhile: don't act on what they loaded unchecked.
        return (
            {
                "error": "sbc_changed",
                "sbc_name": sbc_name,
                "message": f"SBC '{sbc_name}' was renamed or removed; retry",
            },
            404,
        )
    except (ClaimConflict, NotOperatorError, ClaimRequiredError) as exc:
        return _refusal(exc, sbc_name, None)
    return None


def refusal_page(refusal: Refusal, sbc_name: str):
    """The web UI's answer to a refused change: the holder, how to ask for
    the board, and (for operators, on an active claim) a button that
    repeats the change with ``override_claim``."""
    body, status = refusal
    claim = body.get("claim") or {}
    fields = [
        (key, value)
        for key, value in request.form.items(multi=True)
        if key not in ("_csrf_token", "override_claim")
    ]
    can_override = (
        body["error"] == "sbc_claimed"
        and web_actor().operator
        and bool(claim.get("is_active", True))
    )
    return (
        render_template(
            "claim_refused.html",
            body=body,
            sbc_name=sbc_name,
            fields=fields,
            action=request.path,
            can_override=can_override,
            back=url_for("views.sbc_detail", name=sbc_name),
        ),
        status,
    )
