"""Partition table and LVM layout calculation and application.

All partitioning uses ``sfdisk`` (scriptable, non-interactive).
LVM setup uses ``pvcreate``, ``vgcreate``, and ``lvcreate``.
Filesystem creation uses ``mkfs.*`` and ``mkswap``.
"""

from __future__ import annotations

import logging
import os
import time

from .config import PartitionEntry, PartitionScheme
from .disks import DiskInfo, _device_name_sort_key
from .runner import RunError, run

log = logging.getLogger(__name__)

# Size constants (MiB)

EFI_MB = 512
# 1GB /boot is recommended min if /boot is a separate partition due to risk of
# it filling up with multiple kernels over time. Larger /boot doesn't fix it
# but does reduce the risk and extend the time before it causes issues.
# When /boot is merged into /, add BOOT_MB * 0.5 to the root minimum warning
# threshold to account for boot/kernel files - but reduced risk of /boot
# overfilling.
BOOT_MB = 1024
SWAP_SMALL_MB = 512  # used when disk < SMALL_DISK_MB
SWAP_NORMAL_MB = 2048
SWAP_LARGE_MB = 4096  # used when disk >= LARGE_DISK_MB
SMALL_DISK_MB = 16 * 1024
LARGE_DISK_MB = 64 * 1024
# Ideally this should account for rootfs size + working headroom.
MIN_ROOT_MB = 2 * 1024  # 2 GiB absolute minimum


def _partition_sort_key(entry: PartitionEntry) -> tuple[int, int]:
    """Sort key for ``PartitionEntry`` instances.

    See ``disks._device_name_sort_key``.
    """
    return _device_name_sort_key(entry.name)


def calculate_default_scheme(
    disk: DiskInfo,
    uefi: bool,
    scheme_type: str,
    separate_boot: bool = False,
) -> PartitionScheme:
    """Generate a default partition scheme for the given disk and options.

    Args:
        disk (DiskInfo): Disk to partition - see ``.disks.DiskInfo``.
        uefi (bool): ``True`` if the scheme is for a UEFI system.
        scheme_type (str): One of ``"guided"``, ``"guided-lvm"``, or
            ``"manual"``.
        separate_boot (bool): If ``True``, create a dedicated ``/boot``
            partition of ``BOOT_MB`` MiB.  If ``False``, ``/boot``
            lives inside ``/``.  Defaults to ``False``.
            LUKS NOTE: when adding a ``"guided-luks-lvm"`` scheme,
            force ``separate_boot=True`` here - ``/boot`` must remain
            unencrypted for the bootloader.

    Returns:
        PartitionScheme: Calculated layout
            - see ``.config.PartitionScheme``.
            - see also _partition_sort_key() for partition sort order.

    """
    available_mb = disk.size_mb - 2  # leave ~1 MiB at each end for GPT/align

    efi_mb = EFI_MB if uefi else 0
    # LUKS NOTE: when adding "guided-luks-lvm", always set boot_mb = BOOT_MB
    # unconditionally so /boot is always a separate partition.
    boot_mb = BOOT_MB if separate_boot else 0

    if disk.size_mb < SMALL_DISK_MB:
        swap_mb = SWAP_SMALL_MB
    elif disk.size_mb >= LARGE_DISK_MB:
        swap_mb = SWAP_LARGE_MB
    else:
        swap_mb = SWAP_NORMAL_MB

    root_mb = available_mb - efi_mb - boot_mb - swap_mb
    if root_mb < MIN_ROOT_MB:
        swap_mb = max(0, available_mb - efi_mb - boot_mb - MIN_ROOT_MB)
        root_mb = available_mb - efi_mb - boot_mb - swap_mb

    scheme = PartitionScheme(
        disk=disk.path,
        scheme_type=scheme_type,
        uefi=uefi,
        separate_boot=separate_boot,
    )

    part_num = 1

    if uefi:
        scheme.partitions.append(
            PartitionEntry(
                name=_part_name(disk.path, part_num),
                size_mb=efi_mb,
                fs="vfat",
                mount="/boot/efi",
                note="EFI System Partition",
            ),
        )
        part_num += 1

    if separate_boot:
        scheme.partitions.append(
            PartitionEntry(
                name=_part_name(disk.path, part_num),
                size_mb=boot_mb,
                fs="ext4",
                mount="/boot",
                note="",
            ),
        )
        part_num += 1
    # else: /boot lives inside /; no separate partition is needed.
    # LUKS NOTE: when implementing "guided-luks-lvm", always create the
    # /boot partition here unconditionally - the bootloader must read
    # /boot before decrypting the root volume.

    if scheme_type == "guided-lvm":
        pv_mb = root_mb + swap_mb
        scheme.partitions.append(
            PartitionEntry(
                name=_part_name(disk.path, part_num),
                size_mb=pv_mb,
                fs="lvm2pv",
                mount="",
                note="LVM Physical Volume",
            ),
        )
        scheme.lvm_pv_partition = _part_name(disk.path, part_num)
        part_num += 1

        scheme.partitions.append(
            PartitionEntry(
                name=f"{scheme.lvm_vg_name}-root",
                size_mb=root_mb,
                fs="ext4",
                mount="/",
                note="LVM logical volume",
            ),
        )
        if swap_mb > 0:
            scheme.partitions.append(
                PartitionEntry(
                    name=f"{scheme.lvm_vg_name}-swap",
                    size_mb=swap_mb,
                    fs="swap",
                    mount="swap",
                    note="LVM logical volume",
                ),
            )
    else:
        scheme.partitions.append(
            PartitionEntry(
                name=_part_name(disk.path, part_num),
                size_mb=root_mb,
                fs="ext4",
                mount="/",
                note="",
            ),
        )
        part_num += 1
        if swap_mb > 0:
            scheme.partitions.append(
                PartitionEntry(
                    name=_part_name(disk.path, part_num),
                    size_mb=swap_mb,
                    fs="swap",
                    mount="swap",
                    note="",
                ),
            )

    scheme.partitions.sort(key=_partition_sort_key)
    log.debug("Calculated scheme: %s", scheme)
    return scheme


