"""Tests for actuator/binding manager CRUD and audit-log entries."""

import pytest

from labctl.core.database import Database
from labctl.core.manager import ResourceManager
from labctl.core.models import (
    ActuatorKind,
    ChannelState,
    DesiredState,
    DriverName,
    SamplePhase,
    ShapeMode,
)


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / "actuators.db")
    db.initialize()
    return ResourceManager(db)


class TestActuatorCrud:
    def test_create_and_get_actuator(self, manager):
        a = manager.create_actuator(
            "relay-1",
            DriverName.LCUS1_SERIAL,
            device_path="/dev/ttyUSB-relay-1",
        )
        assert a.id is not None
        assert a.name == "relay-1"
        assert a.driver is DriverName.LCUS1_SERIAL
        assert a.kind is ActuatorKind.RELAY  # default

        fetched = manager.get_actuator_by_name("relay-1")
        assert fetched is not None
        assert fetched.id == a.id

    def test_create_actuator_duplicate_name_fails(self, manager):
        manager.create_actuator("relay-1", DriverName.LCUS1_SERIAL)
        with pytest.raises(Exception):
            manager.create_actuator("relay-1", DriverName.NUMATO_ACM)

    def test_list_actuators_loads_channels_in_batch(self, manager):
        a1 = manager.create_actuator("a1", DriverName.LCUS1_SERIAL)
        a2 = manager.create_actuator("a2", DriverName.NUMATO_ACM)
        manager.add_actuator_channel(a1.id, 1)
        manager.add_actuator_channel(a2.id, 1)
        manager.add_actuator_channel(a2.id, 2)

        actuators = {a.name: a for a in manager.list_actuators()}
        assert len(actuators["a1"].channels) == 1
        assert len(actuators["a2"].channels) == 2
        assert [c.channel_index for c in actuators["a2"].channels] == [1, 2]

    def test_delete_actuator_cascades_channels(self, manager):
        a = manager.create_actuator("a", DriverName.LCUS1_SERIAL)
        manager.add_actuator_channel(a.id, 1)
        manager.add_actuator_channel(a.id, 2)

        assert manager.delete_actuator(a.id) is True
        assert manager.list_actuator_channels(a.id) == []

    def test_record_actuator_probe_stamps_fields(self, manager):
        a = manager.create_actuator("a", DriverName.LCUS1_SERIAL)
        manager.record_actuator_probe(a.id, "ok")

        fresh = manager.get_actuator(a.id)
        assert fresh is not None
        assert fresh.last_probe_result == "ok"
        assert fresh.last_probe_at is not None


