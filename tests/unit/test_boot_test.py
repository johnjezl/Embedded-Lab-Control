"""Tests for boot test functionality."""

from unittest.mock import MagicMock, patch

import pytest

from labctl.serial.boot_test import BootRunResult, BootTestResult, run_boot_test


class TestBootRunResult:
    """Tests for BootRunResult."""

    def test_pass_result(self):
        r = BootRunResult(
            run_number=1,
            passed=True,
            elapsed_seconds=12.0,
            pattern_matched=True,
            output="boot output",
        )
        assert r.passed is True
        assert r.pattern_matched is True

    def test_fail_result(self):
        r = BootRunResult(
            run_number=1,
            passed=False,
            elapsed_seconds=30.0,
            pattern_matched=False,
            last_line="stuck here",
        )
        assert r.passed is False
        assert r.last_line == "stuck here"


class TestBootTestResult:
    """Tests for BootTestResult."""

    def test_pass_rate_all_pass(self):
        result = BootTestResult(
            sbc_name="test",
            expect_pattern="ok",
            total_runs=3,
            timeout_per_run=30.0,
            runs=[BootRunResult(i, True, 10.0, True) for i in range(1, 4)],
        )
        assert result.passed_count == 3
        assert result.failed_count == 0
        assert result.pass_rate == 100.0

    def test_pass_rate_mixed(self):
        result = BootTestResult(
            sbc_name="test",
            expect_pattern="ok",
            total_runs=4,
            timeout_per_run=30.0,
            runs=[
                BootRunResult(1, True, 10.0, True),
                BootRunResult(2, False, 30.0, False),
                BootRunResult(3, True, 12.0, True),
                BootRunResult(4, False, 30.0, False),
            ],
        )
        assert result.passed_count == 2
        assert result.failed_count == 2
        assert result.pass_rate == 50.0

    def test_avg_boot_time(self):
        result = BootTestResult(
            sbc_name="test",
            expect_pattern="ok",
            total_runs=3,
            timeout_per_run=30.0,
            runs=[
                BootRunResult(1, True, 10.0, True),
                BootRunResult(2, False, 30.0, False),
                BootRunResult(3, True, 14.0, True),
            ],
        )
        assert result.avg_boot_time == 12.0

    def test_avg_boot_time_no_passes(self):
        result = BootTestResult(
            sbc_name="test",
            expect_pattern="ok",
            total_runs=1,
            timeout_per_run=30.0,
            runs=[BootRunResult(1, False, 30.0, False)],
        )
        assert result.avg_boot_time == 0.0

    def test_format_summary(self):
        result = BootTestResult(
            sbc_name="pi-5-1",
            expect_pattern="slmos>",
            total_runs=3,
            timeout_per_run=30.0,
            image="slmos.bin",
            dest="kernel.img",
            partition=1,
            runs=[
                BootRunResult(1, True, 12.0, True),
                BootRunResult(2, False, 30.0, False, last_line="PMM init"),
                BootRunResult(3, True, 11.0, True),
            ],
        )
        summary = result.format_summary()
        assert "pi-5-1" in summary
        assert "slmos>" in summary
        assert "2/3" in summary
        assert "PASS" in summary
        assert "FAIL" in summary
        assert "PMM init" in summary

    def test_format_summary_no_deploy(self):
        result = BootTestResult(
            sbc_name="pi-5-1",
            expect_pattern="ok",
            total_runs=1,
            timeout_per_run=30.0,
            runs=[BootRunResult(1, True, 5.0, True)],
        )
        summary = result.format_summary()
        assert "no deploy" in summary


class TestRunBootTest:
    """Tests for run_boot_test."""

    def test_basic_run(self):
        """Test boot test with mocked serial capture."""
        from labctl.serial.capture import CaptureResult

        mock_power = MagicMock()

        with patch("labctl.serial.boot_test.capture_serial_output") as mock_capture:
            mock_capture.return_value = CaptureResult(
                output="booting...\nslmos>",
                lines=2,
                pattern_matched=True,
                elapsed_seconds=10.0,
            )

            result = run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="slmos>",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                runs=3,
                timeout=30.0,
            )

        assert len(result.runs) == 3
        assert result.passed_count == 3
        assert mock_power.call_count == 3

    def test_mixed_results(self):
        """Test boot test with some failures."""
        from labctl.serial.capture import CaptureResult

        mock_power = MagicMock()
        call_count = [0]

        def mock_capture_fn(**kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                return CaptureResult(
                    output="stuck",
                    lines=1,
                    pattern_matched=False,
                    elapsed_seconds=30.0,
                )
            return CaptureResult(
                output="slmos>",
                lines=1,
                pattern_matched=True,
                elapsed_seconds=10.0,
            )

        with patch(
            "labctl.serial.boot_test.capture_serial_output",
            side_effect=mock_capture_fn,
        ):
            result = run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="slmos>",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                runs=3,
                timeout=30.0,
            )

        assert result.passed_count == 2
        assert result.failed_count == 1

    def test_with_deploy(self):
        """Test boot test calls deploy function."""
        from labctl.serial.capture import CaptureResult

        mock_power = MagicMock()
        mock_deploy = MagicMock()

        with patch("labctl.serial.boot_test.capture_serial_output") as mock_capture:
            mock_capture.return_value = CaptureResult(
                output="ok",
                lines=1,
                pattern_matched=True,
                elapsed_seconds=5.0,
            )

            result = run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="ok",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                deploy_fn=mock_deploy,
                runs=1,
                timeout=10.0,
                image="test.bin",
                dest="kernel.img",
                partition=1,
            )

        mock_deploy.assert_called_once()
        assert result.passed_count == 1
        assert result.image == "test.bin"

    def test_with_output_dir(self, tmp_path):
        """Test boot test saves per-run output."""
        from labctl.serial.capture import CaptureResult

        mock_power = MagicMock()
        output_dir = str(tmp_path / "results")

        with patch("labctl.serial.boot_test.capture_serial_output") as mock_capture:
            mock_capture.return_value = CaptureResult(
                output="boot output here",
                lines=1,
                pattern_matched=True,
                elapsed_seconds=5.0,
            )

            run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="ok",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                runs=2,
                timeout=10.0,
                output_dir=output_dir,
            )

        assert (tmp_path / "results" / "run_01.txt").exists()
        assert (tmp_path / "results" / "run_02.txt").exists()
        assert "boot output" in (tmp_path / "results" / "run_01.txt").read_text()

    def test_progress_callback(self):
        """Test progress callback is called."""
        from labctl.serial.capture import CaptureResult

        mock_power = MagicMock()
        progress_calls = []

        with patch("labctl.serial.boot_test.capture_serial_output") as mock_capture:
            mock_capture.return_value = CaptureResult(
                output="ok",
                lines=1,
                pattern_matched=True,
                elapsed_seconds=5.0,
            )

            run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="ok",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                runs=3,
                timeout=10.0,
                progress_fn=lambda n, t, r: progress_calls.append(n),
            )

        assert progress_calls == [1, 2, 3]

    def test_capture_error_handled(self):
        """Test that capture errors produce failed runs, not exceptions."""
        mock_power = MagicMock()

        with patch(
            "labctl.serial.boot_test.capture_serial_output",
            side_effect=RuntimeError("connection refused"),
        ):
            result = run_boot_test(
                sbc_name="test-sbc",
                expect_pattern="ok",
                tcp_host="localhost",
                tcp_port=4000,
                power_cycle_fn=mock_power,
                runs=2,
                timeout=10.0,
            )

        assert result.passed_count == 0
        assert result.failed_count == 2
        assert "connection refused" in result.runs[0].error
