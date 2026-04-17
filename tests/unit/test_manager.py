"""Unit tests for resource manager."""

import time

import pytest

from labctl.core.manager import get_manager
from labctl.core.models import (
    AddressType,
    ClaimConflict,
    ClaimNotFoundError,
    NotClaimantError,
    PlugType,
    PortType,
    ReleaseReason,
    SerialDevice,
    Status,
    UnknownSBCError,
)


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


class TestSDWireDeviceOperations:
    """Tests for SDWire device CRUD operations."""

    def test_create_sdwire_device(self, manager):
        """Test creating an SDWireC device."""
        device = manager.create_sdwire_device(
            name="sdwire-1",
            serial_number="bdgrd_sdwirec_522",
        )
        assert device.id is not None
        assert device.name == "sdwire-1"
        assert device.serial_number == "bdgrd_sdwirec_522"
        assert device.device_type == "sdwirec"
        assert device.created_at is not None

    def test_create_sdwire_device_duplicate_name(self, manager):
        """Test creating a device with duplicate name fails."""
        manager.create_sdwire_device(name="dup-name", serial_number="serial-1")
        with pytest.raises(Exception):
            manager.create_sdwire_device(name="dup-name", serial_number="serial-2")

    def test_create_sdwire_device_duplicate_serial(self, manager):
        """Test creating a device with duplicate serial fails."""
        manager.create_sdwire_device(name="name-1", serial_number="dup-serial")
        with pytest.raises(Exception):
            manager.create_sdwire_device(name="name-2", serial_number="dup-serial")

    def test_get_sdwire_device_by_id(self, manager):
        """Test getting an SDWire device by ID."""
        created = manager.create_sdwire_device(
            name="get-by-id", serial_number="serial-get-id"
        )
        fetched = manager.get_sdwire_device(created.id)
        assert fetched is not None
        assert fetched.name == "get-by-id"
        assert fetched.serial_number == "serial-get-id"

    def test_get_sdwire_device_nonexistent(self, manager):
        """Test getting a non-existent SDWire device returns None."""
        assert manager.get_sdwire_device(999) is None

    def test_get_sdwire_device_by_name(self, manager):
        """Test getting an SDWire device by name."""
        manager.create_sdwire_device(name="by-name", serial_number="serial-by-name")
        fetched = manager.get_sdwire_device_by_name("by-name")
        assert fetched is not None
        assert fetched.serial_number == "serial-by-name"

    def test_get_sdwire_device_by_name_nonexistent(self, manager):
        """Test getting a non-existent device by name returns None."""
        assert manager.get_sdwire_device_by_name("nope") is None

    def test_list_sdwire_devices(self, manager):
        """Test listing SDWire devices returns all, sorted by name."""
        manager.create_sdwire_device(name="z-device", serial_number="s1")
        manager.create_sdwire_device(name="a-device", serial_number="s2")
        manager.create_sdwire_device(name="m-device", serial_number="s3")

        devices = manager.list_sdwire_devices()
        assert len(devices) == 3
        assert devices[0].name == "a-device"
        assert devices[1].name == "m-device"
        assert devices[2].name == "z-device"

    def test_list_sdwire_devices_empty(self, manager):
        """Test listing returns empty list when none registered."""
        assert manager.list_sdwire_devices() == []

    def test_delete_sdwire_device(self, manager):
        """Test deleting an SDWire device."""
        device = manager.create_sdwire_device(
            name="delete-me", serial_number="s-delete"
        )
        assert manager.delete_sdwire_device(device.id) is True
        assert manager.get_sdwire_device(device.id) is None

    def test_delete_sdwire_device_nonexistent(self, manager):
        """Test deleting a non-existent device returns False."""
        assert manager.delete_sdwire_device(999) is False

    def test_delete_sdwire_device_fails_if_assigned(self, manager):
        """Test deleting a device that is assigned to an SBC raises ValueError."""
        sbc = manager.create_sbc(name="sdwire-sbc")
        device = manager.create_sdwire_device(
            name="assigned-sw", serial_number="s-assigned"
        )
        manager.assign_sdwire(sbc.id, device.id)

        with pytest.raises(ValueError, match="still assigned"):
            manager.delete_sdwire_device(device.id)


