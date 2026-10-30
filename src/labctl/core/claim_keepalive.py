"""Keep a caller's claim alive during one long operation (issue #2).

A claim's deadline slides only when a call that touches the board starts
(:meth:`ResourceManager.heartbeat_claim`), so a single long call
(``boot_test``, a long ``serial_capture``, flashing a card) could outlast
the claim, and another agent could then claim the board and power-cycle
it mid-run. :class:`ClaimKeepalive` heartbeats the claim the caller
*already holds* from a background thread while the operation runs. It
never creates a claim.

Between steps of a multi-step operation (each boot-test run, the reboot
after a write), :meth:`ClaimKeepalive.checkpoint` says whether it may go
on: not when the caller's claim was lost meanwhile (released,
force-released or expired), nor when someone else has claimed the board
(or a board sharing its hardware) since the operation started.

The thread only reads and updates the claims table; each database call
opens its own SQLite connection, so it is safe beside the caller's
thread (and beside the MCP server's worker threads).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

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


class ClaimKeepalive:
    """Heartbeat ``actor``'s claim on ``sbc_name`` while an operation runs.

    Use as a context manager around the operation. With ``enabled=False``
    (claims are off) it does nothing. When the actor holds no claim at
    the start, nothing is heartbeated; :meth:`checkpoint` still refuses
    once someone else claims the board (``recheck``), or when
    ``require_claim`` is set.

    ``recheck=False`` is for a caller that overrode someone else's claim
    to start (CLI ``--override-claim``): only the loss of the caller's
    own claim stops it then.
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
        interval: Optional[float] = None,
    ):
        self.manager = manager
        self.sbc_name = sbc_name
        self.actor = actor
        self.enabled = enabled
        self.require_claim = require_claim
        self.recheck = recheck
        self._interval = interval
        #: The actor held the board's claim when the operation started.
        self.held = False
        #: Why the claim counts as lost, once it does.
        self.lost_reason: Optional[str] = None
        self.beats = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ClaimKeepalive":
        if not self.enabled:
            return self
        try:
            claim = self.manager.get_active_claim(self.sbc_name)
        except UnknownSBCError:
            return self
        if claim is None or not self.actor.holds(claim):
            return self
        self.held = True
        interval = self._interval
        if interval is None:
            interval = min(
                MAX_INTERVAL_SECONDS,
                max(MIN_INTERVAL_SECONDS, (claim.duration_seconds or 0) / 4),
            )
        self._thread = threading.Thread(
            target=self._run,
            args=(interval,),
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

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            if not self.beat():
                return

    # -- heartbeat and checks ----------------------------------------------

    def beat(self) -> bool:
        """Heartbeat the claim if the actor still holds it.

        Returns False once the claim is lost (and from then on); True
        when it was heartbeated, when the actor held none to begin with,
        and when the database couldn't be reached this time (the next
        beat tries again).
        """
        if not self.held:
            return True
        with self._lock:
            if self.lost_reason is not None:
                return False
            try:
                claim = self.manager.get_active_claim(self.sbc_name)
            except UnknownSBCError:
                self.lost_reason = f"SBC '{self.sbc_name}' no longer exists"
                return False
            except Exception:
                logger.debug("Claim keepalive read failed", exc_info=True)
                return True
            if claim is None or not self.actor.holds(claim):
                self.lost_reason = self._lost_text(claim)
                return False
            try:
                self.manager.touch_claim(claim)
                self.beats += 1
            except Exception:
                logger.debug("Claim keepalive heartbeat failed", exc_info=True)
            return True

    def _lost_text(self, claim) -> str:
        text = (
            f"the claim on '{self.sbc_name}' is no longer held by this caller "
            "(released, force-released or expired)"
        )
        if claim is not None:
            text += f"; it is now claimed by '{claim.agent_name or 'unknown'}'"
        return text

    def checkpoint(self) -> Optional[str]:
        """Whether the operation may go on to its next step.

        Returns None to go on, or the reason to stop: the actor's claim was
        lost, someone else has claimed the board or a board sharing its
        hardware (``recheck``), or ``require_claim`` is set and nobody
        holds it. The actor's own claim is heartbeated.
        """
        if not self.enabled:
            return None
        if self.held and not self.beat():
            return self.lost_reason
        if not self.recheck:
            return None
        try:
            self.manager.check_claim(
                self.sbc_name,
                self.actor,
                require_claim=self.require_claim,
                include_shared=True,
                heartbeat=False,
            )
        except UnknownSBCError:
            return f"SBC '{self.sbc_name}' no longer exists"
        except ClaimConflict as e:
            board = e.claim.sbc_name or self.sbc_name
            return f"'{board}' is now claimed by '{e.claim.agent_name or 'unknown'}'"
        except ClaimRequiredError as e:
            return str(e)
        return None
