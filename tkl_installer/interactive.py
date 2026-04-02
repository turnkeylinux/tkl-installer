"""Interactive installation wizard.

Steps the user through each installation decision, populating an
``InstallerConfig`` as it goes.  Any value already set via CLI or config
file is skipped.  When ``cfg.unattended`` is ``True`` no questions are
asked - safe defaults are used or a fatal error is raised.
"""

# import from __future__ to enable lazy evaluation
from __future__ import annotations

import logging
import os

from .config import InstallerConfig, PartitionEntry, PartitionScheme
from .disks import (
    DiskInfo,
    _disk_info_sort_key,
    detect_uefi,
    get_candidate_disks,
    human_size,
    probe_disks,
    validate_disk_size,
)
from .installer import find_squashfs
from .live import assert_live_system, get_ram_mb
from .partitioner import (
    BOOT_MB,
    MIN_ROOT_MB,
    _partition_sort_key,
    _teardown_lvm,
    calculate_default_scheme,
)
from .ui_wrapper import UI

log = logging.getLogger(__name__)

# minimum swap to warn about when RAM is low (MiB)
LOW_SWAP_WARN_MB = 512
LOW_RAM_WARN_MB = 1024
ui = UI()

# any free disk space not part of a partition after interactive manual
# partition setup will raise a warning
_FREE_SPACE_WARN_THRESHOLD_MB = 1024  # 1 GiB

# top-level entry point