class TestChannelOperations:
    def test_add_channel_persists(self, manager):
        a = manager.create_actuator("a", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(
            a.id, 1, label="recovery", default_state=ChannelState.OPEN
        )
        assert ch.id is not None
        assert ch.channel_index == 1
        assert ch.default_state is ChannelState.OPEN

    def test_update_channel_state_bumps_cycle_count(self, manager):
        a = manager.create_actuator("a", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(a.id, 1)

        manager.update_channel_state(ch.id, ChannelState.CLOSED)
        manager.update_channel_state(ch.id, ChannelState.OPEN)

        fresh = manager.get_actuator_channel(a.id, 1)
        assert fresh is not None
        assert fresh.last_state is ChannelState.OPEN
        assert fresh.cycle_count == 2

    def test_update_channel_state_no_bump(self, manager):
        a = manager.create_actuator("a", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(a.id, 1)

        manager.update_channel_state(ch.id, ChannelState.CLOSED, bump_cycle_count=False)

        fresh = manager.get_actuator_channel(a.id, 1)
        assert fresh is not None
        assert fresh.cycle_count == 0
        assert fresh.last_state is ChannelState.CLOSED


class TestBindingCrud:
    def _setup(self, manager):
        sbc = manager.create_sbc(name="jetson")
        a = manager.create_actuator("relay-1", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(a.id, 1)
        return sbc, a, ch

    def test_create_binding(self, manager):
        sbc, a, ch = self._setup(manager)
        b = manager.create_binding(
            sbc.id,
            "recovery_mode",
            ch.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
            sample_phase=SamplePhase.PRE_POWER,
        )
        assert b.id is not None
        assert b.purpose == "recovery_mode"
        assert b.shape_mode is ShapeMode.LATCH
        assert b.sample_phase is SamplePhase.PRE_POWER
        assert b.desired_state is DesiredState.RELEASED  # default
        assert b.sbc_name == "jetson"
        assert b.actuator_name == "relay-1"
        assert b.channel_index == 1

    def test_get_binding_by_target(self, manager):
        sbc, a, ch = self._setup(manager)
        manager.create_binding(
            sbc.id,
            "recovery_mode",
            ch.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
        )

        b = manager.get_binding_by_target(sbc.id, "recovery_mode")
        assert b is not None
        assert b.purpose == "recovery_mode"

        assert manager.get_binding_by_target(sbc.id, "boot_select") is None

    def test_list_bindings_filters_by_sbc(self, manager):
        sbc1, a, _ch = self._setup(manager)
        sbc2 = manager.create_sbc(name="pi")
        ch2 = manager.add_actuator_channel(a.id, 2)
        manager.create_binding(
            sbc1.id, "recovery_mode", _ch.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        manager.create_binding(
            sbc2.id, "power_button", ch2.id,
            shape_mode=ShapeMode.MOMENTARY, shape_active=ChannelState.CLOSED,
            momentary_pulse_ms=200,
        )

        all_b = manager.list_bindings()
        assert {b.sbc_name for b in all_b} == {"jetson", "pi"}
        only_jetson = manager.list_bindings(sbc_id=sbc1.id)
        assert len(only_jetson) == 1
        assert only_jetson[0].sbc_name == "jetson"

    def test_delete_binding(self, manager):
        sbc, _a, ch = self._setup(manager)
        b = manager.create_binding(
            sbc.id, "recovery_mode", ch.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        assert manager.delete_binding(b.id) is True
        assert manager.get_binding(b.id) is None

    def test_update_desired_state(self, manager):
        sbc, _a, ch = self._setup(manager)
        b = manager.create_binding(
            sbc.id, "recovery_mode", ch.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        manager.update_binding_desired_state(b.id, DesiredState.ASSERTED)
        fresh = manager.get_binding(b.id)
        assert fresh is not None
        assert fresh.desired_state is DesiredState.ASSERTED

    def test_audit_log_records_create_delete(self, manager):
        sbc, _a, ch = self._setup(manager)
        b = manager.create_binding(
            sbc.id, "recovery_mode", ch.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        manager.delete_binding(b.id)

        rows = manager.db.execute(
            "SELECT action, entity_type, entity_name, details "
            "FROM audit_log WHERE entity_type = 'binding' ORDER BY id"
        )
        assert [r["action"] for r in rows] == ["create", "delete"]
        assert all(r["entity_name"] == "jetson:recovery_mode" for r in rows)


class TestClaimComposition:
    """Phase 6: claim_sbc consults per-channel busy state."""

    def _setup(self, manager):
        sbc = manager.create_sbc(name="jetson")
        actuator = manager.create_actuator("relay-1", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(actuator.id, 1)
        manager.create_binding(
            sbc.id, "recovery_mode", ch.id,
            shape_mode=ShapeMode.LATCH,
            shape_active=ChannelState.CLOSED,
        )
        return sbc, ch

    def test_claim_succeeds_when_channels_free(self, manager):
        sbc, _ch = self._setup(manager)
        claim = manager.claim_sbc(
            sbc_name=sbc.name,
            agent_name="alice",
            session_id="cli-alice@host",
            session_kind="cli",
            duration_seconds=600,
            reason="test",
        )
        assert claim.id is not None

    def test_claim_refuses_when_channel_busy(self, manager):
        from labctl.actuators import runtime

        sbc, ch = self._setup(manager)
        # Hold the per-channel lock to simulate a verb in flight.
        lock = runtime._get_channel_lock(ch.id)
        assert lock.acquire(blocking=False)
        try:
            with pytest.raises(runtime.ChannelBusyError, match="busy"):
                manager.claim_sbc(
                    sbc_name=sbc.name,
                    agent_name="alice",
                    session_id="cli-alice@host",
                    session_kind="cli",
                    duration_seconds=600,
                    reason="test",
                )
        finally:
            lock.release()
            runtime._clear_runtime_state()

    def test_claim_no_bindings_is_unaffected(self, manager):
        """An SBC with no actuator bindings claims fine even when other
        actuators have busy locks."""
        sbc = manager.create_sbc(name="lonely")
        claim = manager.claim_sbc(
            sbc_name=sbc.name,
            agent_name="alice",
            session_id="cli-alice@host",
            session_kind="cli",
            duration_seconds=600,
            reason="test",
        )
        assert claim.id is not None


class TestBindingConstraints:
    def test_one_binding_per_channel_v1(self, manager):
        """UNIQUE on actuator_channel_id enforces single-target v1."""
        sbc1 = manager.create_sbc(name="a")
        sbc2 = manager.create_sbc(name="b")
        a = manager.create_actuator("relay", DriverName.LCUS1_SERIAL)
        ch = manager.add_actuator_channel(a.id, 1)

        manager.create_binding(
            sbc1.id, "recovery_mode", ch.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        with pytest.raises(Exception):
            manager.create_binding(
                sbc2.id, "recovery_mode", ch.id,
                shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
            )

    def test_one_purpose_per_sbc(self, manager):
        sbc = manager.create_sbc(name="a")
        a = manager.create_actuator("relay", DriverName.LCUS1_SERIAL)
        ch1 = manager.add_actuator_channel(a.id, 1)
        ch2 = manager.add_actuator_channel(a.id, 2)

        manager.create_binding(
            sbc.id, "recovery_mode", ch1.id,
            shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
        )
        with pytest.raises(Exception):
            manager.create_binding(
                sbc.id, "recovery_mode", ch2.id,
                shape_mode=ShapeMode.LATCH, shape_active=ChannelState.CLOSED,
            )