def apply_scheme(scheme: PartitionScheme) -> None:
    """Apply partition scheme to disk: write the table, set up LVM, and format.

    Steps:

    1. Write the partition table via ``sfdisk``.
    2. If LVM is required, create PV / VG / LVs.
    3. Format all filesystems.

    Args:
        scheme (PartitionScheme): Layout to apply
            - see ``.config.PartitionScheme``.
            - see also _partition_sort_key() for partition sort order.

    """
    scheme.partitions.sort(key=_partition_sort_key)
    _write_partition_table(scheme)
    _settle()

    if scheme.scheme_type == "guided-lvm" or scheme.manual_lvm:
        try:
            _setup_lvm(scheme)
            _settle()
        except Exception:
            # if set up fails, ensure no LV remnants remain
            _teardown_lvm(check=False)
            raise
        _settle()

    _format_partitions(scheme)
    _settle()


def mount_partitions(scheme: PartitionScheme, mount_root: str) -> None:
    """Mount all partitions and volumes under ``mount_root`` in correct order.

    Root is mounted first, then remaining mounts in depth order.  Swap
    is enabled last.

    Args:
        scheme (PartitionScheme): Layout to mount - see
            ``.config.PartitionScheme``.
        mount_root (str): Base path for mounts - e.g. ``"/mnt/target"``.

    Raises:
        RuntimeError: If no root partition is found in ``scheme``.

    """
    root_entry = _find_mount(scheme, "/")
    if root_entry is None:
        raise RuntimeError("No root partition in scheme.")

    os.makedirs(mount_root, exist_ok=True)
    run(["mount", _dev_path(root_entry, scheme), mount_root], destructive=True)

    others = sorted(
        [p for p in scheme.partitions if p.mount not in ("/", "swap", "")],
        key=lambda p: p.mount,
    )
    for part in others:
        mp = mount_root + part.mount
        os.makedirs(mp, exist_ok=True)
        run(["mount", _dev_path(part, scheme), mp], destructive=True)
        log.info("Mounted %s -> %s", part.mount, mp)

    for part in scheme.partitions:
        if part.is_swap():
            run(
                ["swapon", _dev_path(part, scheme)],
                check=False,
                destructive=True,
            )


