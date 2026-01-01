"""Unit tests for serial proxy module."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from labctl.serial.proxy import (
    ProxyClient,
    ProxyManager,
    SerialProxy,
    SessionLogger,
)


class TestProxyClient:
    """Tests for ProxyClient dataclass."""

    def test_create_client(self):
        """Test creating a proxy client."""
        reader = MagicMock()
        writer = MagicMock()

        client = ProxyClient(
            client_id="test-123",
            reader=reader,
            writer=writer,
        )

        assert client.client_id == "test-123"
        assert client.reader is reader
        assert client.writer is writer
        assert client.has_write_lock is False
        assert isinstance(client.connected_at, datetime)

    def test_client_address_with_peername(self):
        """Test getting client address when peername is available."""
        reader = MagicMock()
        writer = MagicMock()
        writer.get_extra_info.return_value = ("192.168.1.100", 54321)

        client = ProxyClient(
            client_id="test-123",
            reader=reader,
            writer=writer,
        )

        assert client.address == "192.168.1.100:54321"

    def test_client_address_unknown(self):
        """Test getting client address when peername is not available."""
        reader = MagicMock()
        writer = MagicMock()
        writer.get_extra_info.return_value = None

        client = ProxyClient(
            client_id="test-123",
            reader=reader,
            writer=writer,
        )

        assert client.address == "unknown"


class TestSessionLogger:
    """Tests for SessionLogger."""

    def test_logger_start(self, tmp_path):
        """Test starting a session logger."""
        logger = SessionLogger(tmp_path, "test-sbc")
        log_file = logger.start()

        assert log_file.exists()
        assert "test-sbc" in log_file.name
        assert log_file.suffix == ".log"

        logger.stop()

    def test_logger_creates_directory(self, tmp_path):
        """Test logger creates log directory if missing."""
        log_dir = tmp_path / "logs" / "nested"
        logger = SessionLogger(log_dir, "test-sbc")

        log_file = logger.start()
        assert log_dir.exists()
        assert log_file.exists()

        logger.stop()

    def test_logger_writes_header(self, tmp_path):
        """Test logger writes session header."""
        logger = SessionLogger(tmp_path, "test-sbc")
        log_file = logger.start()
        logger.stop()

        content = log_file.read_text()
        assert "# Session: test-sbc" in content
        assert "# Started:" in content
        assert "# Ended:" in content

    def test_log_output(self, tmp_path):
        """Test logging output data (from device)."""
        logger = SessionLogger(tmp_path, "test-sbc")
        log_file = logger.start()

        logger.log_output(b"Hello World\n")
        logger.stop()

        content = log_file.read_text()
        assert ">>" in content
        assert "Hello World" in content

    def test_log_input(self, tmp_path):
        """Test logging input data (to device)."""
        logger = SessionLogger(tmp_path, "test-sbc")
        log_file = logger.start()

        logger.log_input(b"ls -la\n", "client-123")
        logger.stop()

        content = log_file.read_text()
        assert "<<" in content
        assert "ls -la" in content
        assert "client-1" in content  # First 8 chars of client ID


class TestSerialProxy:
    """Tests for SerialProxy class."""

    def test_proxy_init(self):
        """Test proxy initialization."""
        proxy = SerialProxy(
            name="test-sbc",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
        )

        assert proxy.name == "test-sbc"
        assert proxy.ser2net_host == "localhost"
        assert proxy.ser2net_port == 4001
        assert proxy.proxy_port == 5001
        assert proxy.write_policy == "first"
        assert proxy.max_clients == 10
        assert proxy.client_count == 0
        assert not proxy.is_running

    def test_proxy_init_with_options(self, tmp_path):
        """Test proxy initialization with custom options."""
        proxy = SerialProxy(
            name="test-sbc",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
            log_dir=tmp_path,
            write_policy="all",
            max_clients=5,
        )

        assert proxy.log_dir == tmp_path
        assert proxy.write_policy == "all"
        assert proxy.max_clients == 5

    def test_can_write_first_policy_no_writer(self):
        """Test can_write with first policy when no writer."""
        proxy = SerialProxy(
            name="test",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
            write_policy="first",
        )

        assert proxy._can_write("client-1") is True
        assert proxy._can_write("client-2") is True

    def test_can_write_first_policy_with_writer(self):
        """Test can_write with first policy when writer exists."""
        proxy = SerialProxy(
            name="test",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
            write_policy="first",
        )
        proxy.writer_client_id = "client-1"

        assert proxy._can_write("client-1") is True
        assert proxy._can_write("client-2") is False

    def test_can_write_all_policy(self):
        """Test can_write with all policy."""
        proxy = SerialProxy(
            name="test",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
            write_policy="all",
        )
        proxy.writer_client_id = "client-1"

        assert proxy._can_write("client-1") is True
        assert proxy._can_write("client-2") is True

    def test_get_clients_info_empty(self):
        """Test getting client info when no clients."""
        proxy = SerialProxy(
            name="test",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
        )

        info = proxy.get_clients_info()
        assert info == []

    def test_get_clients_info_with_clients(self):
        """Test getting client info with connected clients."""
        proxy = SerialProxy(
            name="test",
            ser2net_host="localhost",
            ser2net_port=4001,
            proxy_port=5001,
        )

        # Add mock clients
        reader = MagicMock()
        writer = MagicMock()
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)

        client = ProxyClient(
            client_id="abc-123",
            reader=reader,
            writer=writer,
            has_write_lock=True,
        )
        proxy.clients["abc-123"] = client

        info = proxy.get_clients_info()
        assert len(info) == 1
        assert info[0]["client_id"] == "abc-123"
        assert info[0]["address"] == "127.0.0.1:12345"
        assert info[0]["has_write_lock"] is True


class TestProxyManager:
    """Tests for ProxyManager class."""

    def test_manager_init(self, tmp_path):
        """Test manager initialization."""
        manager = ProxyManager(log_dir=tmp_path)

        assert manager.log_dir == tmp_path
        assert manager.proxies == {}

    def test_get_next_port(self):
        """Test getting next available port."""
        manager = ProxyManager()

        port = manager.get_next_port(base=5000, range_size=100)
        assert port == 5000

    def test_get_next_port_skips_used(self):
        """Test getting next port skips used ports."""
        manager = ProxyManager()

        # Add mock proxy using port 5000
        mock_proxy = MagicMock()
        mock_proxy.proxy_port = 5000
        manager.proxies["test1"] = mock_proxy

        port = manager.get_next_port(base=5000, range_size=100)
        assert port == 5001

    def test_get_next_port_exhausted(self):
        """Test error when no ports available."""
        manager = ProxyManager()

        # Fill up all ports
        for i in range(10):
            mock_proxy = MagicMock()
            mock_proxy.proxy_port = 5000 + i
            manager.proxies[f"test{i}"] = mock_proxy

        with pytest.raises(RuntimeError, match="No available proxy ports"):
            manager.get_next_port(base=5000, range_size=10)

    def test_list_proxies_empty(self):
        """Test listing proxies when none exist."""
        manager = ProxyManager()
        assert manager.list_proxies() == []

    def test_list_proxies_with_proxies(self):
        """Test listing proxies."""
        manager = ProxyManager()

        mock_proxy = MagicMock()
        mock_proxy.name = "test-sbc"
        mock_proxy.proxy_port = 5000
        mock_proxy.ser2net_port = 4001
        mock_proxy.client_count = 2
        mock_proxy.writer_client_id = "abc12345678"

        manager.proxies["test-sbc"] = mock_proxy

        proxies = manager.list_proxies()
        assert len(proxies) == 1
        assert proxies[0]["name"] == "test-sbc"
        assert proxies[0]["proxy_port"] == 5000
        assert proxies[0]["client_count"] == 2

    def test_get_proxy(self):
        """Test getting a proxy by name."""
        manager = ProxyManager()

        mock_proxy = MagicMock()
        manager.proxies["test-sbc"] = mock_proxy

        assert manager.get_proxy("test-sbc") is mock_proxy
        assert manager.get_proxy("nonexistent") is None


class TestProxyConfig:
    """Tests for ProxyConfig in config module."""

    def test_default_proxy_config(self):
        """Test default ProxyConfig values."""
        from labctl.core.config import ProxyConfig

        config = ProxyConfig()

        assert config.enabled is True
        assert config.port_base == 5000
        assert config.port_range == 100
        assert config.write_policy == "first"
        assert config.log_retention_days == 7
        assert config.max_clients == 10
        assert config.idle_timeout == 3600

    def test_proxy_config_in_main_config(self):
        """Test ProxyConfig is part of main Config."""
        from labctl.core.config import Config, ProxyConfig

        config = Config()

        assert hasattr(config, "proxy")
        assert isinstance(config.proxy, ProxyConfig)

    def test_proxy_config_from_dict(self):
        """Test loading ProxyConfig from dict."""
        from labctl.core.config import Config

        data = {
            "proxy": {
                "enabled": False,
                "port_base": 6000,
                "write_policy": "all",
            }
        }

        config = Config.from_dict(data)

        assert config.proxy.enabled is False
        assert config.proxy.port_base == 6000
        assert config.proxy.write_policy == "all"

    def test_proxy_config_to_dict(self):
        """Test serializing ProxyConfig to dict."""
        from labctl.core.config import Config

        config = Config()
        data = config.to_dict()

        assert "proxy" in data
        assert data["proxy"]["enabled"] is True
        assert data["proxy"]["port_base"] == 5000
        assert data["proxy"]["write_policy"] == "first"