def run_interactive(cfg: InstallerConfig) -> InstallerConfig:
    """Walk the user through all installation decisions.

    Each step is skipped if the relevant value is already populated in
    ``cfg`` via CLI or config file.  When ``cfg.unattended`` is ``True``
    no questions are ever asked - missing mandatory values cause a fatal
    error and optional values fall back to safe defaults.

    Args:
        cfg (InstallerConfig): Partially or fully populated config - see
            ``.config.InstallerConfig``.

    Returns:
        InstallerConfig: Fully populated config, ready for the install
            stages - see ``.config.InstallerConfig``.

    """
    ui.app_start()

    # Convenience: unattended mode helper - fail with a clear message instead
    # of asking a question.
    def _unattended_fatal(option: str) -> None:
        ui.fatal(
            f"Unattended mode: '{option}' must be set via"
            f" --{option.replace('_', '-')} or config file.",
        )

    # step 0: live check
    ui.header("System Check")
    assert_live_system()
    ui.ok("Running in a live environment.")

    uefi = detect_uefi()
    ui.ok(f"Boot mode: {'UEFI' if uefi else 'Legacy BIOS'}")

    # step 1: locate squashfs
    ui.header("Locating Installation Source")
    if not cfg.squashfs_path:
        if cfg.unattended:
            _unattended_fatal("squashfs_path")
        try:
            cfg.squashfs_path = find_squashfs()
            ui.ok(f"Found rootfs: {cfg.squashfs_path}")
        except FileNotFoundError as e:
            ui.fatal(str(e))
    else:
        if not os.path.isfile(cfg.squashfs_path):
            ui.fatal(f"squashfs not found at: {cfg.squashfs_path}")
        ui.ok(f"Using rootfs: {cfg.squashfs_path}")

    # step 2: disk selection
    ui.header("Disk Selection")
    ui.step("Probing block devices...")
    all_disks = probe_disks()
    candidates = get_candidate_disks(all_disks)

    if not candidates:
        ui.fatal(
            "No suitable installation target disks found.\n"
            "    (The live device is excluded; check that a drive is"
            " connected.)",
        )

    disk: DiskInfo | None = None

    if cfg.disk:
        # validate the pre-configured disk
        found = next((d for d in candidates if d.path == cfg.disk), None)
        if found is None:
            ui.fatal(
                f"Specified disk '{cfg.disk}' not found or is the live"
                " device.",
            )
        disk = found
        ui.ok(
            f"Using pre-configured disk: {disk.path}"
            " ({human_size(disk.size_mb)})",
        )
    elif len(candidates) == 1:
        disk = candidates[0]
        ui.ok(f"Single disk found: {disk.path} ({human_size(disk.size_mb)})")
    elif cfg.unattended:
        # Multiple candidates and no disk pre-configured - cannot choose.
        ui.fatal(
            "Unattended mode: multiple disks found and no --disk specified.\n"
            "    Available: "
            + ", ".join(
                f"{d.path} ({human_size(d.size_mb)})" for d in candidates
            ),
        )
    elif _show_disk_table(candidates):
        # TODO - this should probably be a radio dialog.
        disk_path = ui.choose_from_list(
            "Which disk should be used for installation?",
            [
                f"{d.path}  -  {human_size(d.size_mb)}  {d.model}"
                for d in candidates
            ],
        ).split()[0]
        disk = next(d for d in candidates if d.path == disk_path)

    if disk is None:
        ui.fatal("Disk is not configured")

    # size validation
    ok, err = validate_disk_size(disk, uefi)
    if not ok:
        ui.fatal(err)

    cfg.disk = disk.path

    # warn if LVM PV
    if disk.is_lvm_pv:
        ui.warn("This disk contains LVM physical volumes.")

    # confirm wipe if disk has existing data
    needs_wipe = bool(disk.partitions) or disk.is_lvm_pv
    if needs_wipe:
        if cfg.force_wipe:
            # Explicit permission granted - proceed without asking.
            cfg.wipe_disk = True
            ui.warn(
                f"Disk {disk.path} has existing data; wiping as requested"
                " (--force-wipe).",
            )
        elif cfg.unattended:
            # No permission and no way to ask - this is a fatal error.
            ui.fatal(
                f"Unattended mode: disk {disk.path} has existing data but"
                " --force-wipe was not set.  Aborting to prevent data loss.",
            )
        elif not cfg.wipe_disk:
            ui.warn(
                f"Disk {disk.path} appears to have existing data/partitions.",
            )
            cfg.wipe_disk = ui.confirm(
                f"All data on {disk.path} will be DESTROYED. Continue?",
                default=False,
            )
            if not cfg.wipe_disk:
                ui.fatal("Aborting at user request.", code=0)
            if disk.is_lvm_pv:
                _teardown_lvm()
    else:
        cfg.wipe_disk = True  # empty disk; still need to write table

    # steps 3 & 4: partition scheme type + preview (loop allows going back)
    while True:
        ui.header("Partitioning Scheme")

        if not cfg.scheme_type:
            if cfg.unattended:
                _unattended_fatal("scheme_type")
            cfg.scheme_type = ui.choose(
                "Choose a partitioning scheme:",
                [
                    ("guided-lvm", "Guided LVM - LVM logical volumes"),
                    ("guided", "Guided - simple ext4 partitions"),
                    ("manual", "Manual - review and adjust partition sizes"),
                ],
                default=0,
            )

        # Ask about /boot placement if not already set via config/CLI.
        # Default: /boot inside / (separate_boot=False).
        # LUKS NOTE: when adding "guided-luks-lvm", skip this question and
        # force separate_boot=True - /boot must be unencrypted for the
        # bootloader.
        if cfg.separate_boot is None:
            if cfg.unattended:
                cfg.separate_boot = False  # default: /boot inside /
                ui.step(
                    "Unattended: separate_boot not set; defaulting to False.",
                )
            else:
                cfg.separate_boot = ui.confirm(
                    "Create a separate /boot partition?\n\n"
                    "If 'No' is selected (recommended):\n\n"
                    "  /boot will be a subdirectory of / - simpler and saves"
                    " space",
                    default=False,
                )

        scheme = calculate_default_scheme(
            disk,
            uefi,
            cfg.scheme_type,
            separate_boot=cfg.separate_boot,
        )

        if cfg.scheme_type == "manual":
            # manual always goes straight to the editor
            scheme = _manual_scheme(scheme, disk, unattended=cfg.unattended)
            break

        # show the auto-calculated scheme; "Back" returns to the chooser
        if cfg.unattended or _show_scheme(scheme):
            break
        # user clicked Back - loop and re-show the scheme chooser
        # Reset scheme_type so the chooser is shown again next iteration.
        cfg.scheme_type = ""

    # swap size warning
    _warn_swap(scheme)

    cfg.scheme = scheme

    # step 5: final confirmation
    ui.header("Ready to Install")
    ui.step(f"Target disk   : {cfg.disk}")
    ui.step(f"Scheme        : {cfg.scheme_type}")
    ui.step(f"Rootfs        : {cfg.squashfs_path}")
    ui.warn(f"This will ERASE ALL DATA on {cfg.disk}.")
    if not cfg.unattended and not ui.confirm(
        "Begin installation?",
        default=False,
    ):
        ui.fatal("Aborting at user request.", code=0)

    return cfg


