"""Unit tests for resource manager."""

import pytest

from labctl.core.manager import get_manager
from labctl.core.models import AddressType, PlugType, PortType, SerialDevice, Status


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


class TestSerialDeviceOperations:
    """Tests for serial device CRUD operations."""

    def test_create_serial_device(self, manager):
        """Test creating a serial device."""
        device = manager.create_serial_device(
            name="usb-ftdi-1",
            usb_path="1-10.1.3",
            vendor="FTDI",
            model="FT232R",
            serial_number="A12345",
        )

        assert device.id is not None
        assert device.name == "usb-ftdi-1"
        assert device.usb_path == "1-10.1.3"
        assert device.vendor == "FTDI"
        assert device.model == "FT232R"
        assert device.serial_number == "A12345"
        assert device.created_at is not None

    def test_create_serial_device_minimal(self, manager):
        """Test creating a serial device with only required fields."""
        device = manager.create_serial_device(
            name="minimal-adapter",
            usb_path="2-1.4",
        )

        assert device.id is not None
        assert device.name == "minimal-adapter"
        assert device.usb_path == "2-1.4"
        assert device.vendor is None
        assert device.model is None
        assert device.serial_number is None

    def test_get_serial_device_by_id(self, manager):
        """Test getting serial device by ID."""
        created = manager.create_serial_device(
            name="by-id-device",
            usb_path="1-2.3",
        )
        fetched = manager.get_serial_device(created.id)

        assert fetched is not None
        assert fetched.name == "by-id-device"
        assert fetched.usb_path == "1-2.3"

    def test_get_serial_device_nonexistent(self, manager):
        """Test getting non-existent serial device returns None."""
        assert manager.get_serial_device(999) is None

    def test_get_serial_device_by_name(self, manager):
        """Test getting serial device by name."""
        manager.create_serial_device(
            name="named-device",
            usb_path="3-1.2",
            vendor="Prolific",
        )
        fetched = manager.get_serial_device_by_name("named-device")

        assert fetched is not None
        assert fetched.vendor == "Prolific"

    def test_get_serial_device_by_name_nonexistent(self, manager):
        """Test getting non-existent serial device by name returns None."""
        assert manager.get_serial_device_by_name("no-such-device") is None

    def test_list_serial_devices(self, manager):
        """Test listing all serial devices."""
        manager.create_serial_device(name="dev-a", usb_path="1-1.1")
        manager.create_serial_device(name="dev-b", usb_path="1-1.2")
        manager.create_serial_device(name="dev-c", usb_path="1-1.3")

        devices = manager.list_serial_devices()
        assert len(devices) == 3
        # Should be ordered by name
        names = [d.name for d in devices]
        assert names == ["dev-a", "dev-b", "dev-c"]

    def test_list_serial_devices_empty(self, manager):
        """Test listing serial devices when none exist."""
        devices = manager.list_serial_devices()
        assert devices == []

    def test_rename_serial_device(self, manager):
        """Test renaming a serial device."""
        device = manager.create_serial_device(
            name="old-name",
            usb_path="1-5.1",
        )
        renamed = manager.rename_serial_device(device.id, "new-name")

        assert renamed is not None
        assert renamed.name == "new-name"
        assert renamed.usb_path == "1-5.1"  # Other fields unchanged

        # Old name should not resolve
        assert manager.get_serial_device_by_name("old-name") is None
        # New name should resolve
        assert manager.get_serial_device_by_name("new-name") is not None

    def test_rename_serial_device_nonexistent(self, manager):
        """Test renaming non-existent device returns None."""
        assert manager.rename_serial_device(999, "any-name") is None

    def test_delete_serial_device(self, manager):
        """Test deleting a serial device."""
        device = manager.create_serial_device(
            name="delete-me",
            usb_path="1-6.1",
        )
        result = manager.delete_serial_device(device.id)
        assert result is True
        assert manager.get_serial_device(device.id) is None

    def test_delete_serial_device_nonexistent(self, manager):
        """Test deleting non-existent device returns False."""
        assert manager.delete_serial_device(999) is False

    def test_delete_serial_device_fails_if_in_use(self, manager):
        """Test that deleting a device assigned to a port raises ValueError."""
        sbc = manager.create_sbc(name="sbc-with-device")
        device = manager.create_serial_device(
            name="in-use-device",
            usb_path="1-7.1",
        )

        # Assign port referencing this device
        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/in-use-device",
            serial_device_id=device.id,
        )

        with pytest.raises(ValueError, match="still assigned"):
            manager.delete_serial_device(device.id)

        # Device should still exist
        assert manager.get_serial_device(device.id) is not None


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

    def test_assign_serial_port_with_alias(self, manager):
        """Test assigning serial port with an alias."""
        sbc = manager.create_sbc(name="alias-test")

        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/alias-test",
            alias="my-console",
        )

        assert port.alias == "my-console"

    def test_assign_serial_port_with_serial_device_id(self, manager):
        """Test assigning serial port linked to a serial device."""
        sbc = manager.create_sbc(name="device-link-test")
        device = manager.create_serial_device(
            name="ftdi-adapter",
            usb_path="1-3.2",
            vendor="FTDI",
        )

        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/ftdi-adapter",
            serial_device_id=device.id,
        )

        assert port.serial_device_id == device.id

    def test_assign_serial_port_with_alias_and_device(self, manager):
        """Test assigning serial port with both alias and serial device."""
        sbc = manager.create_sbc(name="both-test")
        device = manager.create_serial_device(
            name="cp2102-adapter",
            usb_path="2-4.1",
        )

        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.DEBUG,
            device_path="/dev/lab/cp2102",
            alias="debug-port",
            serial_device_id=device.id,
        )

        assert port.alias == "debug-port"
        assert port.serial_device_id == device.id

    def test_alias_uniqueness_different_sbc(self, manager):
        """Test that alias must be unique across different SBCs."""
        sbc1 = manager.create_sbc(name="sbc-alias-1")
        sbc2 = manager.create_sbc(name="sbc-alias-2")

        manager.assign_serial_port(
            sbc_id=sbc1.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/sbc1",
            alias="shared-alias",
        )

        with pytest.raises(ValueError, match="already in use"):
            manager.assign_serial_port(
                sbc_id=sbc2.id,
                port_type=PortType.CONSOLE,
                device_path="/dev/lab/sbc2",
                alias="shared-alias",
            )

    def test_alias_uniqueness_same_sbc_different_type(self, manager):
        """Test that alias must be unique even on the same SBC with different port type."""
        sbc = manager.create_sbc(name="sbc-same-alias")

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/con",
            alias="dup-alias",
        )

        with pytest.raises(ValueError, match="already in use"):
            manager.assign_serial_port(
                sbc_id=sbc.id,
                port_type=PortType.DEBUG,
                device_path="/dev/lab/dbg",
                alias="dup-alias",
            )

    def test_alias_allowed_on_same_sbc_same_type_reassign(self, manager):
        """Test that re-assigning the same sbc/port_type with same alias works (upsert)."""
        sbc = manager.create_sbc(name="sbc-reassign")

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/old",
            alias="reassign-alias",
        )

        # Re-assign same sbc_id + port_type: the old row is deleted first (upsert),
        # so the alias should be available again.
        port = manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/new",
            alias="reassign-alias",
        )

        assert port.device_path == "/dev/lab/new"
        assert port.alias == "reassign-alias"

    def test_assign_with_invalid_serial_device_id(self, manager):
        """Test assigning port with non-existent serial_device_id raises ValueError."""
        sbc = manager.create_sbc(name="bad-device-ref")

        with pytest.raises(ValueError, match="Serial device with ID"):
            manager.assign_serial_port(
                sbc_id=sbc.id,
                port_type=PortType.CONSOLE,
                device_path="/dev/lab/bad",
                serial_device_id=999,
            )

    def test_get_serial_port_by_alias(self, manager):
        """Test getting serial port by alias."""
        sbc = manager.create_sbc(name="find-by-alias")

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/find-me",
            tcp_port=4050,
            alias="find-me-alias",
        )

        port = manager.get_serial_port_by_alias("find-me-alias")
        assert port is not None
        assert port.device_path == "/dev/lab/find-me"
        assert port.tcp_port == 4050
        assert port.alias == "find-me-alias"
        assert port.sbc_id == sbc.id

    def test_get_serial_port_by_alias_nonexistent(self, manager):
        """Test getting serial port by non-existent alias returns None."""
        assert manager.get_serial_port_by_alias("no-such-alias") is None

    def test_get_serial_port_by_alias_loads_serial_device(self, manager):
        """Test that get_serial_port_by_alias loads the related serial device."""
        sbc = manager.create_sbc(name="alias-with-device")
        device = manager.create_serial_device(
            name="my-ftdi",
            usb_path="1-8.1",
            vendor="FTDI",
            model="FT232R",
        )

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/my-ftdi",
            alias="ftdi-console",
            serial_device_id=device.id,
        )

        port = manager.get_serial_port_by_alias("ftdi-console")
        assert port is not None
        assert port.serial_device is not None
        assert port.serial_device.name == "my-ftdi"
        assert port.serial_device.vendor == "FTDI"

    def test_list_serial_ports_loads_serial_devices(self, manager):
        """Test that list_serial_ports populates serial_device on each port."""
        sbc = manager.create_sbc(name="list-with-device")
        device = manager.create_serial_device(
            name="list-adapter",
            usb_path="1-9.1",
        )

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/list-adapter",
            serial_device_id=device.id,
        )

        ports = manager.list_serial_ports()
        assert len(ports) == 1
        assert ports[0].serial_device is not None
        assert ports[0].serial_device.name == "list-adapter"

    def test_sbc_relations_load_serial_device(self, manager):
        """Test that get_sbc loads serial_device on each serial port."""
        sbc = manager.create_sbc(name="relations-test")
        device = manager.create_serial_device(
            name="relation-adapter",
            usb_path="1-10.1",
        )

        manager.assign_serial_port(
            sbc_id=sbc.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/relation-adapter",
            serial_device_id=device.id,
        )

        loaded_sbc = manager.get_sbc(sbc.id)
        assert len(loaded_sbc.serial_ports) == 1
        assert loaded_sbc.serial_ports[0].serial_device is not None
        assert loaded_sbc.serial_ports[0].serial_device.name == "relation-adapter"


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
