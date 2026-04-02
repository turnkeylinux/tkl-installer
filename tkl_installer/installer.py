"""Rootfs unpacking, bootloader installation, and post-install steps."""

from __future__ import annotations

import logging
import os
import shutil
from os.path import dirname, isfile, join
from typing import Any

from .config import PartitionScheme  # noqa: TC001
from .partitioner import _dev_path
from .runner import RunError, run, run_lines, run_output

log = logging.getLogger(__name__)


def find_squashfs(file_path: str = "", strict: bool = False) -> str:
    """Locate the rootfs squashfs file and return its absolute path.

    Search order (unless ``strict=True`` with a ``file_path``):

    1. ``file_path`` argument, if given.
    2. The default TKL location
       (``/run/live/medium/live/10root.squashfs``).
    3. A range of common Debian-live paths.
    4. Any ``*.squashfs`` file mounted under ``/run``, ``/lib``, or
       ``/live`` as listed in ``/proc/mounts``.

    The first match is returned.

    Args:
        file_path (str): Explicit path to check first.  Does not require
            a ``.squashfs`` extension.  Defaults to ``""``.
        strict (bool): Only meaningful when ``file_path`` is set.  If
            ``True``, raise ``FileNotFoundError`` immediately when
            ``file_path`` does not exist rather than falling back to the
            search paths.  Defaults to ``False``.

    Returns:
        str: Absolute path to the squashfs file found.

    Raises:
        FileNotFoundError: If no squashfs file can be located, or if
            ``strict=True`` and ``file_path`` does not exist.

    """
    if file_path and strict:
        if not isfile(file_path):
            raise FileNotFoundError(
                f"Squashfs file not found: {file_path} (strict=True)",
            )
        log.info("Found squashfs at: %s", file_path)
        return file_path

    candidates = []
    if file_path:
        candidates.append(file_path)

    candidates.extend(
        [
            "/run/live/medium/live/10root.squashfs",
            "/run/live/medium/live/filesystem.squashfs",
            "/lib/live/mount/medium/live/filesystem.squashfs",
            "/cdrom/live/filesystem.squashfs",
            "/run/initramfs/live/filesystem.squashfs",
            "/live/image/live/filesystem.squashfs",
        ],
    )

    try:
        with open("/proc/mounts") as fob:
            for line in fob:
                parts = line.split()
                if len(parts) >= 2:
                    mount = parts[1]
                    if (
                        mount.endswith(".squashfs")
                        and mount.startswith("/")
                        and len(mount.split("/")) > 1
                        and mount.split("/")[1] in ("run", "lib", "live")
                    ):
                        candidates.append(mount)
    except OSError:
        pass

    for c in candidates:
        if isfile(c):
            log.info("Found squashfs at: %s", c)
            return c

    raise FileNotFoundError(
        "Could not locate the rootfs squashfs file. "
        "Please specify its path with --squashfs or in the config file.",
    )


def unpack_rootfs(squashfs_path: str, target: str) -> None:
    """Unpack a squashfs image directly into a target directory.

    Uses ``unsquashfs -dest`` so the content lands directly in ``target``
    rather than in a ``squashfs-root`` subdirectory.

    Args:
        squashfs_path (str): Path to the squashfs file.
        target (str): Destination directory; created if it does not
            exist.

    Raises:
        FileNotFoundError: If ``squashfs_path`` does not exist.

    """
    if not isfile(squashfs_path):
        raise FileNotFoundError(f"squashfs not found: {squashfs_path}")

    os.makedirs(target, exist_ok=True)
    log.info("Unpacking %s -> %s", squashfs_path, target)
    run(
        ["unsquashfs", "-f", "-d", target, squashfs_path],
        destructive=True,
        timeout=3600,
        capture=False,
    )
    log.info("Rootfs unpacked.")


