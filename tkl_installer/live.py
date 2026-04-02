"""Live environment detection.

Checks several independent heuristics and requires at least one to pass.
A hard failure here aborts the install to prevent accidentally running on
an already-installed system.
"""

from __future__ import annotations

import logging

from .ui_wrapper import fatal

log = logging.getLogger(__name__)

# TODO: double check TKL defaults when running live and adjust as relevant

# Paths that should only exist on live system - tkl default: "/run/live"
_LIVE_PATHS = ("/run/live", "/run/initramfs/live", "/live")

# Live system related kernel cmdline keywords
_LIVE_CMDLINE_KEYWORDS = (
    "boot=live",  # tkl default
    "initrd=/live/initrd.gz",  # tkl default
    "live-media",
    "toram",
    "fromiso",
)

# Live system '/' mount types (overlay filesystem) - tkl default: "overlay"
_OVERLAY_FS = ("overlay", "aufs", "overlayfs")


def is_live_system() -> bool:
    """Return ``True`` if the system appears to be a live environment.

    Performs four independent tests:

    - "/" is mounted as an overlay filesystem (see _OVERLAY_FS)
    - A "iso9660" file system is mounted within an expected live system path
      (see _LIVE_PATHS)
    - A "squashfs" filesystem is mounted within an expected live system path
      (see _LIVE_PATHS)
    - At least one of the expected kernel cmdline keywords occurs (see
      _LIVE_CMDLINE_KEYWORDS)

    These test are specifically developed to ensure a TurnKey Live ISO is
    detected, but will likely work with offical debian-live ISOs and
    possibly other live ISOs too.

    Returns:
        bool: ``True`` only if all four tests pass.

    """
    root_test = False
    iso_test = False
    squashfs_test = False
    cmdline_test = False

    # mount tests
    try:
        with open("/proc/mounts") as fob:
            for mnt_src, mnt_pt, mnt_type, *_ in map(
                str.split,
                fob.readlines(),
            ):
                if mnt_pt == "/" and mnt_src in _OVERLAY_FS:
                    root_test = True
                    log.debug("/ mounted as %s - likely live", mnt_src)
                elif any(mnt_pt.startswith(live_p) for live_p in _LIVE_PATHS):
                    _debug_msg = "%s is %s - likely live"
                    if mnt_type == "iso9660":
                        iso_test = True
                        log.debug(_debug_msg, mnt_pt, mnt_type)
                    elif mnt_type == "squashfs":
                        squashfs_test = True
                        log.debug(_debug_msg, mnt_pt, mnt_type)
    except (OSError, ValueError):
        pass

    # kernel cmdline test
    try:
        with open("/proc/cmdline") as fob:
            for cmdline_kw in fob.read().lower().split():
                if cmdline_kw in _LIVE_CMDLINE_KEYWORDS:
                    cmdline_test = True
                    log.debug("Live cmdline keyword found: %s", cmdline_kw)
                    break
    except OSError:
        pass

    passed = root_test and iso_test and squashfs_test and cmdline_test
    log.info("Live detected: %s", passed)
    return passed


def assert_live_system() -> None:
    """Abort with a clear error if not running in a live environment.

    Calls ``ui_wrapper.fatal()`` (exit code 2) if ``is_live_system()``
    returns ``False``.
    """
    if not is_live_system():
        fatal(
            "This installer must be run from a live system.\n"
            "    It does not appear to be running in a live environment.\n"
            "    Refusing to continue to protect existing data.",
            code=2,
        )
    log.info("Live system confirmed.")


def get_ram_mb() -> int:
    """Return total system RAM in MiB, or ``0`` if it cannot be determined.

    Returns:
        int: RAM in MiB, or ``0`` on read failure.

    """
    try:
        with open("/proc/meminfo") as fob:
            for line in fob:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except OSError:
        pass
    return 0