class TestSDWireAssignment:
    """Tests for SDWire assignment operations."""

    def test_assign_sdwire(self, manager):
        """Test assigning an SDWire device to an SBC."""
        sbc = manager.create_sbc(name="sw-assign-sbc")
        device = manager.create_sdwire_device(
            name="sw-assign", serial_number="s-assign"
        )

        manager.assign_sdwire(sbc.id, device.id)

        loaded = manager.get_sbc(sbc.id)
        assert loaded.sdwire is not None
        assert loaded.sdwire.name == "sw-assign"
        assert loaded.sdwire.serial_number == "s-assign"

    def test_assign_sdwire_replaces_existing(self, manager):
        """Test assigning a new SDWire replaces the existing one."""
        sbc = manager.create_sbc(name="sw-replace-sbc")
        dev1 = manager.create_sdwire_device(name="sw-old", serial_number="s-old")
        dev2 = manager.create_sdwire_device(name="sw-new", serial_number="s-new")

        manager.assign_sdwire(sbc.id, dev1.id)
        manager.assign_sdwire(sbc.id, dev2.id)

        loaded = manager.get_sbc(sbc.id)
        assert loaded.sdwire.name == "sw-new"

    def test_assign_sdwire_bad_sbc(self, manager):
        """Test assigning to non-existent SBC raises ValueError."""
        device = manager.create_sdwire_device(
            name="sw-bad-sbc", serial_number="s-bad-sbc"
        )
        with pytest.raises(ValueError, match="SBC with ID"):
            manager.assign_sdwire(999, device.id)

    def test_assign_sdwire_bad_device(self, manager):
        """Test assigning non-existent device raises ValueError."""
        sbc = manager.create_sbc(name="sw-bad-dev-sbc")
        with pytest.raises(ValueError, match="SDWire device with ID"):
            manager.assign_sdwire(sbc.id, 999)

    def test_unassign_sdwire(self, manager):
        """Test unassigning an SDWire from an SBC."""
        sbc = manager.create_sbc(name="sw-unassign-sbc")
        device = manager.create_sdwire_device(
            name="sw-unassign", serial_number="s-unassign"
        )
        manager.assign_sdwire(sbc.id, device.id)

        assert manager.unassign_sdwire(sbc.id) is True

        loaded = manager.get_sbc(sbc.id)
        assert loaded.sdwire is None

    def test_unassign_sdwire_when_none(self, manager):
        """Test unassigning returns False when no SDWire is assigned."""
        sbc = manager.create_sbc(name="sw-none-sbc")
        assert manager.unassign_sdwire(sbc.id) is False

    def test_sbc_without_sdwire(self, manager):
        """Test SBC loads with sdwire=None when not assigned."""
        sbc = manager.create_sbc(name="no-sdwire")
        loaded = manager.get_sbc(sbc.id)
        assert loaded.sdwire is None

    def test_list_sbcs_loads_sdwire(self, manager):
        """Test that list_sbcs populates sdwire on each SBC."""
        sbc = manager.create_sbc(name="list-sw-sbc")
        device = manager.create_sdwire_device(name="list-sw", serial_number="s-list")
        manager.assign_sdwire(sbc.id, device.id)

        sbcs = manager.list_sbcs()
        assert len(sbcs) == 1
        assert sbcs[0].sdwire is not None
        assert sbcs[0].sdwire.name == "list-sw"

    def test_delete_sbc_cascades_sdwire_assignment(self, manager):
        """Test deleting an SBC removes its SDWire assignment."""
        sbc = manager.create_sbc(name="cascade-sw-sbc")
        device = manager.create_sdwire_device(
            name="cascade-sw", serial_number="s-cascade"
        )
        manager.assign_sdwire(sbc.id, device.id)

        manager.delete_sbc(sbc.id)

        # Device still exists, but assignment is gone
        assert manager.get_sdwire_device_by_name("cascade-sw") is not None
        # Can now delete the device since it's unassigned
        manager.delete_sdwire_device(device.id)


class TestCascadeDelete:
    """Tests for cascade delete behavior."""

    def test_delete_sbc_removes_ports(self, manager):
        """Test deleting SBC removes associated ports."""
        sbc = manager.create_sbc(name="cascade-test")
        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/test")

        manager.delete_sbc(sbc.id)

        ports = manager.list_serial_ports()
        assert len(ports) == 0