def _get_uuid(dev_path: str) -> str:
    """Return the filesystem UUID of a block device, or ``""`` on failure.

    Args:
        dev_path (str): Block device path - e.g. ``"/dev/sda1"``.

    Returns:
        str: UUID string, or ``""`` if ``blkid`` cannot determine one.

    """
    try:
        return run_output(
            ["blkid", "--match-tag", "UUID", "--output", "value", dev_path],
            check=False,
        )
    except RunError:
        return ""


def generate_fstab(scheme: PartitionScheme, target: str) -> None:
    """Write ``/etc/fstab`` into the target rootfs.

    Entries are sorted root-first then by mount-point depth.  Swap
    entries are appended last.  UUID-based references are used where
    available.

    Args:
        scheme (PartitionScheme): Resolved partition layout - see
            ``.config.PartitionScheme``.
        target (str): Path to the installed OS root - e.g.
            ``"/mnt/target"``.

    """
    fstab_path = join(target, "etc", "fstab")
    os.makedirs(dirname(fstab_path), exist_ok=True)

    lines = [
        "# /etc/fstab - generated by tkl-installer",
        "# <file system>  <mount point>  <type>  <options>  <dump>  <pass>",
        "",
    ]

    ordered = sorted(
        [p for p in scheme.partitions if p.mount not in ("", "swap")],
        key=lambda p: (len(p.mount), p.mount),
    )
    swaps = [p for p in scheme.partitions if p.is_swap()]

    for part in ordered + swaps:
        dev = _dev_path(part, scheme)
        uuid = _get_uuid(dev)
        spec = f"UUID={uuid}" if uuid else dev

        if part.is_swap():
            lines.append(f"{spec}  none  swap  sw  0  0")
        elif part.is_efi():
            lines.append(f"{spec}  {part.mount}  vfat  umask=0077  0  1")
        elif part.mount == "/":
            lines.append(f"{spec}  /  {part.fs}  errors=remount-ro  0  1")
        else:
            lines.append(f"{spec}  {part.mount}  {part.fs}  defaults  0  2")

    content = "\n".join(lines) + "\n"
    log.debug("fstab:\n%s", content)

    with open(fstab_path, "w") as fob:
        fob.write(content)
    log.info("Wrote fstab: %s", fstab_path)


def install_grub(scheme: PartitionScheme, target: str) -> None:
    """Install and configure GRUB inside the target rootfs chroot.

    Handles both UEFI (``grub-efi-amd64``) and legacy BIOS
    (``grub-pc``).  The appropriate GRUB package must already be
    installed in the rootfs for UEFI systems.

    Args:
        scheme (PartitionScheme): Resolved partition layout - see
            ``.config.PartitionScheme``.
        target (str): Path to the installed OS root - e.g.
            ``"/mnt/target"``.

    """
    disk = scheme.disk
    _bind_mounts(target)
    try:
        if scheme.uefi:
            _install_grub_uefi(target)
        else:
            _install_grub_bios(target, disk)
        _update_grub(target)
    finally:
        _unbind_mounts(target)


def _chroot(target: str, cmd: list[str], **kwargs: Any) -> None:  # noqa: ANN401
    """Run a command inside the target chroot.

    Args:
        target (str): Chroot root path.
        cmd (list[str]): Command to run inside the chroot.
        **kwargs: Additional keyword arguments forwarded to ``run()``.

    """
    run(["chroot", target, *cmd], destructive=True, timeout=120, **kwargs)


def _install_grub_uefi(target: str) -> None:
    """Install GRUB for UEFI inside the chroot.

    Args:
        target (str): Chroot root path.

    """
    log.info("Installing GRUB (EFI)")
    efi_dir = os.path.join(target, "boot", "efi")
    os.makedirs(efi_dir, exist_ok=True)
    _chroot(
        target,
        [
            "grub-install",
            "--target=x86_64-efi",
            "--efi-directory=/boot/efi",
            "--bootloader-id=debian",
            "--recheck",
        ],
        capture=True,
    )


