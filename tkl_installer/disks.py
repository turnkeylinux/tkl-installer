"""Disk detection, probing, and validation.

All disk inspection uses standard Debian tools: ``lsblk``, ``blkid``,
``pvs``, ``wipefs``, and ``blockdev``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from os.path import basename

from .runner import RunError, run, run_output

log = logging.getLogger(__name__)


def human_size(size_mb: int) -> str:
    """Convert a size in MiB to a human-readable string.

    Args:
        size_mb (int): Size in MiB.

    Returns:
        str: Human-readable size rounded to two decimal places -
            e.g. ``1536`` -> ``"1.5 GiB"``.

    """
    if size_mb >= 1024 * 1024:
        return f"{round(size_mb / 1024 * 1024, 2)} TiB"
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 2)} GiB"
    return f"{size_mb} MiB"


@dataclass
class PartInfo:
    """Information about a single existing partition on a disk.

    Attributes:
        path (str): Partition device path - e.g. ``"/dev/sda1"``.
        size_mb (int): Partition size in MiB.
        fs (str): Filesystem type if formatted, otherwise ``""``.
        label (str): Partition label if set, otherwise ``""``.
        mount (str): Current mount point if mounted, otherwise ``""``.
        part_type (str): Partition type string as reported by
            ``lsblk``.

    """

    path: str
    size_mb: int
    fs: str = ""
    label: str = ""
    mount: str = ""
    part_type: str = ""


@dataclass
class DiskInfo:
    """Hardware and state information for a single block device.

    Attributes:
        path (str): Disk device path - e.g. ``"/dev/sda"``.
        name (str): Kernel device name - e.g. ``"sda"``.
        size_mb (int): Disk size in MiB.
        model (str): Disk model name as reported by the kernel.
        transport (str): Data transport type - e.g. ``"usb"``,
            ``"sata"``, ``"nvme"``.
        removable (bool): ``True`` if the disk is flagged as removable.
        partitions (list[PartInfo]): Existing partitions on the disk -
            see ``.disks.PartInfo``.
        is_lvm_pv (bool): ``True`` if the disk or any partition is an
            LVM Physical Volume.
        is_live_device (bool): ``True`` if heuristics suggest this is
            the live installation medium.

    """

    path: str
    name: str
    size_mb: int
    model: str = ""
    transport: str = ""
    removable: bool = False
    partitions: list[PartInfo] = field(default_factory=list)
    is_lvm_pv: bool = False
    is_live_device: bool = False


def _device_name_sort_key(name: str) -> tuple[int, int]:
    """Return a sort key for a kernel device or LV name.

    Sorting helper for sorting paritions/LVs.

    Physical partitions (names ending in digits) are ordered by partition
    number. LVM LVs are ordered: 'root' first, 'swap last', anything else in
    between.
    """
    lv_order = {"root": 0, "swap": 2}
    default_lv = 1
    match = re.search(r"\d+$", name)
    if match:
        return (0, int(match.group()))
    return (1, lv_order.get(name, default_lv))


def _disk_info_sort_key(disk: DiskInfo) -> tuple[int, int]:
    """Sort key for ``DiskInfo`` instances - see ``_device_name_sort_key``."""
    return _device_name_sort_key(disk.name)


def _find_live_devices() -> set[str]:
    """Heuristically identify the block device the live system booted from.

    Checks mount points and filesystem types associated with live systems.

    Returns:
        set[str]: Kernel device names that appear to be the live medium
            - e.g. ``{"sda", "sda1"}``.

    """
    live_devs: set[str] = set()

    try:
        with open("/proc/mounts") as fob:
            mounts = fob.readlines()
    except OSError:
        log.warning(
            "Could not read /proc/mounts - live device detection may be"
            " inaccurate.",
        )
        mounts = []

    live_mount_points = {"/", "/cdrom", "/run/live", "/live", "/run/initramfs"}
    for line in mounts:
        parts = line.split()
        if len(parts) < 2:
            continue
        dev, mp = parts[0], parts[1]
        if (
            mp in live_mount_points or mp.startswith(("/lib/live", "/media"))
        ) and dev.startswith("/dev/"):
            devname = basename(dev)
            # nvme0n1p1 -> nvme0n1;  sda1 -> sda
            disk = re.sub(r"p?\d+$", "", devname)
            live_devs.add(devname)
            live_devs.add(disk)
            log.debug(
                "Live device candidate from mounts: %s -> %s",
                dev,
                disk,
            )

    # also flag ISO9660/squashfs/overlay mounts that hint at live media
    for line in mounts:
        parts = line.split()
        if len(parts) >= 3 and parts[2] in ("iso9660", "squashfs", "overlay"):
            dev = parts[0]
            if dev.startswith("/dev/"):
                devname = os.path.basename(dev)
                disk = re.sub(r"p?\d+$", "", devname)
                live_devs.add(devname)
                live_devs.add(disk)

    return live_devs


def probe_disks() -> list[DiskInfo]:
    """Return a ``DiskInfo`` list for every non-virtual block device found.

    Marks each entry with ``is_live_device`` where heuristics suggest it
    is the live installation medium.

    Returns:
        list[DiskInfo]: All discovered disks - see ``.disks.DiskInfo``.

    Raises:
        RuntimeError: If ``lsblk`` fails or its output cannot be parsed.

    """
    try:
        raw = run_output(
            [
                "lsblk",
                "--json",
                "--output",
                "NAME,SIZE,MODEL,TRAN,RM,TYPE,FSTYPE,LABEL,MOUNTPOINT",
                "--bytes",
            ],
        )
    except RunError as e:
        raise RuntimeError(f"lsblk failed: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse lsblk output: {e}") from e

    live_devs = _find_live_devices()
    results: list[DiskInfo] = []

    for blk in data.get("blockdevices", []):
        if blk.get("type") != "disk":
            continue
        name = blk["name"]
        path = f"/dev/{name}"

        size_bytes = int(blk.get("size") or 0)
        size_mb = size_bytes // (1024 * 1024)

        partitions: list[PartInfo] = []
        for child in blk.get("children") or []:
            if child.get("type") not in ("part", "lvm"):
                continue
            partitions.append(
                PartInfo(
                    path=f"/dev/{child['name']}",
                    size_mb=int(child.get("size") or 0) // (1024 * 1024),
                    fs=child.get("fstype") or "",
                    label=child.get("label") or "",
                    mount=child.get("mountpoint") or "",
                ),
            )

        is_lvm_pv = _is_lvm_pv(path)

        disk = DiskInfo(
            path=path,
            name=name,
            size_mb=size_mb,
            model=(blk.get("model") or "").strip(),
            transport=blk.get("tran") or "",
            removable=bool(blk.get("rm")),
            partitions=partitions,
            is_lvm_pv=is_lvm_pv,
            is_live_device=(name in live_devs),
        )
        results.append(disk)
        log.debug(
            "Found disk: %s size=%dMiB live=%s",
            path,
            size_mb,
            disk.is_live_device,
        )
    return results


def _is_lvm_pv(disk_path: str) -> bool:
    """Return ``True`` if the disk or any of its partitions is an LVM PV.

    Args:
        disk_path (str): Disk device path - e.g. ``"/dev/sda"``.

    Returns:
        bool: ``True`` if an LVM PV is found on the device.

    """
    try:
        out = run_output(
            ["pvs", "--noheadings", "--options", "pv_name"],
            check=False,
        )
        for line in out.splitlines():
            if disk_path in line.strip():
                return True
    except RunError:
        pass
    return False


def get_candidate_disks(all_disks: list[DiskInfo]) -> list[DiskInfo]:
    """Filter out the live device, loop devices, and RAM disks.

    Args:
        all_disks (list[DiskInfo]): All discovered disks - see
            ``.disks.DiskInfo``.

    Returns:
        list[DiskInfo]: Disks suitable as install targets - see
            ``.disks.DiskInfo``.

    """
    candidates = []
    for d in all_disks:
        if d.is_live_device:
            log.info("Skipping live device: %s", d.path)
            continue
        if d.name.startswith("loop") or d.name.startswith("ram"):
            continue
        candidates.append(d)
    return candidates


def disk_has_partition_table(disk_path: str) -> bool:
    """Return ``True`` if the disk has any recognisable partition table.

    Args:
        disk_path (str): Disk device path - e.g. ``"/dev/sda"``.

    Returns:
        bool: ``True`` if ``blkid`` detects a partition table or
            filesystem signature.

    """
    try:
        out = run_output(
            [
                "blkid",
                "--probe",
                "--usages",
                "filesystem,raid,crypto,other",
                disk_path,
            ],
            check=False,
        )
        return bool(out)
    except RunError:
        return False


def wipe_disk(disk_path: str) -> None:
    """Destroy all signatures and partition tables on a disk.

    Uses ``wipefs`` followed by a partial zero-fill of the first and
    last 2 MiB to ensure GPT primary and secondary headers are cleared.

    Args:
        disk_path (str): Disk device path - e.g. ``"/dev/sda"``.

    """
    log.info("Wiping disk %s", disk_path)
    run(["wipefs", "--all", "--force", disk_path], destructive=True)
    # Zero the first 2 MiB (MBR / GPT primary header)
    run(
        [
            "dd",
            "if=/dev/zero",
            f"of={disk_path}",
            "bs=1M",
            "count=2",
            "conv=noerror,sync",
        ],
        destructive=True,
        capture=True,
    )
    # Zero the last 2 MiB (GPT secondary header)
    try:
        size_bytes = int(
            run_output(
                ["blockdev", "--getsize64", disk_path],
                destructive=False,
            ),
        )
        seek_blocks = (size_bytes // (1024 * 1024)) - 2
        if seek_blocks > 0:
            run(
                [
                    "dd",
                    "if=/dev/zero",
                    f"of={disk_path}",
                    "bs=1M",
                    "count=2",
                    f"seek={seek_blocks}",
                    "conv=noerror,sync",
                ],
                destructive=True,
                capture=True,
            )
    except RunError:
        log.warning("Could not zero end of disk - continuing anyway.")
    run(["partprobe", disk_path], check=False, destructive=True)
    run(["udevadm", "settle"], check=False, destructive=True)


# UEFI detection


def detect_uefi() -> bool:
    """Return ``True`` if the running system booted via UEFI."""
    return os.path.isdir("/sys/firmware/efi")


# Absolute minimum disk sizes (MiB)
MIN_DISK_MB_EFI = 3 * 1024
MIN_DISK_MB_LEGACY = 3 * 1024


def validate_disk_size(disk: DiskInfo, uefi: bool) -> tuple[bool, str]:
    """Check whether a disk meets the minimum size requirement for install.

    Args:
        disk (DiskInfo): Disk to validate - see ``.disks.DiskInfo``.
        uefi (bool): ``True`` if the install will use a UEFI layout
            (slightly larger minimum due to EFI partition).

    Returns:
        tuple[bool, str]: ``(True, "")`` if the disk is large enough,
            or ``(False, reason)`` if it is too small.

    """
    minimum = MIN_DISK_MB_EFI if uefi else MIN_DISK_MB_LEGACY
    if disk.size_mb < minimum:
        return (
            False,
            f"{disk.path} is only {human_size(disk.size_mb)} - "
            f"minimum required is {human_size(minimum)}.",
        )
    return True, ""
