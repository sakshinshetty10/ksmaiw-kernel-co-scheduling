#!/usr/bin/env python3
"""KSMAIW Linux prototype: Python workloads, Linux affinity enforcement, hysteresis policy."""
from __future__ import annotations
import json
import os
import platform
import signal
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float('nan')
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def cpu_work(iterations: int, seed: int = 1) -> int:
    """Deterministic CPU-bound Python work; not a model inference implementation."""
    x = (seed ^ 0x9E3779B9) & 0xFFFFFFFF
    for i in range(iterations):
        x = (x * 1664525 + 1013904223 + (i ^ seed)) & 0xFFFFFFFF
    return x


def set_affinity(pid: int, cpus: Iterable[int]) -> None:
    os.sched_setaffinity(pid, set(cpus))


def read_proc_cpu_seconds(pid: int) -> float | None:
    """Read user+system CPU time from /proc/<pid>/stat."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_text()
        tail = raw[raw.rfind(')') + 2:].split()
        utime = int(tail[11])
        stime = int(tail[12])
        return (utime + stime) / os.sysconf(os.sysconf_names['SC_CLK_TCK'])
    except (OSError, ValueError, IndexError):
        return None


def cgroup_snapshot() -> dict:
    base = Path('/sys/fs/cgroup')
    result = {'cgroup_v2_present': (base / 'cgroup.controllers').exists()}
    for name in ('cpuset.cpus.effective', 'cpu.max', 'cpu.weight', 'cpu.stat'):
        p = base / name
        try:
            result[name] = p.read_text().strip()
        except OSError as exc:
            result[name] = f'unavailable: {exc}'
    result['cgroup_v2_writable'] = os.access(base, os.W_OK)
    return result


def system_snapshot() -> dict:
    return {
        'platform': platform.platform(),
        'kernel_release': platform.release(),
        'machine': platform.machine(),
        'python': platform.python_version(),
        'pid': os.getpid(),
        'allowed_cpus': sorted(os.sched_getaffinity(0)),
        'logical_cpu_count': os.cpu_count(),
        'cgroup': cgroup_snapshot(),
        'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


@dataclass
class HysteresisConfig:
    high_ms: float = 8.0
    low_ms: float = 5.0
    enter_windows: int = 2
    exit_windows: int = 3
    min_samples: int = 20
    max_measurement_age_ms: float = 750.0
    protect_window_ms: float = 100.0
    service_borrow_ms: float = 70.0


class HysteresisController:
    """Two-threshold, persistent-window controller with stale/small-sample guard."""
    def __init__(self, config: HysteresisConfig):
        self.config = config
        self.state = 'NORMAL'
        self.high_count = 0
        self.low_count = 0
        self.transitions = 0

    def update(self, p99_ms: float | None, sample_count: int, age_ms: float) -> dict:
        old = self.state
        reason = 'insufficient_or_stale_measurement'
        cfg = self.config
        if p99_ms is not None and sample_count >= cfg.min_samples and age_ms <= cfg.max_measurement_age_ms:
            if self.state == 'NORMAL':
                self.low_count = 0
                self.high_count = self.high_count + 1 if p99_ms >= cfg.high_ms else 0
                reason = 'p99_above_enter_threshold' if p99_ms >= cfg.high_ms else 'below_enter_threshold'
                if self.high_count >= cfg.enter_windows:
                    self.state = 'PROTECT'
                    self.high_count = 0
                    reason = 'persistent_high_tail'
            else:
                self.high_count = 0
                self.low_count = self.low_count + 1 if p99_ms <= cfg.low_ms else 0
                reason = 'p99_below_exit_threshold' if p99_ms <= cfg.low_ms else 'above_exit_threshold'
                if self.low_count >= cfg.exit_windows:
                    self.state = 'NORMAL'
                    self.low_count = 0
                    reason = 'persistent_recovery'
        changed = old != self.state
        if changed:
            self.transitions += 1
        return {
            'old_state': old, 'new_state': self.state, 'changed': changed,
            'reason': reason, 'high_count': self.high_count, 'low_count': self.low_count,
            'transition_count': self.transitions,
        }


def apply_process_affinity(pids: list[int], cpus: set[int]) -> None:
    for pid in pids:
        try:
            set_affinity(pid, cpus)
        except ProcessLookupError:
            pass


def signal_process(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
