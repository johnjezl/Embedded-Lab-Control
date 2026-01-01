"""
WebSocket bridge for serial console in web interface.

Bridges between browser WebSocket and serial proxy TCP connection.
"""

import asyncio
import logging
from typing import Optional

from flask import g, request
from flask_socketio import SocketIO, emit, join_room, leave_room

logger = logging.getLogger(__name__)

# SocketIO instance - initialized in init_socketio()
socketio: Optional[SocketIO] = None

# Active console connections: {sid: {sbc_name, reader_task, writer}}
active_connections: dict = {}


def init_socketio(app, **kwargs):
    """Initialize SocketIO with Flask app."""
    global socketio
    socketio = SocketIO(app, **kwargs)
    register_handlers(socketio)
    return socketio


def register_handlers(sio: SocketIO):
    """Register SocketIO event handlers."""

    @sio.on("connect", namespace="/console")
    def handle_connect():
        """Handle new WebSocket connection."""
        logger.info(f"WebSocket client connected: {request.sid}")

    @sio.on("disconnect", namespace="/console")
    def handle_disconnect():
        """Handle WebSocket disconnection."""
        sid = request.sid
        logger.info(f"WebSocket client disconnected: {sid}")

        # Clean up any active console connection
        if sid in active_connections:
            conn = active_connections.pop(sid)
            if "reader_task" in conn and conn["reader_task"]:
                conn["reader_task"].cancel()
            if "writer" in conn and conn["writer"]:
                try:
                    conn["writer"].close()
                except Exception:
                    pass

    @sio.on("open_console", namespace="/console")
    def handle_open_console(data):
        """Open console connection to SBC.

        Expected data: {"sbc_name": "sbc1"}
        """
        sid = request.sid
        sbc_name = data.get("sbc_name")

        if not sbc_name:
            emit("error", {"message": "sbc_name required"})
            return

        # Get SBC info
        manager = g.manager
        sbc = manager.get_sbc_by_name(sbc_name)

        if not sbc:
            emit("error", {"message": f"SBC '{sbc_name}' not found"})
            return

        console_port = sbc.console_port
        if not console_port or not console_port.tcp_port:
            emit("error", {"message": "No console port configured"})
            return

        # Join room for this SBC
        join_room(sbc_name)

        # Store connection info
        active_connections[sid] = {
            "sbc_name": sbc_name,
            "tcp_port": console_port.tcp_port,
            "reader_task": None,
            "writer": None,
        }

        emit(
            "console_opened",
            {
                "sbc_name": sbc_name,
                "message": f"Connected to {sbc_name} console",
            },
        )

        # Start async reader in background
        # Note: This is simplified - in production, use a proper async bridge
        logger.info(f"Console opened for {sbc_name} by {sid}")

    @sio.on("console_input", namespace="/console")
    def handle_console_input(data):
        """Handle input from browser to send to console.

        Expected data: {"text": "ls -la\n"}
        """
        sid = request.sid

        if sid not in active_connections:
            emit("error", {"message": "Console not opened"})
            return

        conn = active_connections[sid]
        text = data.get("text", "")

        if not text:
            return

        # Send to console via proxy
        # Note: Actual implementation would write to TCP socket
        logger.debug(f"Console input from {sid}: {repr(text)}")

        # Echo back for now (in production, this comes from the serial device)
        emit("console_output", {"text": text}, room=conn["sbc_name"])

    @sio.on("close_console", namespace="/console")
    def handle_close_console():
        """Close console connection."""
        sid = request.sid

        if sid in active_connections:
            conn = active_connections.pop(sid)
            leave_room(conn["sbc_name"])
            emit("console_closed", {"message": "Console closed"})
            logger.info(f"Console closed for {conn['sbc_name']} by {sid}")


class ConsoleWebSocketBridge:
    """
    Bridge between WebSocket and TCP proxy.

    This class handles the async bridging between Flask-SocketIO
    and the asyncio-based serial proxy.
    """

    def __init__(self, sio: SocketIO, sid: str, sbc_name: str, proxy_port: int):
        self.sio = sio
        self.sid = sid
        self.sbc_name = sbc_name
        self.proxy_port = proxy_port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False

    async def connect(self) -> bool:
        """Connect to proxy TCP port."""
        try:
            self._reader, self._writer = await asyncio.open_connection(
                "localhost", self.proxy_port
            )
            self._running = True
            return True
        except Exception as e:
            logger.error(f"Failed to connect to proxy: {e}")
            return False

    async def disconnect(self):
        """Disconnect from proxy."""
        self._running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def send_to_console(self, data: str):
        """Send data to console."""
        if self._writer and self._running:
            self._writer.write(data.encode())
            await self._writer.drain()

    async def read_loop(self):
        """Read from console and emit to WebSocket."""
        while self._running and self._reader:
            try:
                data = await asyncio.wait_for(self._reader.read(4096), timeout=1.0)
                if not data:
                    break

                # Emit to WebSocket
                self.sio.emit(
                    "console_output",
                    {"text": data.decode("utf-8", errors="replace")},
                    namespace="/console",
                    room=self.sbc_name,
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Console read error: {e}")
                break

        self._running = False
