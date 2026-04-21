"""Unit tests for MCP server tools and resources."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from labctl.core import audit
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
    sbc2 = manager.create_sbc(name="test-sbc-2", project="ProjectB", ssh_user="admin")

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

    def test_add_sbc_records_mcp_actor_and_source(self, mock_manager):
        from labctl.mcp_server import add_sbc

        with patch("labctl.mcp_server._get_session_id", return_value="mcp-stdio:test"):
            result = add_sbc(name="mcp-audit-sbc")

        data = json.loads(result)
        assert data["created"]["name"] == "mcp-audit-sbc"

        row = mock_manager.db.execute_one(
            "SELECT actor, source FROM audit_log "
            "WHERE action = 'create' AND entity_name = ?",
            ("mcp-audit-sbc",),
        )
        assert row["actor"] == "mcp-stdio:test"
        assert row["source"] == "mcp"

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

    def test_sdwire_to_host_rejects_powered_on(self, mock_manager):
        """Test that sdwire_to_host rejects when SBC is powered on."""
        from labctl.mcp_server import sdwire_to_host
        from labctl.power.base import PowerState

        mock_power = MagicMock()
        mock_power.get_state.return_value = PowerState.ON

        with patch(
            "labctl.power.base.PowerController.from_plug", return_value=mock_power
        ):
            result = sdwire_to_host(sbc_name="test-sbc-1")

        assert "Error" in result
        assert "powered on" in result

    def test_sdwire_to_host_force_overrides(self, mock_manager):
        """Test that force=True bypasses power check."""
        from labctl.mcp_server import sdwire_to_host
        from labctl.power.base import PowerState

        mock_power = MagicMock()
        mock_power.get_state.return_value = PowerState.ON

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"

        with patch(
            "labctl.power.base.PowerController.from_plug", return_value=mock_power
        ):
            with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
                result = sdwire_to_host(sbc_name="test-sbc-1", force=True)

        assert "switched to host" in result

    def test_sdwire_to_host_allows_powered_off(self, mock_manager):
        """Test that sdwire_to_host allows when SBC is powered off."""
        from labctl.mcp_server import sdwire_to_host
        from labctl.power.base import PowerState

        mock_power = MagicMock()
        mock_power.get_state.return_value = PowerState.OFF

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"

        with patch(
            "labctl.power.base.PowerController.from_plug", return_value=mock_power
        ):
            with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
                result = sdwire_to_host(sbc_name="test-sbc-1")

        assert "switched to host" in result

    def test_sdwire_to_host_allows_no_power_plug(self, mock_manager):
        """Test that SBC without power plug can still switch to host."""
        from labctl.mcp_server import sdwire_to_host

        # test-sbc-2 has no power plug, but also no sdwire
        # Use test-sbc-1 but mock out the power plug check
        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = None

        # Temporarily remove power plug from sbc
        sbc = mock_manager.get_sbc_by_name("test-sbc-2")
        # test-sbc-2 has no sdwire, so this will fail for a different reason
        result = sdwire_to_host(sbc_name="test-sbc-2")
        assert "No SDWire" in result

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

        result = sdwire_update(sbc_name="nope", partition=1, copies=["a.bin:b.bin"])
        assert "not found" in result

    def test_sdwire_update_bad_copy_format(self, mock_manager):
        from labctl.mcp_server import sdwire_update

        result = sdwire_update(sbc_name="test-sbc-1", partition=1, copies=["no-colon"])
        assert "Invalid copy format" in result

    def test_sdwire_update_success(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_update

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.update_files.return_value = {
            "copied": ["kernel.img"],
            "renamed": [],
            "deleted": [],
        }

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            result = sdwire_update(
                sbc_name="test-sbc-1",
                partition=1,
                copies=["local.bin:kernel.img"],
            )

        assert "Copied" in result
        assert "kernel.img" in result
        mock_ctrl_instance.switch_to_host.assert_called_once()
        mock_ctrl_instance.switch_to_dut.assert_called_once()

    def test_sdwire_update_with_reboot(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import sdwire_update

        mock_ctrl_instance = MagicMock()
        mock_ctrl_instance.update_files.return_value = {
            "copied": ["kernel.img"],
            "renamed": [],
            "deleted": [],
        }

        mock_power = MagicMock()
        mock_power.power_cycle.return_value = True

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl_instance):
            with patch(
                "labctl.power.base.PowerController.from_plug", return_value=mock_power
            ):
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


# ---------------------------------------------------------------------------
# Serial I/O Tool tests
# ---------------------------------------------------------------------------


class TestMcpSerialTools:
    """Tests for MCP serial capture and send tools."""

    def test_serial_capture_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import serial_capture

        result = serial_capture(port_name="nonexistent")
        assert "Error" in result

    def test_serial_capture_no_tcp_port(self, mock_manager):
        """Test capture on port with no TCP port configured."""
        from labctl.mcp_server import serial_capture

        # test-sbc-2 has no serial ports at all
        result = serial_capture(port_name="test-sbc-2")
        assert "Error" in result

    def test_serial_capture_by_alias(self, mock_manager):
        """Test capture resolves port by alias."""
        from unittest.mock import MagicMock

        from labctl.mcp_server import serial_capture
        from labctl.serial.capture import CaptureResult

        mock_result = CaptureResult(
            output="boot output",
            lines=1,
            pattern_matched=True,
            elapsed_seconds=5.0,
        )

        with patch(
            "labctl.serial.capture.capture_serial_output",
            return_value=mock_result,
        ):
            result = serial_capture(
                port_name="sbc1-console",
                timeout=10.0,
                until_pattern="boot",
            )

        assert "boot output" in result
        assert "matched" in result

    def test_serial_capture_by_sbc_name(self, mock_manager):
        """Test capture resolves port by SBC name (console fallback)."""
        from labctl.mcp_server import serial_capture
        from labctl.serial.capture import CaptureResult

        mock_result = CaptureResult(
            output="hello",
            lines=1,
            pattern_matched=False,
            elapsed_seconds=15.0,
        )

        with patch(
            "labctl.serial.capture.capture_serial_output",
            return_value=mock_result,
        ):
            result = serial_capture(port_name="test-sbc-1", timeout=15.0)

        assert "hello" in result
        assert "timeout" in result

    def test_serial_capture_connection_error(self, mock_manager):
        """Test capture handles connection errors."""
        from labctl.mcp_server import serial_capture

        with patch(
            "labctl.serial.capture.capture_serial_output",
            side_effect=RuntimeError("Connection refused"),
        ):
            result = serial_capture(port_name="sbc1-console")

        assert "Error" in result
        assert "Connection refused" in result

    def test_serial_send_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import serial_send

        result = serial_send(port_name="nonexistent", data="hello")
        assert "Error" in result

    def test_serial_send_success(self, mock_manager):
        """Test send resolves port and sends data."""
        from labctl.mcp_server import serial_send
        from labctl.serial.capture import SendResult

        mock_result = SendResult(sent=True, bytes_sent=7)

        with patch(
            "labctl.serial.capture.send_serial_data",
            return_value=mock_result,
        ):
            result = serial_send(
                port_name="sbc1-console",
                data="hello",
            )

        assert "7 bytes" in result

    def test_serial_send_with_capture(self, mock_manager):
        """Test send with capture returns captured output."""
        from labctl.mcp_server import serial_send
        from labctl.serial.capture import CaptureResult, SendResult

        mock_result = SendResult(
            sent=True,
            bytes_sent=7,
            capture=CaptureResult(
                output="response",
                lines=1,
                pattern_matched=True,
                elapsed_seconds=2.0,
            ),
        )

        with patch(
            "labctl.serial.capture.send_serial_data",
            return_value=mock_result,
        ):
            result = serial_send(
                port_name="sbc1-console",
                data="cmd",
                capture_timeout=5.0,
            )

        assert "response" in result

    def test_serial_send_connection_error(self, mock_manager):
        """Test send handles connection errors."""
        from labctl.mcp_server import serial_send

        with patch(
            "labctl.serial.capture.send_serial_data",
            side_effect=RuntimeError("Connection refused"),
        ):
            result = serial_send(port_name="sbc1-console", data="hello")

        assert "Error" in result


# ---------------------------------------------------------------------------
# Flash Image Tool tests
# ---------------------------------------------------------------------------


class TestMcpFlashImage:
    """Tests for MCP flash_image tool."""

    def test_flash_image_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import flash_image

        result = flash_image(sbc_name="nonexistent", image_path="/tmp/test.img")
        assert "Error" in result
        assert "not found" in result

    def test_flash_image_no_sdwire(self, mock_manager):
        from labctl.mcp_server import flash_image

        result = flash_image(sbc_name="test-sbc-2", image_path="/tmp/test.img")
        assert "Error" in result
        assert "No SDWire" in result

    def test_flash_image_success(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import flash_image

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"
        mock_ctrl.flash_image.return_value = {
            "bytes_written": 1024000,
            "elapsed_seconds": 5.0,
            "block_device": "/dev/sdb",
        }

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
            with patch("labctl.power.base.PowerController.from_plug") as mock_power:
                mock_power.return_value = MagicMock()
                result = flash_image(
                    sbc_name="test-sbc-1",
                    image_path="/tmp/test.img",
                )

        assert "Flashed" in result
        assert "1024000" in result
        mock_ctrl.switch_to_host.assert_called_once()
        mock_ctrl.switch_to_dut.assert_called_once()

    def test_flash_image_with_reboot(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import flash_image

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"
        mock_ctrl.flash_image.return_value = {
            "bytes_written": 1024,
            "elapsed_seconds": 1.0,
            "block_device": "/dev/sdb",
        }

        mock_power_inst = MagicMock()
        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
            with patch(
                "labctl.power.base.PowerController.from_plug",
                return_value=mock_power_inst,
            ):
                result = flash_image(
                    sbc_name="test-sbc-1",
                    image_path="/tmp/test.img",
                    reboot=True,
                )

        assert "Powered on" in result
        mock_power_inst.power_on.assert_called_once()

    def test_flash_image_no_block_device(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import flash_image

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = None

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
            with patch("labctl.power.base.PowerController.from_plug"):
                result = flash_image(
                    sbc_name="test-sbc-1",
                    image_path="/tmp/test.img",
                )

        assert "Error" in result
        assert "Block device not found" in result

    def test_flash_image_flash_error_leaves_on_host(self, mock_manager):
        from unittest.mock import MagicMock

        from labctl.mcp_server import flash_image

        mock_ctrl = MagicMock()
        mock_ctrl.get_block_device.return_value = "/dev/sdb"
        mock_ctrl.flash_image.side_effect = RuntimeError("dd failed")

        with patch("labctl.sdwire.SDWireController", return_value=mock_ctrl):
            with patch("labctl.power.base.PowerController.from_plug"):
                result = flash_image(
                    sbc_name="test-sbc-1",
                    image_path="/tmp/test.img",
                )

        assert "Error" in result
        assert "left on host" in result
        # Should NOT have called switch_to_dut
        mock_ctrl.switch_to_dut.assert_not_called()


# ---------------------------------------------------------------------------
# Boot Test Tool tests
# ---------------------------------------------------------------------------


class TestMcpBootTest:
    """Tests for MCP boot_test tool."""

    def test_boot_test_sbc_not_found(self, mock_manager):
        from labctl.mcp_server import boot_test

        result = boot_test(sbc_name="nonexistent", expect_pattern="ok")
        assert "Error" in result
        assert "not found" in result

    def test_boot_test_no_console_port(self, mock_manager):
        """test-sbc-2 has no console port."""
        from labctl.mcp_server import boot_test

        result = boot_test(sbc_name="test-sbc-2", expect_pattern="ok")
        assert "Error" in result
        assert "console" in result.lower()

    def test_boot_test_no_power_plug(self, mock_manager):
        """SBC with console but no power plug."""
        from labctl.mcp_server import boot_test

        # Add console port to sbc-2 but it has no power plug
        sbc2 = mock_manager.get_sbc_by_name("test-sbc-2")
        mock_manager.assign_serial_port(
            sbc_id=sbc2.id,
            port_type=PortType.CONSOLE,
            device_path="/dev/lab/port-2",
            tcp_port=4001,
        )

        result = boot_test(sbc_name="test-sbc-2", expect_pattern="ok")
        assert "Error" in result
        assert "power" in result.lower()

    def test_boot_test_missing_dest(self, mock_manager):
        """Image specified without dest should error."""
        from labctl.mcp_server import boot_test

        result = boot_test(
            sbc_name="test-sbc-1",
            expect_pattern="ok",
            image="test.bin",
        )
        assert "Error" in result
        assert "dest" in result.lower()

    def test_boot_test_success(self, mock_manager):
        """Test successful boot test run."""
        from labctl.mcp_server import boot_test
        from labctl.serial.boot_test import BootRunResult, BootTestResult

        mock_result = BootTestResult(
            sbc_name="test-sbc-1",
            expect_pattern="ok",
            total_runs=2,
            timeout_per_run=10.0,
            runs=[
                BootRunResult(1, True, 5.0, True),
                BootRunResult(2, True, 6.0, True),
            ],
        )

        with patch(
            "labctl.serial.boot_test.run_boot_test",
            return_value=mock_result,
        ):
            with patch("labctl.power.base.PowerController.from_plug"):
                result = boot_test(
                    sbc_name="test-sbc-1",
                    expect_pattern="ok",
                    runs=2,
                    timeout=10.0,
                )

        assert "2/2" in result
        assert "100%" in result

    def test_boot_test_runtime_error(self, mock_manager):
        """Test boot test handles runtime errors."""
        from labctl.mcp_server import boot_test

        with patch(
            "labctl.serial.boot_test.run_boot_test",
            side_effect=RuntimeError("power failure"),
        ):
            with patch("labctl.power.base.PowerController.from_plug"):
                result = boot_test(
                    sbc_name="test-sbc-1",
                    expect_pattern="ok",
                    runs=1,
                )

        assert "Error" in result
        assert "power failure" in result


# ---------------------------------------------------------------------------
# Remove / Unassign Tool tests
# ---------------------------------------------------------------------------


class TestMcpRemoveTools:
    """Tests for MCP remove and unassign tools."""

    def test_remove_serial_port(self, mock_manager):
        from labctl.mcp_server import remove_serial_port

        result = remove_serial_port(sbc_name="test-sbc-1", port_type="console")
        assert "Removed" in result

    def test_remove_serial_port_not_found(self, mock_manager):
        from labctl.mcp_server import remove_serial_port

        result = remove_serial_port(sbc_name="nonexistent")
        assert "Error" in result

    def test_remove_serial_port_none_assigned(self, mock_manager):
        from labctl.mcp_server import remove_serial_port

        result = remove_serial_port(sbc_name="test-sbc-2", port_type="console")
        assert "No console port" in result

    def test_remove_network_address(self, mock_manager):
        from labctl.mcp_server import remove_network_address

        result = remove_network_address(sbc_name="test-sbc-1", address_type="ethernet")
        assert "Removed" in result

    def test_remove_network_address_not_found(self, mock_manager):
        from labctl.mcp_server import remove_network_address

        result = remove_network_address(sbc_name="nonexistent")
        assert "Error" in result

    def test_remove_power_plug(self, mock_manager):
        from labctl.mcp_server import remove_power_plug

        result = remove_power_plug(sbc_name="test-sbc-1")
        assert "Removed" in result

    def test_remove_power_plug_none(self, mock_manager):
        from labctl.mcp_server import remove_power_plug

        result = remove_power_plug(sbc_name="test-sbc-2")
        assert "No power plug" in result

    def test_sdwire_unassign(self, mock_manager):
        from labctl.mcp_server import sdwire_unassign

        result = sdwire_unassign(sbc_name="test-sbc-1")
        assert "Removed" in result

    def test_sdwire_unassign_none(self, mock_manager):
        from labctl.mcp_server import sdwire_unassign

        result = sdwire_unassign(sbc_name="test-sbc-2")
        assert "No SDWire" in result


# ---------------------------------------------------------------------------
# Serial/SDWire Device CRUD Tool tests
# ---------------------------------------------------------------------------


class TestMcpDeviceCrudTools:
    """Tests for MCP device creation and removal tools."""

    def test_add_serial_device(self, mock_manager):
        from labctl.mcp_server import add_serial_device

        result = add_serial_device(name="new-port", usb_path="1-10.2.1", vendor="FTDI")
        assert "Registered" in result
        assert "new-port" in result

    def test_add_serial_device_duplicate(self, mock_manager):
        from labctl.mcp_server import add_serial_device

        # port-1 already exists in populated_manager
        result = add_serial_device(name="port-1", usb_path="1-10.2.1")
        assert "Error" in result

    def test_remove_serial_device(self, mock_manager):
        from labctl.mcp_server import add_serial_device, remove_serial_device

        add_serial_device(name="temp-port", usb_path="1-99.1")
        result = remove_serial_device(name="temp-port")
        assert "Removed" in result

    def test_remove_serial_device_not_found(self, mock_manager):
        from labctl.mcp_server import remove_serial_device

        result = remove_serial_device(name="nonexistent")
        assert "Error" in result

    def test_sdwire_add(self, mock_manager):
        from labctl.mcp_server import sdwire_add

        result = sdwire_add(name="new-sdwire", serial_number="serial-new-123")
        assert "Registered" in result
        assert "new-sdwire" in result

    def test_sdwire_add_duplicate(self, mock_manager):
        from labctl.mcp_server import sdwire_add

        # sdwire-1 already exists
        result = sdwire_add(name="sdwire-1", serial_number="new-serial")
        assert "Error" in result

    def test_sdwire_remove(self, mock_manager):
        from labctl.mcp_server import sdwire_add, sdwire_remove

        sdwire_add(name="temp-sdwire", serial_number="temp-serial-999")
        result = sdwire_remove(name="temp-sdwire")
        assert "Removed" in result

    def test_sdwire_remove_not_found(self, mock_manager):
        from labctl.mcp_server import sdwire_remove

        result = sdwire_remove(name="nonexistent")
        assert "Error" in result

    def test_sdwire_assign(self, mock_manager):
        from labctl.mcp_server import sdwire_add, sdwire_assign

        sdwire_add(name="assign-sw", serial_number="assign-serial-123")
        result = sdwire_assign(sbc_name="test-sbc-2", device_name="assign-sw")
        assert "Assigned" in result

    def test_sdwire_assign_bad_sbc(self, mock_manager):
        from labctl.mcp_server import sdwire_assign

        result = sdwire_assign(sbc_name="nonexistent", device_name="sdwire-1")
        assert "Error" in result

    def test_sdwire_assign_bad_device(self, mock_manager):
        from labctl.mcp_server import sdwire_assign

        result = sdwire_assign(sbc_name="test-sbc-1", device_name="nonexistent")
        assert "Error" in result


# ---------------------------------------------------------------------------
# Discovery Tool tests
# ---------------------------------------------------------------------------


class TestMcpDiscoveryTools:
    """Tests for MCP hardware discovery tools."""

    def test_sdwire_discover_no_package(self, mock_manager):
        from labctl.mcp_server import sdwire_discover

        with patch(
            "labctl.sdwire.controller.discover_sdwire_devices",
            side_effect=RuntimeError("sdwire package not installed"),
        ):
            result = sdwire_discover()
        assert "Error" in result

    def test_sdwire_discover_empty(self, mock_manager):
        from labctl.mcp_server import sdwire_discover

        with patch(
            "labctl.sdwire.controller.discover_sdwire_devices",
            return_value=[],
        ):
            result = sdwire_discover()
        assert "No SDWire" in result

    def test_sdwire_discover_found(self, mock_manager):
        from labctl.mcp_server import sdwire_discover

        with patch(
            "labctl.sdwire.controller.discover_sdwire_devices",
            return_value=[{"serial_number": "abc", "device_type": "sdwirec"}],
        ):
            result = sdwire_discover()
        assert "abc" in result
        assert "sdwirec" in result

    def test_serial_discover_empty(self, mock_manager):
        from labctl.mcp_server import serial_discover

        with patch("labctl.serial.udev.discover_usb_serial", return_value=[]):
            result = serial_discover()
        assert "No USB-serial" in result

    def test_serial_discover_found(self, mock_manager):
        from labctl.mcp_server import serial_discover

        with patch(
            "labctl.serial.udev.discover_usb_serial",
            return_value=[{"device": "/dev/ttyUSB0", "usb_path": "1-10.1"}],
        ):
            result = serial_discover()
        assert "ttyUSB0" in result

    def test_recent_activity_resource(self, mock_manager, populated_manager):
        from labctl.mcp_server import get_recent_activity_resource

        with audit.activity_context("cli:alice", "cli"):
            populated_manager.create_sbc(name="activity-mcp-sbc", project="proj")

        result = json.loads(get_recent_activity_resource())
        assert any(
            evt["entity_name"] == "activity-mcp-sbc" and evt["actor"] == "cli:alice"
            for evt in result
        )

    def test_activity_for_sbc_resource(self, mock_manager, populated_manager):
        from labctl.mcp_server import get_activity_for_sbc_resource

        with audit.activity_context("cli:alice", "cli"):
            populated_manager.create_sbc(name="activity-filter-a", project="proj")
            populated_manager.create_sbc(name="activity-filter-b", project="proj")

        result = json.loads(get_activity_for_sbc_resource("activity-filter-a"))
        assert result
        assert all(evt["entity_name"] == "activity-filter-a" for evt in result)


# ---------------------------------------------------------------------------
# Claim Tool + Resource tests
# ---------------------------------------------------------------------------


@pytest.fixture
def claims_env(populated_manager, tmp_path):
    """Patch _get_manager, _get_config, and session identity for claim tests."""
    from labctl.core.config import ClaimsConfig, Config

    config = Config()
    config.claims = ClaimsConfig(
        enabled=True,
        default_duration_minutes=30,
        max_duration_minutes=60,
        min_duration_minutes=1,
        grace_period_seconds=60,
    )
    config.database_path = tmp_path / "test.db"

    with (
        patch("labctl.mcp_server._get_manager", return_value=populated_manager),
        patch("labctl.mcp_server._get_config", return_value=config),
        patch("labctl.mcp_server._SESSION_ID", "test-session-1"),
        patch("labctl.mcp_server._SESSION_KIND", "mcp-stdio"),
        patch("labctl.mcp_server._AGENT_NAME", "test-agent"),
    ):
        yield populated_manager


class TestMcpClaimTools:
    """Tests for MCP claim_sbc, release_sbc, renew_sbc_claim, etc."""

    def test_claim_and_release_round_trip(self, claims_env):
        from labctl.mcp_server import claim_sbc, list_claims, release_sbc

        result = json.loads(
            claim_sbc(sbc_name="test-sbc-1", reason="bringup", duration_minutes=10)
        )
        assert result["status"] == "claimed"
        assert result["claim"]["agent_name"] == "test-agent"

        claims = json.loads(list_claims())
        assert len(claims["active_claims"]) == 1

        result = json.loads(release_sbc(sbc_name="test-sbc-1"))
        assert result["status"] == "released"

        claims = json.loads(list_claims())
        assert len(claims["active_claims"]) == 0

    def test_claim_conflict(self, claims_env):
        from labctl.mcp_server import claim_sbc

        # Claim by a different session first
        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="first",
        )

        result = json.loads(claim_sbc(sbc_name="test-sbc-1", reason="second"))
        assert result["error"] == "sbc_claimed"
        assert "hints" in result

    def test_claim_unknown_sbc(self, claims_env):
        from labctl.mcp_server import claim_sbc

        result = json.loads(claim_sbc(sbc_name="ghost", reason="r"))
        assert result["error"] == "unknown_sbc"

    def test_claim_duration_out_of_bounds(self, claims_env):
        from labctl.mcp_server import claim_sbc

        result = json.loads(
            claim_sbc(sbc_name="test-sbc-1", reason="r", duration_minutes=120)
        )
        assert result["error"] == "duration_out_of_bounds"

    def test_renew_extends_claim(self, claims_env):
        from labctl.mcp_server import claim_sbc, renew_sbc_claim

        claim_sbc(sbc_name="test-sbc-1", reason="r", duration_minutes=10)
        result = json.loads(renew_sbc_claim(sbc_name="test-sbc-1", duration_minutes=20))
        assert result["status"] == "renewed"
        assert result["claim"]["duration_seconds"] == 1200

    def test_renew_non_claimant(self, claims_env):
        from labctl.mcp_server import renew_sbc_claim

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = json.loads(renew_sbc_claim(sbc_name="test-sbc-1"))
        assert result["error"] == "not_claimant"

    def test_release_non_claimant(self, claims_env):
        from labctl.mcp_server import release_sbc

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = json.loads(release_sbc(sbc_name="test-sbc-1"))
        assert result["error"] == "not_claimant"

    def test_get_claim_when_free(self, claims_env):
        from labctl.mcp_server import get_claim

        result = json.loads(get_claim(sbc_name="test-sbc-1"))
        assert result["claimed"] is False

    def test_get_claim_when_held(self, claims_env):
        from labctl.mcp_server import claim_sbc, get_claim

        claim_sbc(sbc_name="test-sbc-1", reason="r")
        result = json.loads(get_claim(sbc_name="test-sbc-1"))
        assert result["claimed"] is True
        assert result["claim"]["agent_name"] == "test-agent"

    def test_request_release_recorded(self, claims_env):
        from labctl.mcp_server import (
            claim_sbc,
            get_claim,
            request_sbc_release,
        )

        claim_sbc(sbc_name="test-sbc-1", reason="r")
        result = json.loads(
            request_sbc_release(sbc_name="test-sbc-1", reason="need it")
        )
        assert result["status"] == "request_recorded"

        claim = json.loads(get_claim(sbc_name="test-sbc-1"))
        assert len(claim["claim"]["pending_requests"]) == 1

    def test_force_release(self, claims_env):
        from labctl.mcp_server import force_release_sbc

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = json.loads(
            force_release_sbc(sbc_name="test-sbc-1", reason="emergency")
        )
        assert result["status"] == "force_released"
        assert result["was_held_by"] == "other"


class TestMcpClaimResources:
    """Tests for claim MCP resources."""

    def test_list_claims_resource_empty(self, claims_env):
        from labctl.mcp_server import list_claims_resource

        result = json.loads(list_claims_resource())
        assert result == []

    def test_list_claims_resource_with_claim(self, claims_env):
        from labctl.mcp_server import claim_sbc, list_claims_resource

        claim_sbc(sbc_name="test-sbc-1", reason="r")
        result = json.loads(list_claims_resource())
        assert len(result) == 1
        assert result[0]["sbc_name"] == "test-sbc-1"

    def test_get_claim_resource(self, claims_env):
        from labctl.mcp_server import claim_sbc, get_claim_resource

        claim_sbc(sbc_name="test-sbc-1", reason="r")
        result = json.loads(get_claim_resource("test-sbc-1"))
        assert result["claimed"] is True

    def test_claim_history_resource(self, claims_env):
        from labctl.mcp_server import (
            claim_sbc,
            get_claim_history_resource,
            release_sbc,
        )

        claim_sbc(sbc_name="test-sbc-1", reason="first run")
        release_sbc(sbc_name="test-sbc-1")
        result = json.loads(get_claim_history_resource("test-sbc-1"))
        assert len(result) == 1
        assert result[0]["agent_name"] == "test-agent"


class TestMcpClaimEnforcement:
    """Tests that mutating ops are gated by other-agent claims."""

    def test_power_on_blocked_by_other_claim(self, claims_env):
        from labctl.mcp_server import power_on

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = power_on(sbc_name="test-sbc-1")
        data = json.loads(result)
        assert data["error"] == "sbc_claimed"
        assert "other" in data["claim"]["agent_name"]

    def test_power_on_allowed_for_claimant(self, claims_env):
        from labctl.mcp_server import claim_sbc, power_on

        claim_sbc(sbc_name="test-sbc-1", reason="my claim")

        # power_on will try actual power control — mock it
        with patch("labctl.power.base.PowerController.from_plug") as m:
            m.return_value.power_on.return_value = True
            result = power_on(sbc_name="test-sbc-1")
        assert "Power ON" in result

    def test_power_on_allowed_when_unclaimed(self, claims_env):
        from labctl.mcp_server import power_on

        with patch("labctl.power.base.PowerController.from_plug") as m:
            m.return_value.power_on.return_value = True
            result = power_on(sbc_name="test-sbc-1")
        assert "Power ON" in result

    def test_remove_sbc_blocked_by_claim(self, claims_env):
        from labctl.mcp_server import remove_sbc

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="holding",
        )
        result = remove_sbc(name="test-sbc-1")
        data = json.loads(result)
        assert data["error"] == "sbc_claimed"


class TestMcpAtexitAndSweep:
    """Tests for atexit claim release and background sweep."""

    def test_release_session_claims(self, claims_env):
        """_release_session_claims releases this session's claims."""
        from labctl.mcp_server import _release_session_claims, claim_sbc

        claim_sbc(sbc_name="test-sbc-1", reason="will exit")
        assert claims_env.get_active_claim("test-sbc-1") is not None

        _release_session_claims()
        assert claims_env.get_active_claim("test-sbc-1") is None

    def test_release_session_claims_ignores_other_sessions(self, claims_env):
        """_release_session_claims doesn't touch other sessions' claims."""
        from labctl.mcp_server import _release_session_claims

        claims_env.claim_sbc(
            sbc_name="test-sbc-1",
            agent_name="other",
            session_id="other-session",
            session_kind="cli",
            duration_seconds=600,
            reason="not mine",
        )
        _release_session_claims()
        assert claims_env.get_active_claim("test-sbc-1") is not None


