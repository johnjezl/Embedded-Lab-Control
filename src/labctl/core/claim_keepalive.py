"""Keep a caller's claim alive during one long operation (issue #2).

A claim's deadline slides only when a call that touches the board starts
(the call's claim check: :meth:`ResourceManager.check_claim`, which
heartbeats the caller's claim with :meth:`ResourceManager.touch_claim`),
so a single long call
(``boot_test``, a long ``serial_capture``, flashing a card) could outlast
the claim, and another agent could then claim the board and power-cycle
it mid-run. :class:`ClaimKeepalive` heartbeats the claim the caller
*already holds* from a background thread while the operation runs. It
never creates a claim.

Between steps of a multi-step operation (each boot-test run, the reboot
after a write), :meth:`ClaimKeepalive.checkpoint` says whether it may go
on: not when someone else has claimed the board (or a board sharing its
hardware) since the operation started, nor when the caller's claim was
lost meanwhile (released, force-released or expired).

The thread only reads and updates the claims table; each database call
opens its own SQLite connection, so it is safe beside the caller's
thread (and beside the MCP server's worker threads). A database error is
a missed heartbeat, not a lost claim, until the claim's deadline has
passed without a successful one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional

from labctl.core.models import (
    ClaimActor,
    ClaimConflict,
    ClaimRequiredError,
    UnknownSBCError,
)

logger = logging.getLogger(__name__)

#: Heartbeats come at a quarter of the claim's duration, but at least
#: this often and no more often than MIN_INTERVAL_SECONDS.
MAX_INTERVAL_SECONDS = 60.0
MIN_INTERVAL_SECONDS = 5.0

#: A caller holding no claim has no deadline to go by: when the claim
#: recheck has failed (database errors) for this long without a success,
#: the operation stops rather than run on unchecked.
UNCHECKED_LIMIT_SECONDS = 60.0

# StopReason.kind values.
LOST = "lost"  # the caller's own claim is gone
CLAIMED = "claimed"  # someone else holds a claim on the board or a sharer
REQUIRED = "required"  # claims.require_claim and nobody holds it
GONE = "gone"  # the board was deleted
UNCHECKED = "unchecked"  # claims couldn't be checked for too long


class StopReason(str):
    """Why an operation must stop (the text), with its ``kind``."""

    kind: str

    def __new__(cls, text: str, kind: str) -> "StopReason":
        obj = super().__new__(cls, text)
        obj.kind = kind
        return obj


class ClaimKeepalive:
    """Heartbeat ``actor``'s claim on ``sbc_name`` while an operation runs.

    Use as a context manager around the operation. With ``enabled=False``
    (claims are off) it does nothing. When the actor holds no claim at
    the start, nothing is heartbeated; :meth:`checkpoint` still stops the
    operation once someone else claims the board (``recheck``), or when
    ``require_claim`` is set.

    ``allowed_claim_ids`` are other users' claims the caller overrode to
    start (CLI ``--override-claim``): they don't stop it, but a claim
    taken by anyone after the start still does.
    """

    def __init__(
        self,
        manager,
        sbc_name: str,
        actor: ClaimActor,
        *,
        enabled: bool = True,
        require_claim: bool = False,
        recheck: bool = True,
        allowed_claim_ids: Iterable[int] = (),
        interval: Optional[float] = None,
    ):
        self.manager = manager
        self.sbc_name = sbc_name
        self.actor = actor
        self.enabled = enabled
        self.require_claim = require_claim
        self.recheck = recheck
        self.allowed_claim_ids = set(allowed_claim_ids)
        self._interval = interval
        #: The actor held the board's claim when the operation started.
        self.held = False
        #: That claim's id: a later claim (even by the same holder) is not it.
        self.claim_id: Optional[int] = None
        #: Why the claim counts as lost, once it does.
        self.lost_reason: Optional[StopReason] = None
        self.beats = 0
        self._duration = 0.0
        # Monotonic time by which the claim expires unless heartbeated.
        self._deadline = 0.0
        # Monotonic time of the last recheck that succeeded (or the start).
        self._last_checked = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ClaimKeepalive":
        if not self.enabled:
            return self
        self._last_checked = time.monotonic()
        try:
            claim = self.manager.get_open_claim(self.sbc_name)
        except UnknownSBCError:
            return self
        except Exception:
            # Never fail the call over this: it just runs unprotected.
            logger.warning(
                "Can't read the claim on %s; carrying on without keeping it " "alive",
                self.sbc_name,
                exc_info=True,
            )
            return self
        if claim is None or not claim.is_active or not self.actor.holds(claim):
            return self
        self.held = True
        self.claim_id = claim.id
        self._duration = float(claim.duration_seconds or 0)
        remaining = (claim.expires_at.timestamp() - time.time()) if claim else 0
        self._deadline = time.monotonic() + max(0.0, remaining)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"claim-keepalive-{self.sbc_name}",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def __enter__(self) -> "ClaimKeepalive":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def next_interval(self) -> float:
        """Seconds to the next heartbeat: a quarter of the claim's current
        duration (re-read, so a renew to a shorter one is followed),
        within MIN/MAX_INTERVAL_SECONDS."""
        if self._interval is not None:
            return self._interval
        try:
            claim = self.manager.get_open_claim(self.sbc_name)
            if claim is not None and claim.duration_seconds:
                self._duration = float(claim.duration_seconds)
        except Exception:
            logger.debug("Claim keepalive duration read failed", exc_info=True)
        return min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, self._duration / 4))

    def _run(self) -> None:
        while not self._stop.wait(self.next_interval()):
            if not self.beat():
                return

    # -- heartbeat and checks ----------------------------------------------

    def beat(self) -> bool:
        """Heartbeat the claim if the actor still holds it.

        Returns False once the claim is lost (and from then on); True
        when it was heartbeated, when the actor held none to begin with,
        and when the database couldn't be reached this time but the
        claim's deadline hasn't passed (the next beat tries again).
        """
        if not self.held:
            return True
        with self._lock:
            if self.lost_reason is not None:
                return False
            try:
                still_held = self.manager.heartbeat_held_claim(
                    self.sbc_name, self.actor, claim_id=self.claim_id
                )
            except UnknownSBCError:
                self.lost_reason = StopReason(
                    f"SBC '{self.sbc_name}' no longer exists", GONE
                )
                return False
            except Exception:
                logger.warning(
                    "Claim keepalive for %s couldn't renew the claim",
                    self.sbc_name,
                    exc_info=True,
                )
                if time.monotonic() >= self._deadline:
                    self.lost_reason = StopReason(
                        f"the claim on '{self.sbc_name}' couldn't be renewed "
                        "before it expired (database error)",
                        LOST,
                    )
                    return False
                return True
            if not still_held:
                self.lost_reason = StopReason(self._lost_text(), LOST)
                return False
            self.beats += 1
            self._deadline = time.monotonic() + self._duration
            return True

    def _lost_text(self) -> str:
        text = (
            f"the claim on '{self.sbc_name}' is no longer held by this caller "
            "(released, force-released or expired)"
        )
        try:
            claim = self.manager.get_active_claim(self.sbc_name)
        except Exception:
            claim = None
        if claim is not None:
            text += f"; it is now claimed by '{claim.agent_name or 'unknown'}'"
        return text

    def checkpoint(self) -> Optional[StopReason]:
        """Whether the operation may go on to its next step.

        Returns None to go on, or the reason to stop. First, as in
        ``check_claim``, nobody else may hold a claim on the board or a
        board sharing its hardware (``recheck``; ``require_claim`` too),
        and only then is the actor's own claim heartbeated: it must still
        be held (the same claim the operation started with). Never raises:
        a database error counts as a missed check until the claim's
        deadline has passed, or, for a caller holding no claim, until the
        checks have failed for UNCHECKED_LIMIT_SECONDS.
        """
        if not self.enabled:
            return None
        if self.recheck:
            try:
                reason = self._recheck()
                self._last_checked = time.monotonic()
            except Exception:
                logger.warning(
                    "Claim check for %s failed; going on",
                    self.sbc_name,
                    exc_info=True,
                )
                reason = None
                now = time.monotonic()
                if self.held and now >= self._deadline:
                    reason = StopReason(
                        f"the claim on '{self.sbc_name}' couldn't be checked "
                        "before it expired (database error)",
                        LOST,
                    )
                elif (
                    not self.held
                    and now - self._last_checked >= UNCHECKED_LIMIT_SECONDS
                ):
                    reason = StopReason(
                        f"claims on '{self.sbc_name}' couldn't be checked for "
                        f"{UNCHECKED_LIMIT_SECONDS:.0f} s (database error)",
                        UNCHECKED,
                    )
            if reason:
                return reason
        if self.held and not self.beat():
            return self.lost_reason
        return None

    def _recheck(self) -> Optional[StopReason]:
        """Someone else's claim on the board or a sharer (other than the
        overridden ones), or require_claim unmet; None if clear."""
        boards = [self.sbc_name]
        try:
            boards += list(self.manager.sbcs_sharing_hardware_with(self.sbc_name))
        except UnknownSBCError:
            return StopReason(f"SBC '{self.sbc_name}' no longer exists", GONE)
        for i, board in enumerate(boards):
            try:
                claim = self.manager.check_claim(
                    board,
                    self.actor,
                    require_claim=self.require_claim and i == 0,
                    include_shared=False,
                    heartbeat=False,
                )
                if (
                    i == 0
                    and self.held
                    and (claim is None or claim.id != self.claim_id)
                ):
                    # Released or expired, or replaced by a later claim (even
                    # one by the same holder): not the claim we started with.
                    return self._mark_lost()
            except UnknownSBCError:
                if i == 0:
                    return StopReason(f"SBC '{self.sbc_name}' no longer exists", GONE)
                continue
            except ClaimConflict as e:
                if e.claim.id in self.allowed_claim_ids:
                    continue
                if i == 0 and self.held:
                    return self._mark_lost()
                holder = e.claim.sbc_name or board
                return StopReason(
                    f"'{holder}' is now claimed by "
                    f"'{e.claim.agent_name or 'unknown'}'",
                    CLAIMED,
                )
            except ClaimRequiredError as e:
                if self.held:
                    return self._mark_lost()
                return StopReason(str(e), REQUIRED)
        return None

    def _mark_lost(self) -> StopReason:
        """The actor's own claim on the board is gone (found by the
        recheck): stop, and stop heartbeating."""
        with self._lock:
            if self.lost_reason is None:
                self.lost_reason = StopReason(self._lost_text(), LOST)
            return self.lost_reason
