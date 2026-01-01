"""Unit tests for ser2net configuration generator."""

from labctl.serial.ser2net import Ser2NetPort, generate_ser2net_config


class TestSer2NetPort:
    """Tests for Ser2NetPort dataclass."""

    def test_default_values(self):
        """Test default port configuration values."""
        port = Ser2NetPort(
            name="test-port",
            device="/dev/lab/test",
            tcp_port=4000,
        )
        assert port.baud == 115200
        assert port.databits == 8
        assert port.parity == "none"
        assert port.stopbits == 1
        assert port.local is True
        assert port.kickolduser is False

    def test_custom_values(self):
        """Test custom port configuration values."""
        port = Ser2NetPort(
            name="custom-port",
            device="/dev/lab/custom",
            tcp_port=5000,
            baud=9600,
            databits=7,
            parity="even",
            stopbits=2,
            local=False,
            kickolduser=True,
        )
        assert port.baud == 9600
        assert port.databits == 7
        assert port.parity == "even"
        assert port.stopbits == 2
        assert port.local is False
        assert port.kickolduser is True


class TestGenerateSer2netConfig:
    """Tests for generate_ser2net_config function."""

    def test_single_port(self):
        """Test config generation for a single port."""
        ports = [
            Ser2NetPort(
                name="test-console",
                device="/dev/lab/test-console",
                tcp_port=4000,
            )
        ]
        config = generate_ser2net_config(ports)

        assert "%YAML 1.1" in config
        assert "connection: &test-console" in config
        assert "accepter: tcp,localhost,4000" in config
        assert "connector: serialdev,/dev/lab/test-console,115200n81,local" in config
        assert "enable: on" in config
        assert "kickolduser: false" in config

    def test_multiple_ports(self):
        """Test config generation for multiple ports."""
        ports = [
            Ser2NetPort(name="port1", device="/dev/lab/port1", tcp_port=4001),
            Ser2NetPort(name="port2", device="/dev/lab/port2", tcp_port=4002),
            Ser2NetPort(name="port3", device="/dev/lab/port3", tcp_port=4003),
        ]
        config = generate_ser2net_config(ports)

        assert "connection: &port1" in config
        assert "connection: &port2" in config
        assert "connection: &port3" in config
        assert "accepter: tcp,localhost,4001" in config
        assert "accepter: tcp,localhost,4002" in config
        assert "accepter: tcp,localhost,4003" in config

    def test_custom_baud_rate(self):
        """Test config with custom baud rate."""
        ports = [
            Ser2NetPort(
                name="slow-port",
                device="/dev/lab/slow",
                tcp_port=4000,
                baud=9600,
            )
        ]
        config = generate_ser2net_config(ports)

        assert "9600n81" in config

    def test_parity_settings(self):
        """Test different parity settings."""
        for parity, char in [("none", "n"), ("even", "e"), ("odd", "o")]:
            ports = [
                Ser2NetPort(
                    name="parity-test",
                    device="/dev/lab/test",
                    tcp_port=4000,
                    parity=parity,
                )
            ]
            config = generate_ser2net_config(ports)
            assert f"115200{char}81" in config

    def test_non_local_connection(self):
        """Test config for non-local (network accessible) port."""
        ports = [
            Ser2NetPort(
                name="network-port",
                device="/dev/lab/network",
                tcp_port=4000,
                local=False,
            )
        ]
        config = generate_ser2net_config(ports)

        # Non-local should not have localhost prefix
        assert "accepter: tcp,4000" in config
        assert "localhost" not in config.split("accepter")[1].split("\n")[0]

    def test_kickolduser_enabled(self):
        """Test config with kickolduser enabled."""
        ports = [
            Ser2NetPort(
                name="kick-port",
                device="/dev/lab/kick",
                tcp_port=4000,
                kickolduser=True,
            )
        ]
        config = generate_ser2net_config(ports)

        assert "kickolduser: true" in config

    def test_no_header(self):
        """Test config generation without header."""
        ports = [Ser2NetPort(name="test", device="/dev/lab/test", tcp_port=4000)]
        config = generate_ser2net_config(ports, include_header=False)

        assert "%YAML 1.1" not in config
        assert "---" not in config
        assert "connection: &test" in config

    def test_empty_ports_list(self):
        """Test config generation with empty ports list."""
        config = generate_ser2net_config([])

        assert "%YAML 1.1" in config
        assert "connection:" not in config