def unmount_partitions(scheme: PartitionScheme, mount_root: str) -> None:
    """Unmount all partitions under ``mount_root`` in reverse order.

    Swap is disabled first, then mounts are removed deepest-first by
    reading ``/proc/mounts``.

    Args:
        scheme (PartitionScheme): Layout to unmount - see
            ``.config.PartitionScheme``.
        mount_root (str): Base path that was used for mounts - e.g.
            ``"/mnt/target"``.

    """
    for part in scheme.partitions:
        if part.is_swap():
            run(
                ["swapoff", _dev_path(part, scheme)],
                check=False,
                destructive=True,
            )

    try:
        with open("/proc/mounts") as fob:
            lines = fob.readlines()
    except OSError:
        lines = []

    mounted = sorted(
        [
            line.split()[1]
            for line in lines
            if (
                len(line.split()) >= 2
                and line.split()[1].startswith(mount_root)
            )
        ],
        reverse=True,  # deepest first
    )

    for mp in mounted:
        try:
            run(["umount", "-l", mp], destructive=True)
            log.info("Unmounted %s", mp)
        except RunError as e:
            log.warning("Could not unmount %s: %s", mp, e)

    if mount_root not in mounted:
        run(["umount", "-l", mount_root], check=False, destructive=True)


# Internal helpers
# ----------------


def _part_name(disk_path: str, num: int) -> str:
    """Return the device path for partition ``num`` on ``disk_path``.

    Args:
        disk_path (str): Base disk path - e.g. ``"/dev/sda"`` or
            ``"/dev/nvme0n1"``.
        num (int): Partition number.

    Returns:
        str: Partition device path - e.g. ``"/dev/sda1"`` or
            ``"/dev/nvme0n1p1"``.

    """
    # NVMe and devices whose name ends in a digit use a 'p' separator.
    if "nvme" in disk_path or disk_path[-1].isdigit():
        return f"{disk_path}p{num}"
    return f"{disk_path}{num}"


def _dev_path(part: PartitionEntry, scheme: PartitionScheme) -> str:
    """Resolve the ``/dev`` path for a ``PartitionEntry``.

    Handles plain partitions, LVM logical volumes, and entries that
    already contain an absolute path.

    Args:
        part (PartitionEntry): Partition or LV entry - see
            ``.config.PartitionEntry``.
        scheme (PartitionScheme): Scheme that owns ``part`` - see
            ``.config.PartitionScheme``.

    Returns:
        str: Absolute device path - e.g. ``"/dev/sda1"`` or
            ``"/dev/turnkey/root"``.

    """
    if part.name.startswith(scheme.lvm_vg_name + "-"):
        lv = part.name[len(scheme.lvm_vg_name) + 1 :]
        return f"/dev/{scheme.lvm_vg_name}/{lv}"
    if part.name.startswith("/dev/"):
        return part.name
    return f"/dev/{part.name}"


def _find_mount(
    scheme: PartitionScheme,
    mount: str,
) -> PartitionEntry | None:
    """Return the ``PartitionEntry`` for the given mount point, or ``None``.

    Args:
        scheme (PartitionScheme): Scheme to search - see
            ``.config.PartitionScheme``.
        mount (str): Mount point to find - e.g. ``"/"``.

    Returns:
        PartitionEntry | None: Matching entry, or ``None`` if not found
            - see ``.config.PartitionEntry``.

    """
    for p in scheme.partitions:
        if p.mount == mount:
            return p
    return None


def _settle() -> None:
    """Let udev and the kernel settle after partition table changes."""
    run(["udevadm", "settle"], check=False, destructive=True)
    time.sleep(0.5)


def _write_partition_table(scheme: PartitionScheme) -> None:
    """Write a GPT or MBR partition table to disk using ``sfdisk``.

    Args:
        scheme (PartitionScheme): Layout to write - see
            ``.config.PartitionScheme``.

    """
    label = "gpt" if scheme.uefi else "dos"
    lines = [f"label: {label}", ""]

    phys = [
        p
        for p in scheme.partitions
        if not p.name.startswith(scheme.lvm_vg_name + "-")
    ]

    for i, part in enumerate(phys):
        size_sectors = part.size_mb * 2048  # 512-byte sectors
        if part.is_efi():
            ptype = "U"  # EFI System
        elif part.fs == "lvm2pv":
            ptype = "31"  # LVM
        elif part.is_swap():
            ptype = "S"
        else:
            ptype = "L"  # Linux filesystem

        if i == len(phys) - 1:
            lines.append(f"type={ptype}")
        else:
            lines.append(f"size={size_sectors}, type={ptype}")

    script = "\n".join(lines) + "\n"
    log.debug("sfdisk script:\n%s", script)
    run(
        ["sfdisk", "--no-reread", "--force", scheme.disk],
        input_text=script,
        destructive=True,
        capture=True,
        timeout=60,
    )
    run(["partprobe", scheme.disk], check=False, destructive=True)