class TestStatusHistory:
    """Tests for status logging, history, and uptime."""

    def test_log_status(self, manager):
        """Test logging a status entry."""
        sbc = manager.create_sbc(name="status-sbc")
        log_id = manager.log_status(sbc.id, Status.ONLINE, "Boot complete")
        assert log_id > 0

    def test_get_status_history_by_sbc(self, manager):
        """Test retrieving status history for a specific SBC."""
        sbc = manager.create_sbc(name="hist-sbc")
        manager.log_status(sbc.id, Status.ONLINE, "Boot")
        manager.log_status(sbc.id, Status.OFFLINE, "Shutdown")

        history = manager.get_status_history(sbc_id=sbc.id)
        assert len(history) == 2
        statuses = {h["status"] for h in history}
        assert statuses == {"online", "offline"}
        assert all(h["sbc_name"] == "hist-sbc" for h in history)
        details = {h["details"] for h in history}
        assert "Boot" in details
        assert "Shutdown" in details

    def test_get_status_history_all(self, manager):
        """Test retrieving status history for all SBCs."""
        sbc1 = manager.create_sbc(name="sbc-a")
        sbc2 = manager.create_sbc(name="sbc-b")
        manager.log_status(sbc1.id, Status.ONLINE)
        manager.log_status(sbc2.id, Status.OFFLINE)

        history = manager.get_status_history()
        assert len(history) == 2
        sbc_names = {h["sbc_name"] for h in history}
        assert sbc_names == {"sbc-a", "sbc-b"}

    def test_get_status_history_limit(self, manager):
        """Test status history respects limit parameter."""
        sbc = manager.create_sbc(name="limit-sbc")
        for i in range(10):
            manager.log_status(sbc.id, Status.ONLINE, f"entry-{i}")

        history = manager.get_status_history(sbc_id=sbc.id, limit=3)
        assert len(history) == 3

    def test_get_status_history_empty(self, manager):
        """Test status history returns empty list when no entries."""
        sbc = manager.create_sbc(name="empty-sbc")
        history = manager.get_status_history(sbc_id=sbc.id)
        assert history == []

    def test_get_status_history_dict_keys(self, manager):
        """Test status history returns correct dict keys."""
        sbc = manager.create_sbc(name="keys-sbc")
        manager.log_status(sbc.id, Status.ONLINE)

        history = manager.get_status_history(sbc_id=sbc.id)
        assert len(history) == 1
        entry = history[0]
        assert "id" in entry
        assert "sbc_id" in entry
        assert "sbc_name" in entry
        assert "status" in entry
        assert "details" in entry
        assert "logged_at" in entry

    def test_get_uptime_no_history(self, manager):
        """Test uptime returns None for nonexistent SBC."""
        result = manager.get_uptime(99999)
        assert result is None

    def test_get_uptime_online(self, manager):
        """Test uptime calculation for an online SBC."""
        sbc = manager.create_sbc(name="uptime-sbc")
        manager.update_sbc(sbc.id, status=Status.ONLINE)
        manager.log_status(sbc.id, Status.ONLINE, "Boot")

        result = manager.get_uptime(sbc.id)
        assert result is not None
        assert result["sbc_name"] == "uptime-sbc"
        assert result["current_status"] == "online"
        assert isinstance(result["current_uptime_seconds"], int)
        assert "current_uptime_formatted" in result
        assert "uptime_24h_percent" in result

    def test_get_uptime_offline(self, manager):
        """Test uptime for SBC that went offline."""
        sbc = manager.create_sbc(name="offline-sbc")
        manager.log_status(sbc.id, Status.ONLINE, "Boot")
        manager.log_status(sbc.id, Status.OFFLINE, "Shutdown")

        result = manager.get_uptime(sbc.id)
        assert result is not None
        assert result["current_uptime_seconds"] == 0

    def test_get_uptime_24h_percent(self, manager):
        """Test 24h uptime percentage is present and numeric."""
        sbc = manager.create_sbc(name="pct-sbc")
        manager.update_sbc(sbc.id, status=Status.ONLINE)
        manager.log_status(sbc.id, Status.ONLINE, "Boot")

        result = manager.get_uptime(sbc.id)
        assert result is not None
        assert isinstance(result["uptime_24h_percent"], float)

    def test_cleanup_old_status_logs(self, manager):
        """Test cleaning up old status log entries."""
        sbc = manager.create_sbc(name="cleanup-sbc")
        manager.log_status(sbc.id, Status.ONLINE)
        manager.log_status(sbc.id, Status.OFFLINE)

        # With large retention, nothing should be deleted
        deleted = manager.cleanup_old_status_logs(365)
        assert deleted == 0

        # All entries should still exist
        history = manager.get_status_history(sbc_id=sbc.id)
        assert len(history) == 2

    def test_cleanup_returns_int(self, manager):
        """Test cleanup returns integer count."""
        deleted = manager.cleanup_old_status_logs(30)
        assert isinstance(deleted, int)
        assert deleted >= 0