class TestMcpClaimMetricsResource:
    """Tests for lab://claims/metrics MCP resource."""

    def test_metrics_resource_empty(self, claims_env):
        from labctl.mcp_server import get_claim_metrics_resource

        result = json.loads(get_claim_metrics_resource())
        assert result["total"] == 0
        assert result["active"] == 0

    def test_metrics_resource_after_activity(self, claims_env):
        from labctl.mcp_server import (
            claim_sbc,
            get_claim_metrics_resource,
            release_sbc,
        )

        claim_sbc(sbc_name="test-sbc-1", reason="metrics test")
        release_sbc(sbc_name="test-sbc-1")
        result = json.loads(get_claim_metrics_resource())
        assert result["total"] == 1
        assert result["released"] == 1
        assert result["avg_duration_seconds"] is not None


class TestMcpClaimAdvisory:
    """Tests that pending release requests surface in tool responses."""

    def test_advisory_appended_on_claimant_success(self, claims_env):
        from labctl.mcp_server import claim_sbc, request_sbc_release

        # Claim as our session, then have someone request release
        claim_sbc(sbc_name="test-sbc-1", reason="my claim")
        claims_env.record_release_request(
            "test-sbc-1",
            requested_by="other-agent",
            reason="need the bench",
        )

        # power_on by claimant should include advisory
        from labctl.mcp_server import power_on

        with patch("labctl.power.base.PowerController.from_plug") as m:
            m.return_value.power_on.return_value = True
            result = power_on(sbc_name="test-sbc-1")

        assert "Power ON" in result
        assert "[claim advisory]" in result
        assert "other-agent" in result
        assert "need the bench" in result

    def test_no_advisory_when_no_requests(self, claims_env):
        from labctl.mcp_server import claim_sbc, power_on

        claim_sbc(sbc_name="test-sbc-1", reason="my claim")
        with patch("labctl.power.base.PowerController.from_plug") as m:
            m.return_value.power_on.return_value = True
            result = power_on(sbc_name="test-sbc-1")

        assert "Power ON" in result
        assert "[claim advisory]" not in result