# helper funcs


def _show_disk_table(disks: list[DiskInfo]) -> bool:
    """Display a table of available disks and ask the user to confirm.

    Args:
        disks (list[DiskInfo]): Disks to display - see
            ``.disks.DiskInfo``.

    Returns:
        bool: ``True`` if the user accepted; ``False`` otherwise.

    """
    rows: list[tuple[str, ...]] = []
    disks.sort(key=_disk_info_sort_key)
    for d in disks:
        status = []
        if d.partitions:
            status.append(f"{len(d.partitions)} part(s)")
        if d.is_lvm_pv:
            status.append("LVM PV")
        rows.append(
            (
                d.path,
                human_size(d.size_mb),
                d.model or "-",
                ", ".join(status) or "empty",
            ),
        )
    return ui.show_table(rows, headers=("Device", "Size", "Model", "Status"))


def _show_scheme(scheme: PartitionScheme, disk_size_mb: int = 0) -> bool:
    """Display partition scheme table and ask the user to accept or go back.

    Args:
        scheme (PartitionScheme): Scheme to display - see
            ``.config.PartitionScheme``.
        disk_size_mb (int): Total disk size in MiB.  When non-zero, a
            note is shown if significant space (>= 1 GiB) would be left
            unallocated.  ``0`` disables the check.  Defaults to ``0``.

    Returns:
        bool: ``True`` if the user accepted (OK); ``False`` if they
            pressed Back.

    """
    rows: list[tuple[str, ...]] = []
    scheme.partitions.sort(key=_partition_sort_key)
    for p in scheme.partitions:
        size_str = human_size(p.size_mb)
        rows.append((p.name, size_str, p.fs, p.mount or "-", p.note or ""))

    footer = "\nProceed with this partition scheme?"
    if disk_size_mb:
        used_mb = sum(p.size_mb for p in scheme.partitions)
        free_mb = disk_size_mb - used_mb
        if free_mb >= _FREE_SPACE_WARN_THRESHOLD_MB:
            footer = (
                f"Note: {human_size(free_mb)} of disk space will be left"
                " unallocated."
            )

    return ui.show_table(
        rows,
        headers=("Name", "Size", "FS", "Mount", "Notes"),
        no_label="Back",
        footer=footer,
    )


