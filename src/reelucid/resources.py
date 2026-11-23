"""Estimate capacity and current pressure in the host and its Linux cgroups."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from sys import platform
from time import monotonic

import psutil

FALLBACK_MEMORY = 2 * 1024**3


@dataclass(frozen=True, slots=True)
class ResourceSample:
    cpu_cores: float
    memory_limit: int
    memory_available: int
    cpu_usage: float | None
    cpu_fallback: bool = False
    memory_fallback: bool = False


def private_memory(pid: int) -> int:
    """Return private resident bytes already reflected in memory availability."""
    try:
        return max(0, psutil.Process(pid).memory_full_info().uss)
    except OSError, psutil.Error:
        return 0


@dataclass(frozen=True, slots=True)
class _Cgroups:
    unified: tuple[Path, ...] = ()
    cpu: tuple[Path, ...] = ()
    cpuacct: tuple[Path, ...] = ()
    memory: tuple[Path, ...] = ()


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeError:
        return ""


def _mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _levels(root: PurePosixPath, mount: Path, member: PurePosixPath) -> tuple[Path, ...]:
    if ".." in member.parts:
        return ()
    if member.is_relative_to(root):
        relative = member.relative_to(root)
    else:
        # A cgroup namespace can report membership relative to the mount root.
        relative = member.relative_to("/")
    leaf = mount.joinpath(*relative.parts)
    if not leaf.is_dir():
        return ()
    return (leaf, *(parent for parent in leaf.parents if parent.is_relative_to(mount)))


def _groups(proc: Path) -> _Cgroups:
    memberships: dict[str, PurePosixPath] = {}
    for line in _text(proc / "cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[2].startswith("/"):
            member = PurePosixPath("/" + fields[2].lstrip("/"))
            memberships.update((name, member) for name in fields[1].split(","))

    locations: dict[str, tuple[Path, ...]] = {}
    for line in _text(proc / "mountinfo").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        mount_fields, fs_fields = left.split(), right.split()
        if len(mount_fields) < 5 or len(fs_fields) < 3:
            continue
        kind = fs_fields[0]
        controllers = ("",) if kind == "cgroup2" else ()
        if kind == "cgroup":
            controllers = tuple(fs_fields[2].split(","))
        for controller in controllers:
            if controller in memberships:
                root = PurePosixPath(_mount_path(mount_fields[3]))
                mount = Path(_mount_path(mount_fields[4]))
                paths = _levels(root, mount, memberships[controller])
                # Prefer the mount exposing the highest ancestors if there are several.
                if len(paths) > len(locations.get(controller, ())):
                    locations[controller] = paths
    return _Cgroups(*(locations.get(name, ()) for name in ("", "cpu", "cpuacct", "memory")))


def _integer(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _stat(path: Path, key: str) -> int | None:
    for line in _text(path).splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == key:
            return _integer(fields[1])
    return None


def _quota(quota: str, period: str) -> float | None:
    maximum, interval = _integer(quota), _integer(period)
    if maximum is not None and maximum > 0 and interval is not None and interval > 0:
        return maximum / interval
    return None


def _cpu_quotas(groups: _Cgroups) -> dict[Path, float]:
    quotas: dict[Path, float] = {}
    for path in groups.unified:
        fields = _text(path / "cpu.max").split()
        if len(fields) == 2 and (limit := _quota(*fields)) is not None:
            quotas[path] = limit
    for path in groups.cpu:
        limit = _quota(_text(path / "cpu.cfs_quota_us"), _text(path / "cpu.cfs_period_us"))
        if limit is not None:
            quotas[path] = limit
    return quotas


def _core_count() -> int | None:
    try:
        return os.process_cpu_count() or os.cpu_count()
    except OSError:
        return os.cpu_count()


def _host_cpu() -> tuple[float, float] | None:
    try:
        times = psutil.cpu_times()
    except OSError, RuntimeError, psutil.Error:
        return None

    idle = times.idle + getattr(times, "iowait", 0.0)
    total = sum(times) - getattr(times, "guest", 0.0) - getattr(times, "guest_nice", 0.0)
    return total, total - idle


def _host_memory() -> tuple[int, int] | None:
    try:
        memory = psutil.virtual_memory()
    except OSError, RuntimeError, psutil.Error:
        return None
    return (memory.total, memory.available) if memory.total > 0 else None


def _memory_bounds(groups: _Cgroups) -> tuple[list[int], list[int]]:
    limits: list[int] = []
    available: list[int] = []
    for paths, limit_file, usage_file, inactive_key in (
        (groups.unified, "memory.max", "memory.current", "inactive_file"),
        (groups.memory, "memory.limit_in_bytes", "memory.usage_in_bytes", "total_inactive_file"),
    ):
        for path in paths:
            limit = _integer(_text(path / limit_file))
            # cgroup v1 uses a page-aligned LONG_MAX value for unlimited.
            if limit is None or limit < 0 or limit >= 1 << 60:
                continue
            limits.append(limit)
            usage = _integer(_text(path / usage_file))
            if usage is None or usage < 0:
                available.append(min(limit, FALLBACK_MEMORY))
                continue
            inactive = max(0, _stat(path / "memory.stat", inactive_key) or 0)
            available.append(max(0, limit - max(0, usage - inactive)))
    return limits, available


def _group_cpu(
    groups: _Cgroups, quotas: dict[Path, float], cores: float
) -> dict[Path, tuple[float, float]]:
    samples: dict[Path, tuple[float, float]] = {}
    if any(path in quotas for path in groups.unified):
        leaf, *ancestors = groups.unified
        for path in (leaf, *(path for path in ancestors if path in quotas)):
            usage = _stat(path / "cpu.stat", "usage_usec")
            if usage is not None:
                samples[path] = (usage / 1_000_000, cores if path == leaf else quotas[path])
    if groups.cpuacct and any(path in quotas for path in groups.cpu):
        leaf, *ancestors = groups.cpuacct
        for path in (leaf, *(path for path in ancestors if path in quotas)):
            usage = _integer(_text(path / "cpuacct.usage"))
            if usage is not None:
                samples[path] = (usage / 1_000_000_000, cores if path == leaf else quotas[path])
    return samples


class ResourceMonitor:
    """One sampler per scheduler. CPU fractions require two successive measurements."""

    def __init__(self) -> None:
        self._groups: _Cgroups | None = None
        self._previous_host: tuple[float, float] | None = None
        self._previous_group: dict[Path, tuple[float, float]] = {}
        self._previous_time: float | None = None

    def _pressure(self, groups: _Cgroups, quotas: dict[Path, float], cores: float) -> float | None:
        host = _host_cpu()
        group = _group_cpu(groups, quotas, cores)
        now = monotonic()
        fractions: list[float] = []
        if host is not None and self._previous_host is not None:
            total, busy = host[0] - self._previous_host[0], host[1] - self._previous_host[1]
            if total > 0 and busy >= 0:
                fractions.append(busy / total)
        if self._previous_time is not None and (elapsed := now - self._previous_time) > 0:
            for path, (usage, limit) in group.items():
                last = self._previous_group.get(path)
                if last is not None and usage >= last[0] and limit == last[1]:
                    fractions.append((usage - last[0]) / (elapsed * limit))

        self._previous_host, self._previous_group, self._previous_time = host, group, now
        return min(1.0, max(fractions)) if fractions else None

    def read(self) -> ResourceSample:
        if self._groups is None:
            self._groups = _groups(Path("/proc/self")) if platform == "linux" else _Cgroups()
        groups = self._groups
        quotas = _cpu_quotas(groups)
        core_limits = list(quotas.values())
        if (count := _core_count()) is not None and count > 0:
            core_limits.append(float(count))
        cores = min(core_limits, default=1.0)

        memory_limits, free_memory = _memory_bounds(groups)
        if (host_memory := _host_memory()) is not None:
            memory_limits.append(host_memory[0])
            free_memory.append(host_memory[1])
        memory = min(memory_limits, default=FALLBACK_MEMORY)
        return ResourceSample(
            cpu_cores=cores,
            memory_limit=memory,
            memory_available=max(0, min(memory, *free_memory)) if free_memory else memory,
            cpu_usage=self._pressure(groups, quotas, cores),
            cpu_fallback=not core_limits,
            memory_fallback=not memory_limits,
        )
