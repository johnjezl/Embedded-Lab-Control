"""Unit tests for ser2net configuration generator."""

import pytest

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

    def test_bare_device_name_gets_prefix(self):
        """Test that device names without /dev/ get /dev/lab/ prefix."""
        ports = [
            Ser2NetPort(
                name="bare-port",
                device="port-2-3",  # No /dev/lab/ prefix
                tcp_port=4007,
            )
        ]
        config = generate_ser2net_config(ports)
        assert "serialdev,/dev/lab/port-2-3," in config

    def test_absolute_device_path_unchanged(self):
        """Test that absolute device paths pass through unchanged."""
        ports = [
            Ser2NetPort(
                name="abs-port",
                device="/dev/lab/port-2-3",
                tcp_port=4007,
            )
        ]
        config = generate_ser2net_config(ports)
        assert "serialdev,/dev/lab/port-2-3," in config
        # Should NOT double-prefix
        assert "/dev/lab//dev/lab/" not in config

    def test_to_ser2net_dict_bare_device(self):
        """Test that to_ser2net_dict prefixes bare device names."""
        port = Ser2NetPort(
            name="dict-test",
            device="port-1",
            tcp_port=4000,
        )
        d = port.to_ser2net_dict()
        assert "/dev/lab/port-1" in d["connector"]


class TestWriteSer2netConfig:
    """write_ser2net_config: the --install write, which may run as root."""

    def test_creates_missing_file(self, tmp_path):
        import stat

        from labctl.serial.ser2net import write_ser2net_config

        path = tmp_path / "ser2net.yaml"
        write_ser2net_config(str(path), "a: 1\n")
        assert path.read_text() == "a: 1\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_replaces_atomically_keeping_mode(self, tmp_path):
        import stat

        from labctl.serial.ser2net import write_ser2net_config

        path = tmp_path / "ser2net.yaml"
        path.write_text("old content that is longer\n")
        path.chmod(0o664)
        inode = path.stat().st_ino
        write_ser2net_config(str(path), "new\n")
        assert path.read_text() == "new\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o664
        assert path.stat().st_ino != inode  # renamed over, not rewritten
        assert [p.name for p in tmp_path.iterdir()] == ["ser2net.yaml"]

    def test_failed_write_keeps_old_file_and_no_temp(self, tmp_path, monkeypatch):
        from labctl.serial import ser2net

        path = tmp_path / "ser2net.yaml"
        path.write_text("old\n")

        def boom(fd):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(ser2net.os, "fsync", boom)
        with pytest.raises(OSError, match="No space"):
            ser2net.write_ser2net_config(str(path), "new\n")
        assert path.read_text() == "old\n"
        assert [p.name for p in tmp_path.iterdir()] == ["ser2net.yaml"]

    def test_unwritable_directory_rewrites_in_place(self, tmp_path):
        """A labctl group member can write /etc/ser2net.yaml but not /etc."""
        import stat

        from labctl.serial.ser2net import write_ser2net_config

        etc = tmp_path / "etc"
        etc.mkdir()
        path = etc / "ser2net.yaml"
        path.write_text("old content\n")
        path.chmod(0o664)
        inode = path.stat().st_ino
        etc.chmod(0o555)
        try:
            write_ser2net_config(str(path), "new\n")
        finally:
            etc.chmod(0o755)
        assert path.read_text() == "new\n"
        assert path.stat().st_ino == inode
        assert stat.S_IMODE(path.stat().st_mode) == 0o664

    def test_unwritable_directory_new_file_refused(self, tmp_path):
        from labctl.serial.ser2net import write_ser2net_config

        etc = tmp_path / "etc"
        etc.mkdir()
        etc.chmod(0o555)
        try:
            with pytest.raises(PermissionError):
                write_ser2net_config(str(etc / "ser2net.yaml"), "x\n")
        finally:
            etc.chmod(0o755)

    def test_symlink_refused(self, tmp_path):
        from labctl.serial.ser2net import write_ser2net_config

        victim = tmp_path / "victim"
        victim.write_text("secret\n")
        link = tmp_path / "ser2net.yaml"
        link.symlink_to(victim)
        with pytest.raises(OSError):
            write_ser2net_config(str(link), "x\n")
        assert victim.read_text() == "secret\n"

    def test_symlinked_parent_refused(self, tmp_path):
        from labctl.serial.ser2net import write_ser2net_config

        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "etc").symlink_to(real)
        with pytest.raises(OSError):
            write_ser2net_config(str(tmp_path / "etc" / "ser2net.yaml"), "x\n")
        assert not (real / "ser2net.yaml").exists()

    def test_hard_link_refused(self, tmp_path):
        import os

        from labctl.serial.ser2net import write_ser2net_config

        victim = tmp_path / "victim"
        victim.write_text("secret\n")
        os.link(victim, tmp_path / "ser2net.yaml")
        with pytest.raises(OSError, match="more than one hard link"):
            write_ser2net_config(str(tmp_path / "ser2net.yaml"), "x\n")
        assert victim.read_text() == "secret\n"

    def test_relative_path_refused(self):
        from labctl.serial.ser2net import write_ser2net_config

        with pytest.raises(ValueError):
            write_ser2net_config("ser2net.yaml", "x\n")


class TestCheckSer2netConfigWritable:
    """The pre-check `ser2net generate --install` runs before saving."""

    def test_writable_dir_ok(self, tmp_path):
        from labctl.serial.ser2net import check_ser2net_config_writable

        check_ser2net_config_writable(str(tmp_path / "ser2net.yaml"))
        assert not (tmp_path / "ser2net.yaml").exists()  # nothing created

    def test_unwritable_dir_missing_file_refused(self, tmp_path):
        from labctl.serial.ser2net import check_ser2net_config_writable

        etc = tmp_path / "etc"
        etc.mkdir()
        etc.chmod(0o555)
        try:
            with pytest.raises(PermissionError):
                check_ser2net_config_writable(str(etc / "ser2net.yaml"))
        finally:
            etc.chmod(0o755)

    def test_unwritable_dir_writable_file_ok(self, tmp_path):
        from labctl.serial.ser2net import check_ser2net_config_writable

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "ser2net.yaml").write_text("old\n")
        etc.chmod(0o555)
        try:
            check_ser2net_config_writable(str(etc / "ser2net.yaml"))
        finally:
            etc.chmod(0o755)
        assert (etc / "ser2net.yaml").read_text() == "old\n"

    def test_unwritable_dir_readonly_file_refused(self, tmp_path):
        from labctl.serial.ser2net import check_ser2net_config_writable

        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "ser2net.yaml").write_text("old\n")
        (etc / "ser2net.yaml").chmod(0o444)
        etc.chmod(0o555)
        try:
            with pytest.raises(PermissionError):
                check_ser2net_config_writable(str(etc / "ser2net.yaml"))
        finally:
            etc.chmod(0o755)

    def test_symlink_refused(self, tmp_path):
        from labctl.serial.ser2net import check_ser2net_config_writable

        (tmp_path / "victim").write_text("x")
        (tmp_path / "ser2net.yaml").symlink_to(tmp_path / "victim")
        with pytest.raises(OSError):
            check_ser2net_config_writable(str(tmp_path / "ser2net.yaml"))