def _setup_lvm(scheme: PartitionScheme) -> None:
    """Create the LVM PV, VG, and LVs defined in ``scheme``.

    Args:
        scheme (PartitionScheme): Layout containing LVM details - see
            ``.config.PartitionScheme``.

    """
    pv = scheme.lvm_pv_partition
    vg = scheme.lvm_vg_name
    log.info("Setting up LVM: PV=%s VG=%s", pv, vg)

    run(["pvcreate", "-ff", "-y", pv], destructive=True, capture=True)
    run(["vgcreate", "-y", vg, pv], destructive=True, capture=True)

    lvs = [p for p in scheme.partitions if p.name.startswith(vg + "-")]
    for i, lv in enumerate(lvs):
        lv_name = lv.name[len(vg) + 1 :]
        if i == len(lvs) - 1:
            size_arg = ["-l", "100%FREE"]
        else:
            size_arg = ["-L", f"{lv.size_mb}M"]
        run(
            ["lvcreate", "-y", *size_arg, "-n", lv_name, vg],
            destructive=True,
            capture=True,
        )


def _format_partitions(scheme: PartitionScheme) -> None:
    """Format every partition and LV in ``scheme`` with the appropriate tool.

    Unknown filesystem types are logged as warnings and skipped.

    Args:
        scheme (PartitionScheme): Layout to format - see
            ``.config.PartitionScheme``.

    """
    for part in scheme.partitions:
        dev = _dev_path(part, scheme)
        fs = part.fs

        if fs in ("lvm2pv", ""):
            continue
        if fs == "swap":
            log.info("mkswap %s", dev)
            run(["mkswap", "-f", dev], destructive=True, capture=True)
        elif fs == "vfat":
            log.info("mkfs.vfat %s", dev)
            run(["mkfs.vfat", "-F", "32", dev], destructive=True, capture=True)
        elif fs == "ext4":
            log.info("mkfs.ext4 %s (%s)", dev, part.mount)
            run(
                ["mkfs.ext4", "-F", "-L", _label_for(part.mount), dev],
                destructive=True,
                capture=True,
            )
        elif fs == "ext2":
            run(["mkfs.ext2", "-F", dev], destructive=True, capture=True)
        elif fs == "xfs":
            run(["mkfs.xfs", "-f", dev], destructive=True, capture=True)
        elif fs == "btrfs":
            run(["mkfs.btrfs", "-f", dev], destructive=True, capture=True)
        else:
            log.warning(
                "Unknown filesystem '%s' for %s - skipping format.",
                fs,
                dev,
            )


def _label_for(mount: str) -> str:
    """Return a short filesystem label for a given mount point.

    Args:
        mount (str): Mount point - e.g. ``"/"``, ``"/boot"``.

    Returns:
        str: Label string - e.g. ``"root"``, ``"boot"``.  Unknown mount
            points return the stripped path or ``"data"`` for ``""``.

    """
    mapping = {
        "/": "root",
        "/boot": "boot",
        "/home": "home",
        "/var": "var",
        "/tmp": "tmp",
    }
    return mapping.get(mount, mount.lstrip("/") or "data")


def _teardown_lvm(check: bool = False) -> None:
    """Deactivate LVM and remove device-mapper devices.

    Called on failure to ensure the kernel releases stale LV references
    so a subsequent install attempt can run pvcreate cleanly. Will also disable
    all swap as there may be a swap LV.

    Args:
        check (bool): If ``True``, raise ``RunError`` on failure.
            Defaults to ``False`` so teardown is best-effort.

    """
    run(["swapoff", "--all"], check=check, destructive=True, capture=True)
    run(["vgchange", "-an"], check=check, destructive=True, capture=True)
    run(["dmsetup", "remove_all"], check=check, destructive=True, capture=True)
