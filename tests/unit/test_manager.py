"""Unit tests for resource manager."""

import pytest

from labctl.core.manager import get_manager
from labctl.core.models import AddressType, PlugType, PortType, Status


@pytest.fixture
def manager(tmp_path):
    """Create a resource manager with test database."""
    db_path = tmp_path / "test.db"
    return get_manager(db_path)


class TestSBCOperations:
    """Tests for SBC CRUD operations."""

    def test_create_sbc(self, manager):
        """Test creating an SBC."""
        sbc = manager.create_sbc(
            name="test-sbc",
            project="test-project",
            description="Test description",
        )

        assert sbc.id is not None
        assert sbc.name == "test-sbc"
        assert sbc.project == "test-project"
        assert sbc.description == "Test description"
        assert sbc.status == Status.UNKNOWN

    def test_create_sbc_duplicate_name_fails(self, manager):
        """Test creating SBC with duplicate name fails."""
        manager.create_sbc(name="unique-name")

        with pytest.raises(ValueError, match="already exists"):
            manager.create_sbc(name="unique-name")

    def test_get_sbc_by_id(self, manager):
        """Test getting SBC by ID."""
        created = manager.create_sbc(name="by-id-test")
        fetched = manager.get_sbc(created.id)

        assert fetched is not None
        assert fetched.name == "by-id-test"

    def test_get_sbc_by_name(self, manager):
        """Test getting SBC by name."""
        manager.create_sbc(name="by-name-test", project="proj")
        fetched = manager.get_sbc_by_name("by-name-test")

        assert fetched is not None
        assert fetched.project == "proj"

    def test_get_nonexistent_sbc(self, manager):
        """Test getting non-existent SBC returns None."""
        assert manager.get_sbc(999) is None
        assert manager.get_sbc_by_name("nonexistent") is None

    def test_list_sbcs(self, manager):
        """Test listing SBCs."""
        manager.create_sbc(name="sbc1", project="proj1")
        manager.create_sbc(name="sbc2", project="proj2")
        manager.create_sbc(name="sbc3", project="proj1")

        all_sbcs = manager.list_sbcs()
        assert len(all_sbcs) == 3

        proj1_sbcs = manager.list_sbcs(project="proj1")
        assert len(proj1_sbcs) == 2

    def test_update_sbc(self, manager):
        """Test updating SBC."""
        sbc = manager.create_sbc(name="update-test")

        updated = manager.update_sbc(
            sbc.id,
            project="new-project",
            status=Status.ONLINE,
        )

        assert updated.project == "new-project"
        assert updated.status == Status.ONLINE

    def test_delete_sbc(self, manager):
        """Test deleting SBC."""
        sbc = manager.create_sbc(name="delete-test")

        result = manager.delete_sbc(sbc.id)
        assert result is True

        assert manager.get_sbc(sbc.id) is None

    def test_delete_nonexistent_sbc(self, manager):
        """Test deleting non-existent SBC returns False."""
        assert manager.delete_sbc(999) is False


class TestSerialPortOperations:
    """Tests for serial port operations."""

    def test_assign_serial_port(self, manager):
        """Test assigning serial port."""
        sbc = manager.create_sbc(name="port-test")

        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/test",
            tcp_port=5000,
            baud_rate=9600,
        )

        assert port.sbc_id == sbc.id
        assert port.port_type == PortType.CONSOLE
        assert port.device_path == "/dev/lab/test"
        assert port.tcp_port == 5000
        assert port.baud_rate == 9600

    def test_assign_port_auto_tcp(self, manager):
        """Test auto-assignment of TCP port."""
        sbc = manager.create_sbc(name="auto-tcp-test")

        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/test",
        )

        assert port.tcp_port == 4000  # Base port

    def test_assign_port_replaces_existing(self, manager):
        """Test assigning port replaces existing assignment."""
        sbc = manager.create_sbc(name="replace-test")

        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/old")
        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/new")

        sbc = manager.get_sbc(sbc.id)
        assert len(sbc.serial_ports) == 1
        assert sbc.serial_ports[0].device_path == "/dev/new"

    def test_remove_serial_port(self, manager):
        """Test removing serial port."""
        sbc = manager.create_sbc(name="remove-port-test")
        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/test")

        result = manager.remove_serial_port(sbc.id, PortType.CONSOLE)
        assert result is True

        sbc = manager.get_sbc(sbc.id)
        assert len(sbc.serial_ports) == 0

    def test_list_serial_ports(self, manager):
        """Test listing all serial ports."""
        sbc1 = manager.create_sbc(name="sbc1")
        sbc2 = manager.create_sbc(name="sbc2")

        manager.assign_serial_port(sbc1.id, PortType.CONSOLE, "/dev/lab/sbc1")
        manager.assign_serial_port(sbc2.id, PortType.CONSOLE, "/dev/lab/sbc2")

        ports = manager.list_serial_ports()
        assert len(ports) == 2


class TestNetworkAddressOperations:
    """Tests for network address operations."""

    def test_set_network_address(self, manager):
        """Test setting network address."""
        sbc = manager.create_sbc(name="network-test")

        addr = manager.set_network_address(
            sbc_id=sbc.id,
            address_type=AddressType.ETHERNET,
            ip_address="192.168.1.100",
            mac_address="aa:bb:cc:dd:ee:ff",
        )

        assert addr.ip_address == "192.168.1.100"
        assert addr.mac_address == "aa:bb:cc:dd:ee:ff"

    def test_sbc_primary_ip(self, manager):
        """Test SBC primary_ip property."""
        sbc = manager.create_sbc(name="ip-test")

        manager.set_network_address(sbc.id, AddressType.ETHERNET, "10.0.0.1")

        sbc = manager.get_sbc(sbc.id)
        assert sbc.primary_ip == "10.0.0.1"


class TestPowerPlugOperations:
    """Tests for power plug operations."""

    def test_assign_power_plug(self, manager):
        """Test assigning power plug."""
        sbc = manager.create_sbc(name="power-test")

        plug = manager.assign_power_plug(
            sbc_id=sbc.id,
            plug_type=PlugType.TASMOTA,
            address="192.168.1.50",
            plug_index=2,
        )

        assert plug.plug_type == PlugType.TASMOTA
        assert plug.address == "192.168.1.50"
        assert plug.plug_index == 2

    def test_remove_power_plug(self, manager):
        """Test removing power plug."""
        sbc = manager.create_sbc(name="remove-plug-test")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "192.168.1.50")

        result = manager.remove_power_plug(sbc.id)
        assert result is True

        sbc = manager.get_sbc(sbc.id)
        assert sbc.power_plug is None


class TestCascadeDelete:
    """Tests for cascade delete behavior."""

    def test_delete_sbc_removes_ports(self, manager):
        """Test deleting SBC removes associated ports."""
        sbc = manager.create_sbc(name="cascade-test")
        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/test")

        manager.delete_sbc(sbc.id)

        ports = manager.list_serial_ports()
        assert len(ports) == 0
