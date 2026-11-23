#!/usr/bin/env python3
"""Optional cgroup-v2 helper for delegated Linux subtrees.

This module never writes to /sys/fs/cgroup by default. Pass a non-root delegated
subtree explicitly. The experiment runner uses sched_setaffinity so it can run
without cgroup delegation or elevated privileges.
"""
from __future__ import annotations
from pathlib import Path
import os


class CgroupV2Error(RuntimeError):
    pass


class CgroupV2Backend:
    def __init__(self, delegated_root: str | Path):
        self.root = Path(delegated_root).resolve()
        mount = Path('/sys/fs/cgroup').resolve()
        if not str(self.root).startswith(str(mount) + os.sep):
            raise CgroupV2Error('Use a delegated directory below /sys/fs/cgroup, never the mount root.')
        if not (self.root / 'cgroup.controllers').exists():
            raise CgroupV2Error(f'{self.root} is not a cgroup-v2 directory')
        if not os.access(self.root, os.W_OK):
            raise CgroupV2Error(f'{self.root} is not writable by this process')
        self.created: list[Path] = []

    def create_group(self, name: str, cpus: str, weight: int = 100) -> Path:
        if not name or '/' in name or name in {'.', '..'}:
            raise ValueError('name must be a simple directory name')
        if not 1 <= weight <= 10000:
            raise ValueError('cpu.weight must be in [1, 10000]')
        group = self.root / name
        group.mkdir(exist_ok=False)
        self.created.append(group)
        try:
            # cpuset.mems must be initialized before cpuset.cpus on many systems.
            mems = (self.root / 'cpuset.mems.effective').read_text().strip()
            (group / 'cpuset.mems').write_text(mems)
            (group / 'cpuset.cpus').write_text(cpus)
            (group / 'cpu.weight').write_text(str(weight))
        except Exception:
            try:
                group.rmdir()
            except OSError:
                pass
            raise
        return group

    @staticmethod
    def move_pid(group: str | Path, pid: int) -> None:
        path = Path(group)
        (path / 'cgroup.procs').write_text(str(int(pid)))

    @staticmethod
    def set_weight(group: str | Path, weight: int) -> None:
        if not 1 <= weight <= 10000:
            raise ValueError('cpu.weight must be in [1, 10000]')
        (Path(group) / 'cpu.weight').write_text(str(weight))

    def remove_empty_groups(self) -> None:
        for group in reversed(self.created):
            try:
                group.rmdir()
            except OSError as exc:
                raise CgroupV2Error(f'Could not remove {group}; ensure all processes exited: {exc}')
        self.created.clear()