def _manual_scheme(
    scheme: PartitionScheme,
    disk: DiskInfo,
    unattended: bool = False,
) -> PartitionScheme:
    """Walk the user through configuring a manual partition layout.

    Asks whether to use LVM (unless already decided), then collects GPT
    partition sizes in one form.  For LVM layouts a second form collects
    LV sizes.  The last physical data partition (root or LVM PV) absorbs
    remaining disk space; the root LV absorbs remaining VG space.

    Args:
        scheme (PartitionScheme): Initial scheme used as size defaults -
            see ``.config.PartitionScheme``.
        disk (DiskInfo): Target disk - see ``.disks.DiskInfo``.
        unattended (bool): If ``True``, skip all interactive prompts and
            use calculated defaults.  Defaults to ``False``.

    Returns:
        PartitionScheme: User-adjusted layout - see
            ``.config.PartitionScheme``.

    """
    ui.header("Manual Partition Configuration")

    # --- Ask about LVM if not already decided ---
    if scheme.manual_lvm is None or scheme.scheme_type != "manual":
        if unattended:
            scheme.manual_lvm = False  # default: plain GPT
            ui.step("Unattended: manual_lvm not set; defaulting to False.")
        else:
            # Only ask once; store on the scheme so _return_user preserves it.
            scheme.manual_lvm = ui.confirm(
                "Use LVM (Logical Volume Manager) for this layout?\n"
                "(Yes = flexible LV resizing; No = simple GPT partitions.)",
                default=False,
            )

    use_lvm = scheme.manual_lvm

    # Rebuild the scheme's partition list to match the chosen layout type so
    # that the editable lists below reflect the correct structure.  We call
    # calculate_default_scheme with the appropriate pseudo-scheme_type so the
    # right partitions are generated as starting defaults.
    effective_type = "guided-lvm" if use_lvm else "guided"
    base = calculate_default_scheme(
        disk,
        scheme.uefi,
        effective_type,
        separate_boot=scheme.separate_boot,
    )
    # Keep scheme_type as "manual" and carry LVM flag forward.
    base.scheme_type = "manual"
    base.manual_lvm = use_lvm
    scheme = base

    vg = scheme.lvm_vg_name

    def _return_user() -> PartitionScheme:
        """Reset to default sizes and re-enter ``_manual_scheme``."""
        fresh = calculate_default_scheme(
            disk,
            scheme.uefi,
            effective_type,
            separate_boot=scheme.separate_boot,
        )
        fresh.scheme_type = "manual"
        fresh.manual_lvm = use_lvm
        return _manual_scheme(fresh, disk, unattended=unattended)

    # Separate physical partitions from LVs.
    phys = [p for p in scheme.partitions if not p.name.startswith(vg + "-")]
    lvs = [p for p in scheme.partitions if p.name.startswith(vg + "-")]

    # --- GPT / physical partition size form ---
    # Editable physical partitions: everything except the last data partition.
    # For plain layouts the last partition is root (/); it absorbs the
    # remainder.
    # For LVM layouts the LVM PV is the last physical partition and absorbs
    # the remainder - so EFI and /boot (if present) are the editable ones.
    if use_lvm:
        phys_editable = [p for p in phys if p.fs != "lvm2pv"]
    else:
        phys_editable = [
            p for p in phys if not (p.mount in ("/", "") and p.fs != "lvm2pv")
        ]

    # LV size form (LVM only): swap is editable; root absorbs remainder.
    lv_editable = [lv for lv in lvs if lv.is_swap()] if use_lvm else []

    gpt_form = [
        (p.name, p.fs, p.mount or "-", p.size_mb) for p in phys_editable
    ]
    lv_form = [
        (lv.name, lv.fs, lv.mount or "-", lv.size_mb) for lv in lv_editable
    ]

    while True:
        # --- Step A: GPT partition sizes ---
        if unattended:
            # Use defaults from the scheme as-is; no form shown.
            new_gpt_sizes = [p.size_mb for p in phys_editable]
        else:
            _user_new_gpt_sizes = ui.prompt_sizes_mb(gpt_form, disk.size_mb)
            if _user_new_gpt_sizes is None:
                return _return_user()
            new_gpt_sizes = _user_new_gpt_sizes

        new_phys: list[PartitionEntry] = []
        for part, size_mb in zip(phys_editable, new_gpt_sizes, strict=False):
            new_phys.append(
                PartitionEntry(
                    name=part.name,
                    size_mb=size_mb,
                    fs=part.fs,
                    mount=part.mount,
                    note=part.note,
                ),
            )

        # Remainder for the last physical partition.
        gpt_used = sum(p.size_mb for p in new_phys)
        gpt_remainder = disk.size_mb - 2 - gpt_used

        if use_lvm:
            # PV absorbs remainder.
            pv = next(p for p in phys if p.fs == "lvm2pv")
            if gpt_remainder < 512:
                ui.warn("Very little space for LVM PV - proceed with caution.")
                gpt_remainder = max(256, gpt_remainder)
            new_phys.append(
                PartitionEntry(
                    name=pv.name,
                    size_mb=gpt_remainder,
                    fs="lvm2pv",
                    mount="",
                    note="LVM Physical Volume",
                ),
            )
            scheme.lvm_pv_partition = pv.name

            # --- Step B: LV sizes (LVM only) ---
            pv_mb = gpt_remainder  # total VG space available
            if unattended:
                new_lv_sizes = [lv.size_mb for lv in lv_editable]
            else:
                lv_form = [
                    (lv.name, lv.fs, lv.mount or "-", lv.size_mb)
                    for lv in lv_editable
                ]
                _user_new_lv_sizes = ui.prompt_sizes_mb(lv_form, pv_mb)
                if _user_new_lv_sizes is None:
                    # Back from LV form - re-show the GPT form with last
                    # values.
                    gpt_form = [
                        (p.name, p.fs, p.mount or "-", p.size_mb)
                        for p in phys_editable
                    ]
                    continue
                _new_lv_sizes = _user_new_lv_sizes

            new_lvs: list[PartitionEntry] = []
            for lv, size_mb in zip(lv_editable, new_lv_sizes, strict=False):
                new_lvs.append(
                    PartitionEntry(
                        name=lv.name,
                        size_mb=size_mb,
                        fs=lv.fs,
                        mount=lv.mount,
                        note=lv.note,
                    ),
                )

            # Root LV absorbs remainder of the VG.
            lv_used = sum(lv.size_mb for lv in new_lvs)
            lv_remainder = pv_mb - lv_used
            effective_min = (
                MIN_ROOT_MB + int(BOOT_MB * 0.5)
                if not scheme.separate_boot
                else MIN_ROOT_MB
            )
            if lv_remainder < effective_min:
                ui.warn(
                    "Very little space left for root LV - proceed with"
                    " caution.",
                )
                lv_remainder = max(256, lv_remainder)

            root_lv = next(lv for lv in lvs if lv.mount == "/")
            new_lvs.append(
                PartitionEntry(
                    name=root_lv.name,
                    size_mb=lv_remainder,
                    fs=root_lv.fs,
                    mount="/",
                    note="LVM logical volume",
                ),
            )

            scheme.partitions = new_phys + new_lvs

        else:
            # Plain layout: root absorbs GPT remainder.
            effective_min = (
                MIN_ROOT_MB + int(BOOT_MB * 0.5)
                if not scheme.separate_boot
                else MIN_ROOT_MB
            )
            if gpt_remainder < effective_min:
                ui.warn(
                    "Very little space left for root - proceed with caution.",
                )
                gpt_remainder = max(256, gpt_remainder)

            root = next(p for p in scheme.partitions if p.mount == "/")
            new_phys.append(
                PartitionEntry(
                    name=root.name,
                    size_mb=gpt_remainder,
                    fs=root.fs,
                    mount="/",
                    note="",
                ),
            )
            scheme.partitions = new_phys

        # --- Step C: confirm scheme preview ---
        if not unattended and not _show_scheme(
            scheme,
            disk_size_mb=disk.size_mb,
        ):
            # Back - re-show GPT form with last-entered values.
            gpt_form = [
                (p.name, p.fs, p.mount or "-", p.size_mb)
                for p in phys_editable
            ]
            continue

        if not unattended and not ui.confirm(
            "Proceed with this layout?",
            default=True,
        ):
            return _return_user()
        return scheme


def _warn_swap(scheme: PartitionScheme) -> None:
    """Warn the user if swap size is sub-optimal given available RAM.

    Args:
        scheme (PartitionScheme): Scheme to check - see
            ``.config.PartitionScheme``.

    """
    swap_parts = [p for p in scheme.partitions if p.is_swap()]
    if not swap_parts:
        return
    swap_mb = sum(p.size_mb for p in swap_parts)
    ram_mb = get_ram_mb()

    if swap_mb < LOW_SWAP_WARN_MB:
        ui.warn(
            f"Swap is only {human_size(swap_mb)}.  "
            "The system may struggle under memory pressure.",
        )
    if ram_mb and ram_mb < LOW_RAM_WARN_MB and swap_mb < LOW_SWAP_WARN_MB:
        ui.warn(
            f"System RAM ({human_size(ram_mb)}) is low and swap is small. "
            "Consider a larger disk or more RAM.",
        )