class TestSBCToDict:
    """Tests for SBC.to_dict() serialization."""

    def test_to_dict_basic(self, manager):
        """Test basic SBC serialization without IDs."""
        sbc = manager.create_sbc(name="dict-sbc", project="proj", description="desc")
        d = sbc.to_dict()
        assert d["name"] == "dict-sbc"
        assert d["project"] == "proj"
        assert d["description"] == "desc"
        assert d["status"] == "unknown"
        assert "id" not in d

    def test_to_dict_with_ids(self, manager):
        """Test SBC serialization includes IDs when requested."""
        sbc = manager.create_sbc(name="id-sbc")
        d = sbc.to_dict(include_ids=True)
        assert "id" in d
        assert d["id"] == sbc.id

    def test_to_dict_with_serial_ports(self, manager):
        """Test serialization includes serial port data."""
        sbc = manager.create_sbc(name="port-sbc")
        manager.assign_serial_port(
            sbc.id,
            PortType.CONSOLE,
            "/dev/test",
            tcp_port=4000,
            alias="test-console",
        )
        sbc = manager.get_sbc_by_name("port-sbc")
        d = sbc.to_dict()
        assert "serial_ports" in d
        assert len(d["serial_ports"]) == 1
        assert d["serial_ports"][0]["type"] == "console"
        assert d["serial_ports"][0]["alias"] == "test-console"
        assert "id" not in d["serial_ports"][0]

    def test_to_dict_with_serial_ports_ids(self, manager):
        """Test port IDs included when include_ids=True."""
        sbc = manager.create_sbc(name="port-id-sbc")
        manager.assign_serial_port(sbc.id, PortType.CONSOLE, "/dev/test", tcp_port=4000)
        sbc = manager.get_sbc_by_name("port-id-sbc")
        d = sbc.to_dict(include_ids=True)
        assert "id" in d["serial_ports"][0]

    def test_to_dict_with_network(self, manager):
        """Test serialization includes network address data."""
        sbc = manager.create_sbc(name="net-sbc")
        manager.set_network_address(sbc.id, AddressType.ETHERNET, "192.168.1.100")
        sbc = manager.get_sbc_by_name("net-sbc")
        d = sbc.to_dict()
        assert "network_addresses" in d
        assert d["network_addresses"][0]["ip"] == "192.168.1.100"

    def test_to_dict_with_power_plug(self, manager):
        """Test serialization includes power plug data."""
        sbc = manager.create_sbc(name="power-sbc")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "192.168.1.50")
        sbc = manager.get_sbc_by_name("power-sbc")
        d = sbc.to_dict()
        assert "power_plug" in d
        assert d["power_plug"]["type"] == "tasmota"
        assert d["power_plug"]["address"] == "192.168.1.50"

    def test_to_dict_with_sdwire(self, manager):
        """Test serialization includes SDWire data."""
        sbc = manager.create_sbc(name="sdwire-sbc")
        device = manager.create_sdwire_device("sw1", "serial123")
        manager.assign_sdwire(sbc.id, device.id)
        sbc = manager.get_sbc_by_name("sdwire-sbc")
        d = sbc.to_dict()
        assert "sdwire" in d
        assert d["sdwire"]["name"] == "sw1"
        assert d["sdwire"]["serial_number"] == "serial123"

    def test_to_dict_empty_relations(self, manager):
        """Test serialization omits empty relation fields."""
        sbc = manager.create_sbc(name="empty-sbc")
        d = sbc.to_dict()
        assert "serial_ports" not in d
        assert "network_addresses" not in d
        assert "power_plug" not in d
        assert "sdwire" not in d


