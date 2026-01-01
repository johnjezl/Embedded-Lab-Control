"""
Multi-client serial proxy for lab controller.

Enables multiple clients to simultaneously access a serial console with
first-writer-wins write lock policy. Includes session logging.

Use cases:
- Multiple viewers watching console output (read-only)
- Single writer with multiple readers
- CLI and web clients viewing same console simultaneously
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProxyClient:
    """Represents a connected proxy client."""

    client_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connected_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    has_write_lock: bool = False

    @property
    def address(self) -> str:
        """Get client address as string."""
        try:
            peername = self.writer.get_extra_info("peername")
            if peername:
                return f"{peername[0]}:{peername[1]}"
        except Exception:
            pass
        return "unknown"


class SessionLogger:
    """Logs all serial traffic to timestamped files."""

    def __init__(self, log_dir: Path, session_name: str):
        self.log_dir = log_dir
        self.session_name = session_name
        self.log_file: Optional[Path] = None
        self._file_handle = None

    def start(self) -> Path:
        """Start logging session, returns log file path."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"{self.session_name}_{timestamp}.log"

        self._file_handle = open(self.log_file, "a", buffering=1)  # Line buffered
        self._write_header()
        return self.log_file

    def _write_header(self) -> None:
        """Write session header to log file."""
        if self._file_handle:
            self._file_handle.write(f"# Session: {self.session_name}\n")
            self._file_handle.write(f"# Started: {datetime.now().isoformat()}\n")
            self._file_handle.write("# Format: [timestamp] [direction] data\n")
            self._file_handle.write("# Direction: >> = from device, << = to device\n")
            self._file_handle.write("#" + "=" * 60 + "\n")

    def log_output(self, data: bytes) -> None:
        """Log data received from device (output)."""
        if self._file_handle:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            # Escape non-printable bytes for readability
            text = data.decode("utf-8", errors="replace")
            self._file_handle.write(f"[{timestamp}] >> {repr(text)}\n")

    def log_input(self, data: bytes, client_id: str) -> None:
        """Log data sent to device (input)."""
        if self._file_handle:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            text = data.decode("utf-8", errors="replace")
            self._file_handle.write(
                f"[{timestamp}] << [{client_id[:8]}] {repr(text)}\n"
            )

    def stop(self) -> None:
        """Stop logging session."""
        if self._file_handle:
            self._file_handle.write("#" + "=" * 60 + "\n")
            self._file_handle.write(f"# Ended: {datetime.now().isoformat()}\n")
            self._file_handle.close()
            self._file_handle = None


