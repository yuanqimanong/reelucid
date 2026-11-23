from pathlib import Path

import pytest

from reelucid import resources
from reelucid.resources import ResourceMonitor, ResourceSample

GIB = 1024**3


def write_values(directory: Path, values: dict[str, int | str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (directory / name).write_text(f"{value}\n", encoding="utf-8")


def mountinfo(root: str, mount: Path, kind: str, controllers: str = "rw") -> str:
    encoded_mount = mount.as_posix().replace(" ", r"\040")
    return f"18 15 0:15 {root} {encoded_mount} ro - {kind} cgroup {controllers}"


def test_unified_cgroup_uses_parent_quota_and_shared_pressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc, mount = tmp_path / "proc", tmp_path / "cgroup fs"
    parent, leaf = mount / "parent", mount / "parent" / "leaf"
    write_values(
        proc,
        {"cgroup": "0::/parent/leaf", "mountinfo": mountinfo("/", mount, "cgroup2")},
    )
    write_values(mount, {"cpu.max": "max 100000", "memory.max": "max"})
    write_values(
        parent,
        {
            "cpu.max": "200000 100000",
            "cpu.stat": "usage_usec 20000000",
            "memory.max": 8 * GIB,
            "memory.current": 7 * GIB + GIB // 2,
            "memory.stat": f"inactive_file {GIB // 2}",
        },
    )
    write_values(
        leaf,
        {
            "cpu.max": "400000 100000",
            "cpu.stat": "usage_usec 10000000",
            "memory.max": 6 * GIB,
            "memory.current": GIB,
        },
    )
    groups = resources._groups(proc)  # pyright: ignore[reportPrivateUsage]
    assert groups.unified == (leaf, parent, mount)
    monkeypatch.setattr(resources, "_core_count", lambda: 12)
    monkeypatch.setattr(resources, "_host_memory", lambda: (16 * GIB, 12 * GIB))
    monkeypatch.setattr(resources, "_host_cpu", iter(((100.0, 20.0), (110.0, 27.0))).__next__)
    monkeypatch.setattr(resources, "monotonic", iter((0.0, 1.0)).__next__)

    monitor = ResourceMonitor()
    monkeypatch.setattr(monitor, "_groups", groups)
    assert monitor.read() == ResourceSample(2, 6 * GIB, GIB, None)
    write_values(parent, {"cpu.stat": "usage_usec 21800000"})
    write_values(leaf, {"cpu.stat": "usage_usec 10800000"})
    assert monitor.read().cpu_usage == pytest.approx(0.9)


def test_legacy_cgroup_uses_separate_controller_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = tmp_path / "proc"
    cpu, acct, mem = (tmp_path / name for name in ("cpu", "cpuacct", "memory"))
    write_values(
        proc,
        {
            "cgroup": "2:cpu:/docker/id\n3:cpuacct:/docker/id\n4:memory:/docker/id",
            "mountinfo": "\n".join(
                mountinfo("/docker", root, "cgroup", f"rw,{name}")
                for root, name in ((cpu, "cpu"), (acct, "cpuacct"), (mem, "memory"))
            ),
        },
    )
    write_values(cpu / "id", {"cpu.cfs_quota_us": 50000, "cpu.cfs_period_us": 100000})
    write_values(acct / "id", {"cpuacct.usage": 500_000_000})
    write_values(mem, {"memory.limit_in_bytes": 9223372036854771712})
    write_values(
        mem / "id",
        {
            "memory.limit_in_bytes": 4 * GIB,
            "memory.usage_in_bytes": 2 * GIB,
            "memory.stat": f"total_inactive_file {GIB // 2}",
        },
    )
    groups = resources._groups(proc)  # pyright: ignore[reportPrivateUsage]
    assert groups.cpu == (cpu / "id", cpu)
    assert groups.cpuacct == (acct / "id", acct)
    assert groups.memory == (mem / "id", mem)
    monkeypatch.setattr(resources, "_core_count", lambda: 6)
    monkeypatch.setattr(resources, "_host_memory", lambda: None)
    monkeypatch.setattr(resources, "_host_cpu", lambda: None)
    monkeypatch.setattr(resources, "monotonic", iter((0.0, 1.0)).__next__)

    monitor = ResourceMonitor()
    monkeypatch.setattr(monitor, "_groups", groups)
    assert monitor.read() == ResourceSample(0.5, 4 * GIB, 2 * GIB + GIB // 2, None)
    write_values(acct / "id", {"cpuacct.usage": 800_000_000})
    assert monitor.read().cpu_usage == pytest.approx(0.6)


def test_namespaced_mount_uses_affinity_and_host_available_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc, mount = tmp_path / "proc", tmp_path / "cgroup"
    write_values(
        proc,
        {"cgroup": "0::/", "mountinfo": mountinfo("/docker/container", mount, "cgroup2")},
    )
    write_values(
        mount,
        {"cpu.max": "400000 100000", "cpu.stat": "usage_usec 0", "memory.max": 6 * GIB},
    )
    groups = resources._groups(proc)  # pyright: ignore[reportPrivateUsage]
    assert groups.unified == (mount,)
    monkeypatch.setattr(resources.os, "process_cpu_count", lambda: 2)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        resources, "_host_memory", iter(((8 * GIB, GIB), (8 * GIB, 4 * GIB))).__next__
    )
    monkeypatch.setattr(resources, "_host_cpu", lambda: None)
    monkeypatch.setattr(resources, "monotonic", iter((0.0, 1.0)).__next__)

    monitor = ResourceMonitor()
    monkeypatch.setattr(monitor, "_groups", groups)
    assert monitor.read() == ResourceSample(2, 6 * GIB, GIB, None)
    write_values(mount, {"cpu.stat": "usage_usec 1500000"})
    assert monitor.read() == ResourceSample(2, 6 * GIB, 2 * GIB, 0.75)


def test_missing_metrics_fall_back_per_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "platform", "win32")
    monkeypatch.setattr(resources.os, "process_cpu_count", lambda: None)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: None)
    monkeypatch.setattr(resources, "_host_memory", lambda: None)
    monkeypatch.setattr(resources, "_host_cpu", lambda: None)
    monitor = ResourceMonitor()
    assert monitor.read() == ResourceSample(1, 2 * GIB, 2 * GIB, None, True, True)

    monkeypatch.setattr(resources, "_host_memory", lambda: (12 * GIB, 7 * GIB))
    assert monitor.read() == ResourceSample(1, 12 * GIB, 7 * GIB, None, True, False)


def test_native_host_probe_reports_usable_capacity() -> None:
    sample = ResourceMonitor().read()
    assert sample.cpu_cores > 0
    assert sample.memory_limit > 0
    assert 0 <= sample.memory_available <= sample.memory_limit