class TestUpsertBehavior:
    """Tests for atomic upsert (reassign) behavior."""

    def test_network_address_upsert(self, manager):
        """Test setting network address replaces existing."""
        sbc = manager.create_sbc(name="upsert-net")
        manager.set_network_address(sbc.id, AddressType.ETHERNET, "10.0.0.1")
        manager.set_network_address(sbc.id, AddressType.ETHERNET, "10.0.0.2")
        sbc = manager.get_sbc_by_name("upsert-net")
        eth_addrs = [
            a for a in sbc.network_addresses if a.address_type == AddressType.ETHERNET
        ]
        assert len(eth_addrs) == 1
        assert eth_addrs[0].ip_address == "10.0.0.2"

    def test_power_plug_upsert(self, manager):
        """Test assigning power plug replaces existing."""
        sbc = manager.create_sbc(name="upsert-power")
        manager.assign_power_plug(sbc.id, PlugType.TASMOTA, "192.168.1.50")
        manager.assign_power_plug(sbc.id, PlugType.SHELLY, "192.168.1.60")
        sbc = manager.get_sbc_by_name("upsert-power")
        assert sbc.power_plug is not None
        assert sbc.power_plug.plug_type == PlugType.SHELLY
        assert sbc.power_plug.address == "192.168.1.60"

    def test_sdwire_upsert(self, manager):
        """Test assigning SDWire replaces existing assignment."""
        sbc = manager.create_sbc(name="upsert-sdwire")
        dev1 = manager.create_sdwire_device("sw-a", "serial-a")
        dev2 = manager.create_sdwire_device("sw-b", "serial-b")
        manager.assign_sdwire(sbc.id, dev1.id)
        manager.assign_sdwire(sbc.id, dev2.id)
        sbc = manager.get_sbc_by_name("upsert-sdwire")
        assert sbc.sdwire is not None
        assert sbc.sdwire.name == "sw-b"


def _make_session(name: str = "agent-a", kind: str = "cli") -> tuple[str, str, str]:
    return (name, f"{kind}-{name}-session", kind)


class TestClaimAcquisition:
    """Acquisition, conflict, and basic ownership semantics."""

    def test_claim_sbc_success(self, manager):
        manager.create_sbc(name="sbc1")
        agent, sid, kind = _make_session()
        claim = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name=agent,
            session_id=sid,
            session_kind=kind,
            duration_seconds=600,
            reason="bringup testing",
        )
        assert claim.id is not None
        assert claim.agent_name == agent
        assert claim.session_id == sid
        assert claim.is_active
        assert claim.duration_seconds == 600
        assert claim.expires_at is not None

    def test_claim_conflict_raises(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=600,
            reason="first",
        )
        with pytest.raises(ClaimConflict) as excinfo:
            manager.claim_sbc(
                sbc_name="sbc1",
                agent_name="b",
                session_id="s2",
                session_kind="cli",
                duration_seconds=600,
                reason="second",
            )
        assert excinfo.value.claim.agent_name == "a"

    def test_claim_unknown_sbc_raises(self, manager):
        with pytest.raises(UnknownSBCError):
            manager.claim_sbc(
                sbc_name="missing",
                agent_name="a",
                session_id="s1",
                session_kind="cli",
                duration_seconds=60,
                reason="r",
            )

    def test_get_active_claim_returns_none_when_free(self, manager):
        manager.create_sbc(name="sbc1")
        assert manager.get_active_claim("sbc1") is None

    def test_context_json_roundtrip(self, manager):
        manager.create_sbc(name="sbc1")
        ctx = {"branch": "feat/claims", "ticket": "LAB-42"}
        claim = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
            context=ctx,
        )
        fetched = manager.get_active_claim("sbc1")
        assert claim.context == ctx
        assert fetched.context == ctx