class SerialProxy:
    """
    Multi-client serial proxy with first-writer-wins policy.

    Connects to ser2net TCP port and allows multiple clients to:
    - Read all console output (broadcast to all)
    - Write to console (first client to write gets exclusive lock)
    """

    def __init__(
        self,
        name: str,
        ser2net_host: str,
        ser2net_port: int,
        proxy_port: int,
        log_dir: Optional[Path] = None,
        write_policy: str = "first",
        max_clients: int = 10,
    ):
        """
        Initialize serial proxy.

        Args:
            name: Name for this proxy session (e.g., SBC name)
            ser2net_host: Host where ser2net is running
            ser2net_port: TCP port of ser2net connection
            proxy_port: Port this proxy listens on
            log_dir: Directory for session logs (None to disable)
            write_policy: Write policy - "first", "all", or "queue"
            max_clients: Maximum concurrent clients
        """
        self.name = name
        self.ser2net_host = ser2net_host
        self.ser2net_port = ser2net_port
        self.proxy_port = proxy_port
        self.log_dir = log_dir
        self.write_policy = write_policy
        self.max_clients = max_clients

        self.clients: dict[str, ProxyClient] = {}
        self.writer_client_id: Optional[str] = None
        self._server: Optional[asyncio.Server] = None
        self._ser2net_reader: Optional[asyncio.StreamReader] = None
        self._ser2net_writer: Optional[asyncio.StreamWriter] = None
        self._running = False
        self._read_task: Optional[asyncio.Task] = None
        self._session_logger: Optional[SessionLogger] = None

    @property
    def client_count(self) -> int:
        """Return number of connected clients."""
        return len(self.clients)

    @property
    def is_running(self) -> bool:
        """Check if proxy is running."""
        return self._running

    async def start(self) -> None:
        """Start the proxy server."""
        if self._running:
            raise RuntimeError("Proxy already running")

        # Connect to ser2net
        logger.info(f"Connecting to ser2net at {self.ser2net_host}:{self.ser2net_port}")
        try:
            self._ser2net_reader, self._ser2net_writer = await asyncio.open_connection(
                self.ser2net_host, self.ser2net_port
            )
        except Exception as e:
            host_port = f"{self.ser2net_host}:{self.ser2net_port}"
            raise ConnectionError(f"Failed to connect to ser2net at {host_port}: {e}")

        # Start session logger
        if self.log_dir:
            self._session_logger = SessionLogger(self.log_dir, self.name)
            log_file = self._session_logger.start()
            logger.info(f"Session logging to {log_file}")

        # Start proxy server
        self._server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            self.proxy_port,
        )
        self._running = True

        # Start reading from ser2net
        self._read_task = asyncio.create_task(self._read_serial_loop())

        logger.info(f"Proxy '{self.name}' started on port {self.proxy_port}")

    async def stop(self) -> None:
        """Stop the proxy server gracefully."""
        if not self._running:
            return

        self._running = False
        logger.info(f"Stopping proxy '{self.name}'")

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        # Disconnect all clients
        for client in list(self.clients.values()):
            await self._disconnect_client(client, "Proxy shutting down")

        # Close ser2net connection
        if self._ser2net_writer:
            self._ser2net_writer.close()
            try:
                await self._ser2net_writer.wait_closed()
            except Exception:
                pass

        # Stop server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Stop session logger
        if self._session_logger:
            self._session_logger.stop()

        logger.info(f"Proxy '{self.name}' stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a new client connection."""
        client_id = str(uuid.uuid4())
        client = ProxyClient(
            client_id=client_id,
            reader=reader,
            writer=writer,
        )

        # Check max clients
        if len(self.clients) >= self.max_clients:
            logger.warning(f"Rejecting client {client_id}: max clients reached")
            writer.write(
                b"\r\n[Proxy: Maximum clients reached, connection refused]\r\n"
            )
            await writer.drain()
            writer.close()
            return

        self.clients[client_id] = client
        logger.info(f"Client {client_id[:8]} connected from {client.address}")

        # Send welcome message
        writer.write(f"\r\n[Proxy: Connected to {self.name}".encode())
        if self.writer_client_id:
            writer.write(b" (read-only, another client has write lock)]")
        else:
            writer.write(b" (you have write access)]")
        writer.write(b"\r\n")
        await writer.drain()

        try:
            await self._client_read_loop(client)
        except Exception as e:
            logger.debug(f"Client {client_id[:8]} error: {e}")
        finally:
            await self._disconnect_client(client, "Client disconnected")

    async def _client_read_loop(self, client: ProxyClient) -> None:
        """Read data from client and forward to serial if allowed."""
        while self._running:
            try:
                data = await asyncio.wait_for(client.reader.read(1024), timeout=1.0)
                if not data:
                    break  # Client disconnected

                client.last_activity = datetime.now()

                # Check write permission
                if not self._can_write(client.client_id):
                    # Try to acquire write lock
                    if self.writer_client_id is None:
                        self.writer_client_id = client.client_id
                        client.has_write_lock = True
                        logger.info(
                            f"Client {client.client_id[:8]} acquired write lock"
                        )
                        # Notify client
                        client.writer.write(
                            b"\r\n[Proxy: You now have write access]\r\n"
                        )
                        await client.writer.drain()
                    else:
                        # Notify client they can't write
                        continue  # Silently drop the write

                # Forward to ser2net
                if self._ser2net_writer and self._can_write(client.client_id):
                    self._ser2net_writer.write(data)
                    await self._ser2net_writer.drain()

                    # Log input
                    if self._session_logger:
                        self._session_logger.log_input(data, client.client_id)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Client read error: {e}")
                break

    def _can_write(self, client_id: str) -> bool:
        """Check if client can write based on policy."""
        if self.write_policy == "all":
            return True
        elif self.write_policy == "first":
            return self.writer_client_id is None or self.writer_client_id == client_id
        else:
            # queue policy - not implemented yet
            return self.writer_client_id is None or self.writer_client_id == client_id

    async def _read_serial_loop(self) -> None:
        """Read from ser2net and broadcast to all clients."""
        while self._running and self._ser2net_reader:
            try:
                data = await asyncio.wait_for(
                    self._ser2net_reader.read(4096), timeout=1.0
                )
                if not data:
                    logger.warning("ser2net connection closed")
                    break

                # Log output
                if self._session_logger:
                    self._session_logger.log_output(data)

                # Broadcast to all clients
                await self._broadcast(data)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Serial read error: {e}")
                break

        # Ser2net disconnected, stop proxy
        if self._running:
            logger.error("ser2net connection lost, stopping proxy")
            asyncio.create_task(self.stop())

    async def _broadcast(self, data: bytes) -> None:
        """Send data to all connected clients."""
        disconnected = []

        for client_id, client in self.clients.items():
            try:
                client.writer.write(data)
                await client.writer.drain()
            except Exception as e:
                logger.debug(f"Failed to send to client {client_id[:8]}: {e}")
                disconnected.append(client)

        # Clean up disconnected clients
        for client in disconnected:
            await self._disconnect_client(client, "Write failed")

    async def _disconnect_client(self, client: ProxyClient, reason: str) -> None:
        """Disconnect a client and clean up."""
        client_id = client.client_id

        if client_id not in self.clients:
            return

        del self.clients[client_id]

        # Release write lock if this client had it
        if self.writer_client_id == client_id:
            self.writer_client_id = None
            logger.info(f"Write lock released by {client_id[:8]}")

        try:
            client.writer.close()
            await client.writer.wait_closed()
        except Exception:
            pass

        logger.info(f"Client {client_id[:8]} disconnected: {reason}")

    def get_clients_info(self) -> list[dict]:
        """Get information about connected clients."""
        return [
            {
                "client_id": c.client_id,
                "address": c.address,
                "connected_at": c.connected_at.isoformat(),
                "has_write_lock": c.has_write_lock,
            }
            for c in self.clients.values()
        ]


class ProxyManager:
    """Manages multiple serial proxy instances."""

    def __init__(self, log_dir: Optional[Path] = None):
        self.proxies: dict[str, SerialProxy] = {}
        self.log_dir = log_dir
        self._port_counter = 5000

    def get_next_port(self, base: int = 5000, range_size: int = 100) -> int:
        """Get next available proxy port."""
        used_ports = {p.proxy_port for p in self.proxies.values()}
        for port in range(base, base + range_size):
            if port not in used_ports:
                return port
        raise RuntimeError("No available proxy ports")

    async def create_proxy(
        self,
        name: str,
        ser2net_host: str,
        ser2net_port: int,
        proxy_port: Optional[int] = None,
        write_policy: str = "first",
        max_clients: int = 10,
    ) -> SerialProxy:
        """Create and start a new proxy."""
        if name in self.proxies:
            raise ValueError(f"Proxy '{name}' already exists")

        if proxy_port is None:
            proxy_port = self.get_next_port()

        proxy = SerialProxy(
            name=name,
            ser2net_host=ser2net_host,
            ser2net_port=ser2net_port,
            proxy_port=proxy_port,
            log_dir=self.log_dir,
            write_policy=write_policy,
            max_clients=max_clients,
        )

        await proxy.start()
        self.proxies[name] = proxy
        return proxy

    async def stop_proxy(self, name: str) -> bool:
        """Stop and remove a proxy."""
        if name not in self.proxies:
            return False

        proxy = self.proxies.pop(name)
        await proxy.stop()
        return True

    async def stop_all(self) -> None:
        """Stop all proxies."""
        for name in list(self.proxies.keys()):
            await self.stop_proxy(name)

    def get_proxy(self, name: str) -> Optional[SerialProxy]:
        """Get a proxy by name."""
        return self.proxies.get(name)

    def list_proxies(self) -> list[dict]:
        """List all active proxies."""
        return [
            {
                "name": p.name,
                "proxy_port": p.proxy_port,
                "ser2net_port": p.ser2net_port,
                "client_count": p.client_count,
                "writer_client": p.writer_client_id[:8] if p.writer_client_id else None,
            }
            for p in self.proxies.values()
        ]