def _install_grub_bios(target: str, disk: str) -> None:
    """Install GRUB for legacy BIOS inside the chroot.

    Args:
        target (str): Chroot root path.
        disk (str): Target disk device path - e.g. ``"/dev/sda"``.

    """
    log.info("Installing GRUB (BIOS) to %s", disk)
    _chroot(
        target,
        [
            "grub-install",
            "--target=i386-pc",
            "--recheck",
            disk,
        ],
        capture=True,
    )


def _update_grub(target: str) -> None:
    """Run ``update-grub`` inside the chroot.

    Args:
        target (str): Chroot root path.

    """
    log.info("Running update-grub")
    _chroot(target, ["update-grub"], capture=True)


def _bind_mounts(target: str) -> None:
    """Bind required mount points into the chroot.

    Mount points are: ``/dev``, ``/proc``, ``/sys``, ``/run``, and ``devpts``.

    Args:
        target (str): Chroot root path.

    """
    for src, rel in [
        ("/dev", "dev"),
        ("/proc", "proc"),
        ("/sys", "sys"),
        ("/run", "run"),
    ]:
        dst = os.path.join(target, rel)
        os.makedirs(dst, exist_ok=True)
        run(["mount", "--bind", src, dst], destructive=True, check=False)
    devpts = os.path.join(target, "dev", "pts")
    os.makedirs(devpts, exist_ok=True)
    run(
        [
            "mount",
            "--types",
            "devpts",
            "devpts",
            devpts,
            "--options",
            "gid=5,mode=620",
        ],
        destructive=True,
        check=False,
    )


def _unbind_mounts(target: str) -> None:
    """Lazily unmount all bind mounts created by ``_bind_mounts``.

    Args:
        target (str): Chroot root path.

    """
    for rel in ["dev/pts", "dev", "proc", "sys", "run"]:
        mp = os.path.join(target, rel)
        run(["umount", "--lazy", mp], check=False, destructive=True)


def run_extra_commands(commands: list[str], target: str) -> None:
    """Run each command inside the chroot via ``/bin/bash -c``.

    Failures are logged as warnings but do not abort the install.

    Args:
        commands (list[str]): Shell commands to run - one per item.
        target (str): Chroot root path.

    """
    if not commands:
        return

    _bind_mounts(target)
    try:
        for cmd in commands:
            log.info("Extra command: %s", cmd)
            try:
                run(
                    ["chroot", target, "/bin/bash", "-c", cmd],
                    destructive=True,
                    timeout=300,
                    capture=True,
                )
            except RunError as e:
                log.warning("Extra command failed (continuing): %s", e)
    finally:
        _unbind_mounts(target)


def copy_installer_log(log_path: str, target: str) -> None:
    """Copy the installer log file into the installed system.

    Non-fatal: logs a warning on failure rather than aborting.

    Args:
        log_path (str): Path of the installer log file.
        target (str): Installed OS root path - e.g. ``"/mnt/target"``.

    """
    if not log_path or not os.path.isfile(log_path):
        log.warning("Installer log not found at %s - skipping copy.", log_path)
        return
    dest_dir = os.path.join(target, "var", "log")
    dest = os.path.join(dest_dir, "min-installer.log")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(log_path, dest)
        log.info("Installer log copied to %s", dest)
    except OSError as exc:
        log.warning("Could not copy installer log to target: %s", exc)


def prepare_live_media_for_reboot() -> None:
    """Attempt to unmount or eject live media so the system can reboot cleanly.

    Best-effort; non-fatal.  Calls ``sync`` then attempts to eject any
    CD/DVD drives found under ``/dev/sr*``.
    """
    run(["sync"], check=False, destructive=True)
    try:
        cdrom_devs = run_lines(["find", "/dev", "-name", "sr*", "-type", "b"])
        for dev in cdrom_devs:
            run(["eject", dev], check=False, destructive=True)
            log.info("Ejected %s", dev)
    except Exception as e:  # noqa: BLE001
        log.debug("eject attempt: %s", e)
