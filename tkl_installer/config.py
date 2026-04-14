"""Configuration dataclasses and TOML loader.

The ``InstallerConfig`` is the single source of truth that flows through every
stage of the install.  It can be populated from CLI args, a TOML file, or
interactive prompts - all three can be mixed.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from os.path import exists

log = logging.getLogger(__name__)

# Remove tkl-installer and deps from installed system as last step. This will
# run before any additional 'extra_commands' (in config file).
rm_installer = "apt-get purge --yes --autoremove tkl-installer live-*"


@dataclass
class PartitionEntry:
    """A single partition or LVM logical volume.

    Attributes:
        name (str): Partition or LV name - e.g. ``"sda1"`` or
            ``"turnkey-root"``.
        size_mb (int): Size in MiB.
        fs (str): Filesystem type - e.g. ``"ext4"``, ``"vfat"``,
            ``"swap"``, ``"lvm2pv"``.
        mount (str): Mount point - e.g. ``"/"``, ``"/boot"``,
            ``"/boot/efi"``, or ``"swap"``.
        note (str): Human-readable note shown in the partition table
            preview.  Optional, defaults to ``""``.

    """

    name: str
    size_mb: int
    fs: str
    mount: str
    note: str = ""

    def is_swap(self) -> bool:
        """Return ``True`` if this entry is a swap partition or LV."""
        return self.mount == "swap"

    def is_efi(self) -> bool:
        """Return ``True`` if this entry is an EFI System Partition."""
        return self.mount in ("/boot/efi", "/efi")


@dataclass
class PartitionScheme:
    """The fully resolved partition and volume layout for a single disk.

    Built by ``partitioner.calculate_default_scheme()`` and optionally
    adjusted by the manual partitioning flow before being passed to
    ``partitioner.apply_scheme()``.

    Attributes:
        disk (str): Target disk device path - e.g. ``"/dev/sda"``.
        scheme_type (str): One of ``"guided"``, ``"guided-lvm"``, or
            ``"manual"``.
        uefi (bool): ``True`` if the target system boots via UEFI.
        separate_boot (bool): ``True`` if ``/boot`` is a dedicated
            partition; ``False`` if ``/boot`` lives inside ``/``.
            Always ``True`` for future LUKS schemes - see LUKS NOTE
            comments in ``partitioner.py``.
        manual_lvm (bool): ``True`` when a manual scheme uses LVM.
            Ignored for ``guided`` and ``guided-lvm`` scheme types.
        partitions (list[PartitionEntry]): Ordered list of physical
            partitions followed by any LVM logical volumes - see
            ``.config.PartitionEntry``.
        lvm_vg_name (str): LVM volume group name.  Populated when
            ``scheme_type == "guided-lvm"`` or ``manual_lvm`` is
            ``True``.  Defaults to ``"turnkey"``.
        lvm_pv_partition (str): Device path of the LVM physical volume
            partition - e.g. ``"/dev/sda3"``.  Populated alongside
            ``lvm_vg_name``.

    """

    disk: str
    scheme_type: str
    uefi: bool
    separate_boot: bool = False
    manual_lvm: bool = False
    partitions: list[PartitionEntry] = field(default_factory=list)

    # LVM details - populated when scheme_type == "guided-lvm" or manual_lvm
    lvm_vg_name: str = "turnkey"
    lvm_pv_partition: str = ""


@dataclass
class ManualPartitionConfig:
    """Size overrides for manual partitioning.

    Sourced from ``[manual-partition]`` in the TOML config.

    All sizes are in MiB.  A value of ``0`` means "use the calculated
    default".  LVM fields are only applied when ``lvm = true``.

    Attributes:
        lvm (bool | None): ``True`` = use LVM; ``False`` = plain GPT
            partitions; ``None`` = ask interactively.
        efi_mb (int): EFI System Partition size in MiB.  Only used on
            UEFI systems.  ``0`` = use default (512 MiB).
        boot_mb (int): ``/boot`` partition size in MiB.  Only used when
            ``separate_boot`` is ``True``.  ``0`` = use default
            (1024 MiB).
        swap_mb (int): Swap partition size in MiB (plain layout only).
            ``0`` = calculated from disk size and available RAM.
        pv_mb (int): LVM Physical Volume partition size in MiB.
            ``0`` = remainder of disk after other partitions.
        lv_root_mb (int): Root (``/``) logical volume size in MiB.
            ``0`` = remainder of the VG after swap.
        lv_swap_mb (int): Swap logical volume size in MiB.
            ``0`` = calculated from disk size and available RAM.

    """

    lvm: bool | None = None

    # GPT / physical partition sizes
    efi_mb: int = 0
    boot_mb: int = 0

    # Plain (non-LVM) sizes
    swap_mb: int = 0

    # LVM PV size
    pv_mb: int = 0

    # LVM logical volume sizes
    lv_root_mb: int = 0
    lv_swap_mb: int = 0


@dataclass
class InstallerConfig:
    """Top-level installer configuration.

    All attributes default to a "not yet set" sentinel so that the
    interactive wizard only asks for values that have not been provided
    via CLI or config file.

    Attributes:
        squashfs_path (str): Path to the rootfs squashfs file.  Auto-
            detected from live media when empty.
        mount_root (str): Directory under which the target filesystem is
            mounted during install.  Defaults to ``"/mnt/target"``.
        disk (str): Target block device path - e.g. ``"/dev/sda"``.
            Auto-detected or asked interactively when empty.
        wipe_disk (bool): Set internally once wipe consent is confirmed.
        force_wipe (bool): Wipe the disk without prompting even when it
            contains existing data.  Required in unattended mode when the
            disk is not empty.
        scheme_type (str): Partitioning scheme - one of ``"guided"``,
            ``"guided-lvm"``, or ``"manual"``.  Asked interactively when
            empty.
        separate_boot (bool | None): ``True`` = dedicated ``/boot``
            partition; ``False`` = ``/boot`` inside ``/``;
            ``None`` = ask interactively.
        manual_lvm (bool | None): ``True`` = use LVM for a manual layout;
            ``None`` = ask interactively.  Ignored for guided schemes.
        manual_partition (ManualPartitionConfig): Size overrides for
            manual partitioning - see ``.config.ManualPartitionConfig``.
        scheme (PartitionScheme | None): Resolved partition layout, set
            after the interactive wizard completes - see
            ``.config.PartitionScheme``.
        extra_commands (list[str]): Shell commands run inside the chroot
            after installation.  Each item is passed to ``/bin/bash -c``.
            Must be in a config file (not asked & no switch).
        reboot_after (bool | None): ``True`` = reboot automatically;
            ``False`` = return to live environment; ``None`` = ask.
        dry_run (bool): Log all destructive operations without executing
            them.
        verbose (bool): Enable verbose/debug logging to stderr.
        unattended (bool): Never ask questions interactively.  Uses safe
            defaults where possible; fails if mandatory options are
            missing or ambiguous.
        config_file (str): Path of the TOML config file that was loaded,
            if any.

    """

    # TODO: ensure that CLI args > Config file > defaults

    # paths
    squashfs_path: str = ""
    mount_root: str = "/mnt/target"

    # disk
    disk: str = ""
    wipe_disk: bool = False
    force_wipe: bool = False

    # scheme
    scheme_type: str = ""
    separate_boot: bool | None = None  # None = ask interactively
    manual_lvm: bool | None = None  # None = ask interactively (manual only)
    manual_partition: ManualPartitionConfig = field(
        default_factory=ManualPartitionConfig,
    )
    scheme: PartitionScheme | None = None

    # post-install
    extra_commands: list[str] = field(default_factory=list)
    reboot_after: bool | None = None

    # misc
    dry_run: bool = False
    verbose: bool = False
    unattended: bool = False
    config_file: str = ""

    def __post_init__(self) -> None:
        """Remove installer before any other post install commands."""
        self.extra_commands.insert(0, rm_installer)


# TOML loader
# -----------

# Top-level keys recognised in the config file.
_TOP_LEVEL_KEYS = {
    "squashfs_path",
    "mount_root",
    "disk",
    "wipe_disk",
    "force_wipe",
    "scheme_type",
    "separate_boot",
    "dry_run",
    "verbose",
    "unattended",
    "reboot_after",
    "extra_commands",
    "manual-partition",
}

# Keys recognised inside [manual-partition].
_MANUAL_PARTITION_KEYS = {
    "lvm",
    "efi_mb",
    "boot_mb",
    "swap_mb",
    "pv_mb",
    "lv_root_mb",
    "lv_swap_mb",
}


def load_toml(path: str) -> InstallerConfig:
    """Load an ``InstallerConfig`` from a TOML file.

    Only keys present in the file are applied; everything else keeps its
    dataclass default.  Unknown keys emit a warning rather than an error
    so that future config extensions do not break older installer versions.

    Args:
        path (str): Path to the TOML configuration file.

    Returns:
        InstallerConfig: Populated config object - see
            ``.config.InstallerConfig``.

    Raises:
        FileNotFoundError: If ``path`` does not exist.

    """
    if not exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "rb") as fob:
        data = tomllib.load(fob)

    cfg = InstallerConfig()

    # --- top-level scalar fields ---
    _apply(cfg, data, "squashfs_path", str)
    _apply(cfg, data, "mount_root", str)
    _apply(cfg, data, "disk", str)
    _apply(cfg, data, "wipe_disk", bool)
    _apply(cfg, data, "force_wipe", bool)
    _apply(cfg, data, "scheme_type", str)
    _apply(cfg, data, "separate_boot", bool)
    _apply(cfg, data, "dry_run", bool)
    _apply(cfg, data, "verbose", bool)
    _apply(cfg, data, "unattended", bool)
    _apply(cfg, data, "reboot_after", bool)

    if "extra_commands" in data:
        val = data["extra_commands"]
        if isinstance(val, list) and all(isinstance(c, str) for c in val):
            cfg.extra_commands = val
        else:
            log.warning(
                "extra_commands in config must be a list of strings; ignored.",
            )

    # --- [manual-partition] section ---
    mp_data = data.get("manual-partition")
    if mp_data is not None:
        if not isinstance(mp_data, dict):
            log.warning("[manual-partition] must be a TOML table; ignored.")
        else:
            mp = cfg.manual_partition
            _apply(mp, mp_data, "lvm", bool)
            # Propagate lvm flag to top-level manual_lvm for interactive.py.
            if mp.lvm is not None:
                cfg.manual_lvm = mp.lvm
            _apply_int(mp, mp_data, "efi_mb")
            _apply_int(mp, mp_data, "boot_mb")
            _apply_int(mp, mp_data, "swap_mb")
            _apply_int(mp, mp_data, "pv_mb")
            _apply_int(mp, mp_data, "lv_root_mb")
            _apply_int(mp, mp_data, "lv_swap_mb")

            for key in mp_data:
                if key not in _MANUAL_PARTITION_KEYS:
                    log.warning(
                        "[manual-partition] unknown key '%s' - ignored.",
                        key,
                    )

    # --- warn about unknown top-level keys ---
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            log.warning("Unknown config key '%s' - ignored.", key)

    log.debug("Loaded config from %s", path)
    return cfg


def _apply(obj: object, data: dict, key: str, typ: type) -> None:
    """Set ``obj.key`` from ``data[key]`` if present and the correct type.

    Args:
        obj (object): Target object to update.
        data (dict): Source dictionary.
        key (str): Key to look up in ``data`` and set on ``obj``.
        typ (type): Expected Python type; mismatches are logged and
            ignored.

    """
    if key not in data:
        return
    val = data[key]
    if not isinstance(val, typ):
        log.warning(
            "Config key '%s' should be %s, got %s; ignored.",
            key,
            typ.__name__,
            type(val).__name__,
        )
        return
    setattr(obj, key, val)


def _apply_int(obj: object, data: dict, key: str) -> None:
    """Set ``obj.key`` from ``data[key]`` if present; accepts int or float.

    Float values are truncated to ``int``.

    Args:
        obj (object): Target object to update.
        data (dict): Source dictionary.
        key (str): Key to look up in ``data`` and set on ``obj``.

    """
    if key not in data:
        return
    val = data[key]
    if not isinstance(val, (int, float)):
        log.warning(
            "Config key '%s' should be a number, got %s; ignored.",
            key,
            type(val).__name__,
        )
        return
    setattr(obj, key, int(val))
