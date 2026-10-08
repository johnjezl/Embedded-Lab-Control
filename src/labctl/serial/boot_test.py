"""
Automated boot reliability testing.

Deploys a kernel image, reboots the SBC multiple times, and captures
boot output to determine success rate.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from labctl.serial.capture import capture_serial_output

logger = logging.getLogger(__name__)


def _write_run_file(path: Path | str, text: str, dir_fd: Optional[int] = None) -> None:
    """Write one run's output without following a planted link.

    The output directory may be shared (e.g. group-writable
    /var/lib/labctl/output), and the content is console output the DUT
    controls. A symlink planted at ``run_NN.txt`` would otherwise redirect
    the write to any file the service user can write, so open with
    O_NOFOLLOW (fails with ELOOP on a symlink), and refuse a file with
    other hard links. With ``dir_fd`` (a checked directory handle) the
    file is created relative to it, so the directory path itself can't
    be swapped either. Raises OSError on refusal.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    try:
        if os.fstat(fd).st_nlink > 1:
            raise OSError(f"refusing to write {path}: it has other hard links")
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", closefd=False) as f:
            f.write(text)
    finally:
        os.close(fd)


@dataclass
class BootRunResult:
    """Result of a single boot run."""

    run_number: int
    passed: bool
    elapsed_seconds: float
    pattern_matched: bool
    output: str = ""
    last_line: str = ""
    error: str = ""


@dataclass
class BootTestResult:
    """Result of a complete boot test."""

    sbc_name: str
    expect_pattern: str
    total_runs: int
    timeout_per_run: float
    image: Optional[str] = None
    dest: Optional[str] = None
    partition: Optional[int] = None
    runs: list[BootRunResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.runs if not r.passed)

    @property
    def pass_rate(self) -> float:
        if not self.runs:
            return 0.0
        return self.passed_count / len(self.runs) * 100

    @property
    def avg_boot_time(self) -> float:
        passed = [r.elapsed_seconds for r in self.runs if r.passed]
        if not passed:
            return 0.0
        return sum(passed) / len(passed)

    def format_summary(self) -> str:
        """Format as human-readable summary."""
        lines = [
            f"Boot Test Results: {self.sbc_name}",
            "=" * 40,
        ]

        if self.image:
            lines.append(
                f"Image: {self.image} -> {self.dest} (partition {self.partition})"
            )
        else:
            lines.append("Image: (no deploy, testing current SD contents)")

        lines.append(f"Pattern: '{self.expect_pattern}'")
        lines.append(f"Timeout: {self.timeout_per_run:.0f}s per boot")
        lines.append("")

        for r in self.runs:
            status = "PASS" if r.passed else "FAIL"
            detail = "pattern matched" if r.pattern_matched else ""
            if not r.passed:
                if r.error:
                    detail = r.error
                elif r.last_line:
                    detail = f'timeout, last output: "{r.last_line}"'
                else:
                    detail = "timeout, no output"
            lines.append(
                f"Run {r.run_number:2d}/{self.total_runs}: "
                f"{status} ({r.elapsed_seconds:.1f}s) - {detail}"
            )

        lines.append("")
        lines.append(
            f"Result: {self.passed_count}/{len(self.runs)} boots successful "
            f"({self.pass_rate:.0f}%)"
        )

        if self.passed_count > 0:
            lines.append(f"Average boot time (successful): {self.avg_boot_time:.1f}s")

        # Failure mode breakdown
        if self.failed_count > 0:
            timeout_partial = sum(
                1 for r in self.runs if not r.passed and not r.error and r.last_line
            )
            timeout_none = sum(
                1 for r in self.runs if not r.passed and not r.error and not r.last_line
            )
            errors = sum(1 for r in self.runs if not r.passed and r.error)
            lines.append("Failure modes:")
            if timeout_partial:
                lines.append(f"  - Timeout with partial output: {timeout_partial}")
            if timeout_none:
                lines.append(f"  - Timeout with no output: {timeout_none}")
            if errors:
                lines.append(f"  - Errors: {errors}")

        return "\n".join(lines)


def run_boot_test(
    sbc_name: str,
    expect_pattern: str,
    tcp_host: str,
    tcp_port: int,
    power_cycle_fn,
    runs: int = 10,
    timeout: float = 30.0,
    deploy_fn=None,
    image: str | None = None,
    dest: str | None = None,
    partition: int = 1,
    output_dir: str | None = None,
    output_dir_fd: Optional[int] = None,
    progress_fn=None,
) -> BootTestResult:
    """Run automated boot reliability test.

    Args:
        sbc_name: Name of the SBC being tested
        expect_pattern: Regex to match for "success"
        tcp_host: Host where ser2net is running
        tcp_port: TCP port for serial console
        power_cycle_fn: Callable that power cycles the SBC (off, wait, on)
        runs: Number of boot cycles
        timeout: Seconds to wait per boot for pattern match
        deploy_fn: Optional callable to deploy image before testing
        image: Image filename (for reporting)
        dest: Destination filename (for reporting)
        partition: Partition number (for reporting)
        output_dir: Save per-run output to files here
        output_dir_fd: Optional open handle for ``output_dir`` (already
            checked and created by the caller); files are created
            relative to it and ``output_dir`` is only used for messages
        progress_fn: Optional callback(run_number, total, result) for progress
    """
    result = BootTestResult(
        sbc_name=sbc_name,
        expect_pattern=expect_pattern,
        total_runs=runs,
        timeout_per_run=timeout,
        image=image,
        dest=dest,
        partition=partition,
    )

    # Deploy if requested
    if deploy_fn:
        deploy_fn()

    # Create output dir if needed (a caller-provided handle already exists)
    if output_dir and output_dir_fd is None:
        os.makedirs(output_dir, exist_ok=True)

    for i in range(1, runs + 1):
        run_result = _run_single_boot(
            run_number=i,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            power_cycle_fn=power_cycle_fn,
            expect_pattern=expect_pattern,
            timeout=timeout,
        )

        result.runs.append(run_result)

        # Save per-run output
        if output_dir and run_result.output:
            name = f"run_{i:02d}.txt"
            run_file = Path(output_dir) / name
            try:
                if output_dir_fd is not None:
                    _write_run_file(name, run_result.output, dir_fd=output_dir_fd)
                else:
                    _write_run_file(run_file, run_result.output)
            except OSError as e:
                logger.warning("Not writing %s: %s", run_file, e)

        if progress_fn:
            progress_fn(i, runs, run_result)

    return result


def _run_single_boot(
    run_number: int,
    tcp_host: str,
    tcp_port: int,
    power_cycle_fn,
    expect_pattern: str,
    timeout: float,
) -> BootRunResult:
    """Execute a single boot cycle and capture output."""
    try:
        # Power cycle: off, wait, on
        power_cycle_fn()

        # Small delay for power to settle before capturing
        time.sleep(1)

        # Capture serial output
        capture = capture_serial_output(
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            timeout=timeout,
            until_pattern=expect_pattern,
        )

        # Extract last non-empty line for failure reporting
        last_line = ""
        if capture.output:
            output_lines = capture.output.strip().splitlines()
            if output_lines:
                last_line = output_lines[-1][:80]  # Truncate

        return BootRunResult(
            run_number=run_number,
            passed=capture.pattern_matched,
            elapsed_seconds=capture.elapsed_seconds,
            pattern_matched=capture.pattern_matched,
            output=capture.output,
            last_line=last_line,
        )

    except RuntimeError as e:
        return BootRunResult(
            run_number=run_number,
            passed=False,
            elapsed_seconds=0.0,
            pattern_matched=False,
            error=str(e),
        )
