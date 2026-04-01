"""Unit tests for MCP server tools and resources."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from labctl.core.database import get_database
from labctl.core.manager import ResourceManager
from labctl.core.models import AddressType, PlugType, PortType


@pytest.fixture
def manager(tmp_path):
    """Create a test manager with temporary database."""
    db = get_database(tmp_path / "test.db")
    return ResourceManager(db)


@pytest.fixture
def populated_manager(manager):
    """Manager with SBCs, ports, network, and power plug data."""
    sbc1 = manager.create_sbc(
        name="test-sbc-1", project="ProjectA", description="First test SBC"
    )
    sbc2 = manager.create_sbc(
        name="test-sbc-2", project="ProjectB", ssh_user="admin"
    )

    device = manager.create_serial_device(
        name="port-1", usb_path="1-10.1.3", vendor="FTDI", model="FT232R"
    )

    manager.assign_serial_port(
        sbc_id=sbc1.id,
        port_type=PortType.CONSOLE,
        device_path="/dev/lab/port-1",
        tcp_port=4000,
        alias="sbc1-console",
        serial_device_id=device.id,
    )

    manager.set_network_address(
        sbc_id=sbc1.id,
        address_type=AddressType.ETHERNET,
        ip_address="192.168.1.100",
    )

    manager.assign_power_plug(
        sbc_id=sbc1.id,
        plug_type=PlugType.TASMOTA,
        address="192.168.1.200",
    )

    sdwire = manager.create_sdwire_device(
        name="sdwire-1",
        serial_number="bdgrd_sdwirec_001",
        device_type="sdwirec",
    )
    manager.assign_sdwire(sbc1.id, sdwire.id)

    return manager


@pytest.fixture
def mock_manager(populated_manager, tmp_path):
    """Patch _get_manager in mcp_server to use the test manager."""
    with patch("labctl.mcp_server._get_manager", return_value=populated_manager):
        yield populated_manager


# ---------------------------------------------------------------------------
# Resource tests
# ---------------------------------------------------------------------------


class TestMcpResources:
    """Tests for MCP resource handlers."""

    def test_list_sbcs(self, mock_manager):
        from labctl.mcp_server import list_sbcs

        result = json.loads(list_sbcs())
        assert len(result) == 2
        names = {s["name"] for s in result}
        assert names == {"test-sbc-1", "test-sbc-2"}

    def test_list_sbcs_includes_details(self, mock_manager):
        from labctl.mcp_server import list_sbcs

        result = json.loads(list_sbcs())
        sbc1 = next(s for s in result if s["name"] == "test-sbc-1")
        assert sbc1["project"] == "ProjectA"
        assert sbc1["primary_ip"] == "192.168.1.100"
        assert sbc1["power_plug"]["type"] == "tasmota"
        assert sbc1["power_plug"]["address"] == "192.168.1.200"
        assert len(sbc1["serial_ports"]) == 1
        assert sbc1["serial_ports"][0]["alias"] == "sbc1-console"
        assert sbc1["serial_ports"][0]["serial_device"] == "port-1"

    def test_get_sbc_details(self, mock_manager):
        from labctl.mcp_server import get_sbc_details

        result = json.loads(get_sbc_details("test-sbc-1"))
        assert result["name"] == "test-sbc-1"
        assert result["description"] == "First test SBC"
        assert result["ssh_user"] == "root"
        assert len(result["serial_ports"]) == 1
        assert len(result["network_addresses"]) == 1

    def test_get_sbc_details_not_found(self, mock_manager):
        from labctl.mcp_server import get_sbc_details

        result = json.loads(get_sbc_details("nonexistent"))
        assert "error" in result

    def test_list_serial_devices(self, mock_manager):
        from labctl.mcp_server import list_serial_devices

        result = json.loads(list_serial_devices())
        assert len(result) == 1
        assert result[0]["name"] == "port-1"
        assert result[0]["usb_path"] == "1-10.1.3"
        assert result[0]["vendor"] == "FTDI"
        assert result[0]["model"] == "FT232R"

    def test_list_ports(self, mock_manager):
        from labctl.mcp_server import list_ports

        result = json.loads(list_ports())
        assert len(result) == 1
        assert result[0]["sbc"] == "test-sbc-1"
        assert result[0]["alias"] == "sbc1-console"
        assert result[0]["device"] == "/dev/lab/port-1"
        assert result[0]["tcp_port"] == 4000
        assert result[0]["serial_device"] == "port-1"

    def test_get_sbc_details_no_power_plug(self, mock_manager):
        from labctl.mcp_server import get_sbc_details

        result = json.loads(get_sbc_details("test-sbc-2"))
        assert result["name"] == "test-sbc-2"
        assert result["ssh_user"] == "admin"
        assert "power_plug" not in result


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------


class TestMcpTools:
    """Tests for MCP tool handlers."""

    def test_add_sbc(self, mock_manager):
        from labctl.mcp_server import add_sbc

        result = add_sbc(name="new-sbc", project="TestProj", description="A new board")
        data = json.loads(result)
        assert data["created"]["name"] == "new-sbc"
        assert data["created"]["project"] == "TestProj"

    def test_add_sbc_duplicate(self, mock_manager):
        from labctl.mcp_server import add_sbc

        result = add_sbc(name="test-sbc-1")
        assert "Error" in result

    def test_remove_sbc(self, mock_manager):
        from labctl.mcp_server import remove_sbc

        result = remove_sbc(name="test-sbc-2")
        assert "Removed" in result

    def test_remove_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import remove_sbc

        result = remove_sbc(name="nope")
        assert "not found" in result

    def test_update_sbc(self, mock_manager):
        from labctl.mcp_server import update_sbc

        result = update_sbc(name="test-sbc-1", project="NewProject")
        data = json.loads(result)
        assert data["updated"]["project"] == "NewProject"

    def test_update_sbc_rename(self, mock_manager):
        from labctl.mcp_server import update_sbc

        result = update_sbc(name="test-sbc-2", rename="renamed-sbc")
        data = json.loads(result)
        assert data["updated"]["name"] == "renamed-sbc"

    def test_update_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import update_sbc

        result = update_sbc(name="nope", project="X")
        assert "not found" in result

    def test_assign_serial_port(self, mock_manager):
        from labctl.mcp_server import assign_serial_port

        result = assign_serial_port(
            sbc_name="test-sbc-2",
            port_type="console",
            device="/dev/lab/new-port",
            alias="sbc2-console",
        )
        assert "Assigned" in result
        assert "sbc2-console" not in result or "tcp:" in result

    def test_assign_serial_port_bad_sbc(self, mock_manager):
        from labctl.mcp_server import assign_serial_port

        result = assign_serial_port(
            sbc_name="nope", port_type="console", device="/dev/lab/x"
        )
        assert "not found" in result

    def test_assign_power_plug(self, mock_manager):
        from labctl.mcp_server import assign_power_plug

        result = assign_power_plug(
            sbc_name="test-sbc-2",
            plug_type="shelly",
            address="192.168.1.50",
        )
        assert "Assigned" in result
        assert "shelly" in result

    def test_set_network_address(self, mock_manager):
        from labctl.mcp_server import set_network_address

        result = set_network_address(
            sbc_name="test-sbc-2",
            address_type="ethernet",
            ip_address="10.0.0.50",
        )
        assert "Set" in result
        assert "10.0.0.50" in result

    def test_set_network_address_bad_sbc(self, mock_manager):
        from labctl.mcp_server import set_network_address

        result = set_network_address(
            sbc_name="nope", address_type="ethernet", ip_address="1.1.1.1"
        )
        assert "not found" in result


class TestMcpPowerTools:
    """Tests for power control tools (using mock controller)."""

    def test_power_on_no_plug(self, mock_manager):
        from labctl.mcp_server import power_on

        result = power_on(sbc_name="test-sbc-2")
        assert "No power plug" in result

    def test_power_off_no_plug(self, mock_manager):
        from labctl.mcp_server import power_off

        result = power_off(sbc_name="test-sbc-2")
        assert "No power plug" in result

    def test_power_cycle_no_plug(self, mock_manager):
        from labctl.mcp_server import power_cycle

        result = power_cycle(sbc_name="test-sbc-2")
        assert "No power plug" in result

    def test_power_on_not_found(self, mock_manager):
        from labctl.mcp_server import power_on

        result = power_on(sbc_name="nope")
        assert "not found" in result

    def test_power_on_with_plug(self, mock_manager):
        """Test power_on with a real plug assignment (mocked controller)."""
        from unittest.mock import MagicMock

        from labctl.mcp_server import power_on

        mock_ctrl = MagicMock()
        mock_ctrl.power_on.return_value = True

        with patch("labctl.power.base.PowerController.from_plug") as mock_from_plug:
            mock_from_plug.return_value = mock_ctrl
            result = power_on(sbc_name="test-sbc-1")

        assert "Power ON" in result
        mock_ctrl.power_on.assert_called_once()

    def test_power_off_with_plug(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import power_off

        mock_ctrl = MagicMock()
        mock_ctrl.power_off.return_value = True

        with patch("labctl.power.base.PowerController.from_plug") as mock_from_plug:
            mock_from_plug.return_value = mock_ctrl
            result = power_off(sbc_name="test-sbc-1")

        assert "Power OFF" in result

    def test_power_cycle_with_plug(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import power_cycle

        mock_ctrl = MagicMock()
        mock_ctrl.power_cycle.return_value = True

        with patch("labctl.power.base.PowerController.from_plug") as mock_from_plug:
            mock_from_plug.return_value = mock_ctrl
            result = power_cycle(sbc_name="test-sbc-1", delay=1.0)

        assert "Power cycled" in result
        mock_ctrl.power_cycle.assert_called_once_with(1.0)

    def test_power_on_failure(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import power_on

        mock_ctrl = MagicMock()
        mock_ctrl.power_on.return_value = False

        with patch("labctl.power.base.PowerController.from_plug") as mock_from_plug:
            mock_from_plug.return_value = mock_ctrl
            result = power_on(sbc_name="test-sbc-1")

        assert "Failed" in result

    def test_power_on_runtime_error(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import power_on

        mock_ctrl = MagicMock()
        mock_ctrl.power_on.side_effect = RuntimeError("device unreachable")

        with patch("labctl.power.base.PowerController.from_plug") as mock_from_plug:
            mock_from_plug.return_value = mock_ctrl
            result = power_on(sbc_name="test-sbc-1")

        assert "Error" in result
        assert "device unreachable" in result


class TestMcpSDWireResource:
    """Tests for SDWire MCP resource."""

    def test_list_sdwire_devices(self, mock_manager):
        from labctl.mcp_server import list_sdwire_devices

        result = json.loads(list_sdwire_devices())
        assert len(result) == 1
        assert result[0]["name"] == "sdwire-1"
        assert result[0]["serial_number"] == "bdgrd_sdwirec_001"
        assert result[0]["device_type"] == "sdwirec"
        assert result[0]["assigned_to"] == "test-sbc-1"

    def test_sbc_details_includes_sdwire(self, mock_manager):
        from labctl.mcp_server import get_sbc_details

        result = json.loads(get_sbc_details("test-sbc-1"))
        assert "sdwire" in result
        assert result["sdwire"]["name"] == "sdwire-1"
        assert result["sdwire"]["serial_number"] == "bdgrd_sdwirec_001"

    def test_sbc_without_sdwire(self, mock_manager):
        from labctl.mcp_server import get_sbc_details

        result = json.loads(get_sbc_details("test-sbc-2"))
        assert "sdwire" not in result

    def test_list_sbcs_includes_sdwire(self, mock_manager):
        from labctl.mcp_server import list_sbcs

        result = json.loads(list_sbcs())
        sbc1 = next(s for s in result if s["name"] == "test-sbc-1")
        assert "sdwire" in sbc1
        assert sbc1["sdwire"]["name"] == "sdwire-1"


class TestMcpSDWireTools:
    """Tests for SDWire MCP tools."""

    def test_sdwire_to_dut_no_sdwire(self, mock_manager):
        from labctl.mcp_server import sdwire_to_dut

        result = sdwire_to_dut(sbc_name="test-sbc-2")
        assert "No SDWire" in result

    def test_sdwire_to_host_no_sdwire(self, mock_manager):
        from labctl.mcp_server import sdwire_to_host

        result = sdwire_to_host(sbc_name="test-sbc-2")
        assert "No SDWire" in result

    def test_sdwire_to_dut_not_found(self, mock_manager):
        from labctl.mcp_server import sdwire_to_dut

        result = sdwire_to_dut(sbc_name="nope")
        assert "not found" in result

    def test_sdwire_to_host_not_found(self, mock_manager):
        from labctl.mcp_server import sdwire_to_host

        result = sdwire_to_host(sbc_name="nope")
        assert "not found" in result

    def test_sdwire_to_dut_with_device(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_to_dut

        mock_ctrl_instance = MagicMock()

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_to_dut(sbc_name="test-sbc-1")

        assert "switched to DUT" in result
        mock_ctrl_instance.switch_to_dut.assert_called_once()

    def test_sdwire_to_host_with_device(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_to_host

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.get_block_device.return_value = "/dev/sdb"

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_to_host(sbc_name="test-sbc-1")

        assert "switched to host" in result
        assert "/dev/sdb" in result
        mock_ctrl_instance.switch_to_host.assert_called_once()

    def test_sdwire_to_dut_runtime_error(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_to_dut

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.switch_to_dut.side_effect = RuntimeError("device not found")

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_to_dut(sbc_name="test-sbc-1")

        assert "Error" in result
        assert "device not found" in result

    def test_sdwire_update_no_sdwire(self, mock_manager):
        from labctl.mcp_server import sdwire_update

        result = sdwire_update(
            sbc_name="test-sbc-2", partition=1, copies=["a.bin:b.bin"]
        )
        assert "No SDWire" in result

    def test_sdwire_update_not_found(self, mock_manager):
        from labctl.mcp_server import sdwire_update

        result = sdwire_update(
            sbc_name="nope", partition=1, copies=["a.bin:b.bin"]
        )
        assert "not found" in result

    def test_sdwire_update_bad_copy_format(self, mock_manager):
        from labctl.mcp_server import sdwire_update

        result = sdwire_update(
            sbc_name="test-sbc-1", partition=1, copies=["no-colon"]
        )
        assert "Invalid copy format" in result

    def test_sdwire_update_success(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_update

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.update_files.return_value = ["kernel.img"]

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=["local.bin:kernel.img"],
            )

        assert "Updated 1 file(s)" in result
        assert "kernel.img" in result
        mock_ctrl_instance.switch_to_host.assert_called_once()
        mock_ctrl_instance.update_files.assert_called_once_with(
            1, [("local.bin", "kernel.img")]
        )
        mock_ctrl_instance.switch_to_dut.assert_called_once()

    def test_sdwire_update_with_reboot(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_update

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.update_files.return_value = ["kernel.img"]

        mock_power = MagicMock()
        mock_power.power_cycle.return_value = True

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            with patch("labctl.power.base.PowerController.from_plug", return_value=mock_power):
                result = sdwire_update(
                    sbc_name="test-sbc-1",
                    partition=1,
                    copies=["local.bin:kernel.img"],
                    reboot=True,
                )

        assert "Power cycled" in result
        mock_power.power_cycle.assert_called_once()

    def test_sdwire_update_runtime_error(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_update

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.update_files.side_effect = RuntimeError("mount failed")

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=["a.bin:b.bin"],
            )

        assert "Error" in result
        assert "mount failed" in result