class TestClaimRelease:
    """Explicit release, force-release, and session ownership enforcement."""

    def test_release_by_claimant(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        released = manager.release_claim("sbc1", "s1")
        assert released.released_at is not None
        assert released.release_reason == ReleaseReason.RELEASED
        assert manager.get_active_claim("sbc1") is None

    def test_release_by_non_claimant_raises(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        with pytest.raises(NotClaimantError):
            manager.release_claim("sbc1", "s2")

    def test_release_with_no_claim_raises(self, manager):
        manager.create_sbc(name="sbc1")
        with pytest.raises(ClaimNotFoundError):
            manager.release_claim("sbc1", "s1")

    def test_force_release_bypasses_session_check(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        released = manager.force_release_claim(
            "sbc1", "stuck agent, need the bench", released_by="john"
        )
        assert released.released_at is not None
        assert released.release_reason == ReleaseReason.FORCE_RELEASED
        assert released.released_by == "john"
        assert manager.get_active_claim("sbc1") is None

    def test_allow_reclaim_after_release(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        manager.release_claim("sbc1", "s1")
        # New claim by different session should succeed
        claim = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="b",
            session_id="s2",
            session_kind="cli",
            duration_seconds=60,
            reason="second",
        )
        assert claim.is_active


class TestClaimExpiryAndHeartbeat:
    """Expiry sweep, renewal, and heartbeat semantics."""

    def test_expire_stale_releases_past_deadline(self, manager):
        manager.create_sbc(name="sbc1")
        # Claim with a 1-second duration; wait longer than grace
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=1,
            reason="short",
        )
        time.sleep(1.2)
        # grace=0 lets the sweep release immediately past the deadline
        count = manager.expire_stale_claims(grace_seconds=0)
        assert count == 1
        history = manager.list_claim_history("sbc1")
        assert len(history) == 1
        assert history[0].release_reason == ReleaseReason.EXPIRED
        assert history[0].released_by == "system"

    def test_expired_claim_not_considered_active(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=1,
            reason="short",
        )
        time.sleep(1.2)
        # Even before a sweep runs, is_active (deadline-based) is False
        assert manager.get_active_claim("sbc1") is None

    def test_reclaim_over_expired_without_explicit_sweep(self, manager):
        """Acquisition must succeed when the old claim is past its deadline,
        even if no sweep has run — claim_sbc sweeps inside the transaction."""
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=1,
            reason="short",
        )
        time.sleep(1.2)
        # grace=0 so the in-transaction sweep fires
        claim = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="b",
            session_id="s2",
            session_kind="cli",
            duration_seconds=60,
            reason="takeover",
            grace_seconds=0,
        )
        assert claim.is_active
        assert claim.agent_name == "b"

    def test_renew_extends_deadline(self, manager):
        manager.create_sbc(name="sbc1")
        original = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        time.sleep(1.05)
        renewed = manager.renew_claim("sbc1", "s1", duration_seconds=120)
        assert renewed.renewal_count == original.renewal_count + 1
        assert renewed.duration_seconds == 120
        assert renewed.expires_at > original.expires_at

    def test_renew_by_non_claimant_raises(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        with pytest.raises(NotClaimantError):
            manager.renew_claim("sbc1", "s2")

    def test_heartbeat_advances_last_activity(self, manager):
        manager.create_sbc(name="sbc1")
        original = manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        time.sleep(1.05)
        assert manager.heartbeat_claim("sbc1", "s1") is True
        fresh = manager.get_active_claim("sbc1")
        assert fresh.last_activity > original.last_activity
        assert fresh.expires_at > original.expires_at

    def test_heartbeat_from_other_session_silent_no_op(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        assert manager.heartbeat_claim("sbc1", "other-session") is False

    def test_heartbeat_with_no_claim_returns_false(self, manager):
        manager.create_sbc(name="sbc1")
        assert manager.heartbeat_claim("sbc1", "any") is False


class TestClaimListing:
    """list_active_claims and list_claim_history."""

    def test_list_active_claims_across_sbcs(self, manager):
        manager.create_sbc(name="sbc1")
        manager.create_sbc(name="sbc2")
        manager.create_sbc(name="sbc3")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        manager.claim_sbc(
            sbc_name="sbc3",
            agent_name="c",
            session_id="s3",
            session_kind="mcp-stdio",
            duration_seconds=120,
            reason="r",
        )
        claims = manager.list_active_claims()
        names = {c.sbc_name for c in claims}
        assert names == {"sbc1", "sbc3"}

    def test_history_preserves_released_claims(self, manager):
        manager.create_sbc(name="sbc1")
        for i in range(3):
            manager.claim_sbc(
                sbc_name="sbc1",
                agent_name=f"a{i}",
                session_id=f"s{i}",
                session_kind="cli",
                duration_seconds=60,
                reason=f"cycle {i}",
            )
            manager.release_claim("sbc1", f"s{i}")
        history = manager.list_claim_history("sbc1")
        assert len(history) == 3
        # Newest first
        assert [c.agent_name for c in history] == ["a2", "a1", "a0"]


class TestClaimRequests:
    """Polite release request flow."""

    def test_record_request_and_surface_in_active_claim(self, manager):
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="holder",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="long run",
        )
        request = manager.record_release_request(
            "sbc1", requested_by="other-agent", reason="need the board"
        )
        assert request.id is not None
        claim = manager.get_active_claim("sbc1")
        assert len(claim.pending_requests) == 1
        assert claim.pending_requests[0].requested_by == "other-agent"

    def test_request_with_no_active_claim_raises(self, manager):
        manager.create_sbc(name="sbc1")
        with pytest.raises(ClaimNotFoundError):
            manager.record_release_request("sbc1", "me", "why not")


class TestDeleteSBCGatedByClaim:
    """Remove-SBC must refuse while a claim is active (spec gating matrix)."""

    def test_delete_sbc_refuses_while_claimed(self, manager):
        sbc = manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        with pytest.raises(ClaimConflict):
            manager.delete_sbc(sbc.id)
        # SBC still exists
        assert manager.get_sbc(sbc.id) is not None

    def test_delete_sbc_force_succeeds_while_claimed(self, manager):
        sbc = manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        assert manager.delete_sbc(sbc.id, force=True) is True
        assert manager.get_sbc(sbc.id) is None

    def test_delete_sbc_after_release_succeeds(self, manager):
        sbc = manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="a",
            session_id="s1",
            session_kind="cli",
            duration_seconds=60,
            reason="r",
        )
        manager.release_claim("sbc1", "s1")
        assert manager.delete_sbc(sbc.id) is True


class TestDeadSessionRelease:
    """Session liveness check for mcp-stdio claims."""

    def test_dead_pid_releases_claim(self, manager):
        """Claim by a dead PID gets released after grace expires."""
        from unittest.mock import patch

        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="dead-agent",
            session_id="mcp-stdio:99999-1700000000",
            session_kind="mcp-stdio",
            duration_seconds=1,
            reason="test",
        )
        time.sleep(1.2)

        with patch.object(type(manager), "_is_pid_alive", return_value=False):
            count = manager.release_dead_sessions(grace_seconds=0)

        assert count == 1
        history = manager.list_claim_history("sbc1")
        assert history[0].release_reason == ReleaseReason.SESSION_LOST

    def test_alive_pid_not_released(self, manager):
        """Claim by a living PID is left alone."""
        from unittest.mock import patch

        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="live-agent",
            session_id="mcp-stdio:1-1700000000",
            session_kind="mcp-stdio",
            duration_seconds=1,
            reason="test",
        )
        time.sleep(1.2)

        with patch.object(type(manager), "_is_pid_alive", return_value=True):
            count = manager.release_dead_sessions(grace_seconds=0)

        assert count == 0

    def test_cli_session_skipped(self, manager):
        """CLI claims are not subject to PID liveness checks."""
        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="cli-user",
            session_id="cli-john@host",
            session_kind="cli",
            duration_seconds=1,
            reason="test",
        )
        time.sleep(1.2)
        count = manager.release_dead_sessions(grace_seconds=0)
        assert count == 0

    def test_grace_period_respected(self, manager):
        """Dead PID within grace period is not released."""
        from unittest.mock import patch

        manager.create_sbc(name="sbc1")
        manager.claim_sbc(
            sbc_name="sbc1",
            agent_name="dead-agent",
            session_id="mcp-stdio:99999-1700000000",
            session_kind="mcp-stdio",
            duration_seconds=600,
            reason="test",
        )
        # Claim still within its deadline — grace check won't trigger
        with patch.object(type(manager), "_is_pid_alive", return_value=False):
            count = manager.release_dead_sessions(grace_seconds=60)

        assert count == 0
