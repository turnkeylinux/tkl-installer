# tkl-installer

An opinionated, minimal CLI installer for TurnKey Linux. Designed to run from a
live environment (ISO/USB) and install a pre-built rootfs (squashfs) to a
target disk.

Intended for TurnKey Linux but likely works on other Debian based live systems.

## Features

- Auto-detects UEFI vs legacy BIOS boot mode
- Detects and excludes the live boot device
- Supports guided (ext4), guided-LVM, and manual partition schemes
- Calculates sensible default partition sizes based on disk space
- Installs GRUB (EFI or BIOS) via chroot
- Generates `/etc/fstab` using UUIDs
- Optional post-install chroot commands
- Dry-run mode for testing
- TOML config file support

## Requirements

Debian-based live environment with:
- `lsblk`, `blkid`, `sfdisk`, `wipefs`, `partprobe`
- `mkfs.ext4`, `mkfs.vfat`, `mkswap`
- `unsquashfs` (squashfs-tools)
- `grub-install`, `update-grub`
- `pvcreate`, `vgcreate`, `lvcreate` (for LVM)
- `udevadm`
- Python 3.13+

## Common usage

```
tkl-installer [OPTIONS]

Options:
  -c FILE, --config FILE
                        TOML configuration file (all options can be set here).
  --disk DEVICE         Target disk device (e.g. /dev/sda). Skips disk selection.
  --scheme TYPE         Partitioning scheme: guided, guided-lvm, or manual.
  --squashfs PATH       Path to the rootfs squashfs file (default: auto-detected).
  --mount-root PATH     Target filesystem mount point (default: /mnt/target).
  --separate-boot       Create a dedicated /boot partition.
  --no-separate-boot    /boot lives inside / - no separate partition (default).
  --force-wipe          Wipe target disk without prompting, even if existing data.
  --unattended          Never ask questions interactively. Uses defaults where
                        possible; fails if mandatory options are missing.
  --dry-run             Simulate all destructive actions without executing them.
  -v, --verbose         Enable verbose/debug logging.
  -V, --version         Show version and exit.
  -h, --help            Show common options and exit.
  --help-all            Show all options (including advanced) and examples,
                        then exit.
```

## Advanced usage

```
[see above - plus]

  --manual-lvm          Use LVM for manual partitioning layout.
  --no-manual-lvm       Use plain GPT partitions for manual layout (default).
  --yes, -y             Assume yes for all confirmations (dangerous!).

manual partition sizes:
  Sizes in MiB for manual partitioning (--scheme manual only).
  0 or omitted = use default / ask interactively.

  --manual-efi-mb MiB   EFI System Partition size in MiB (UEFI only).
  --manual-boot-mb MiB  /boot partition size in MiB (only used with
                        --separate-boot).
  --manual-swap-mb MiB  Swap partition size in MiB (plain layout only).

manual LVM sizes:
  LVM-specific sizes in MiB (only used when --manual-lvm is set).
  0 or omitted = use default / ask interactively.

  --manual-pv-mb MiB    LVM Physical Volume partition size in MiB (0 = remainder).
  --manual-lv-root-mb MiB
                        Root LV (/) size in MiB (0 = remainder of VG after swap).
  --manual-lv-swap-mb MiB
                        Swap LV size in MiB (0 = calculated from disk/RAM).
```

### Fully interactive

```bash
tkl-installer
```

### From config file

```bash
tkl-installer --config /etc/tkl-installer.toml
```

### Scripted (non-interactive)

```bash
tkl-installer \
  --disk /dev/sda \
  --scheme guided \
  --squashfs /run/live/medium/live/filesystem.squashfs \
  --yes
```

## Partition Scheme Defaults

| Partition | UEFI      | BIOS      | Notes                 |
|-----------|-----------|-----------|-----------------------|
| EFI       | 512 MiB   |     -     | vfat (only UEFI boot) |
| /boot*    | 1 GiB*    | 1 GiB*    | ext4                  |
| swap      | 512M - 4G | 512M - 4G | Scaled to disk size   |
| /         | Remainder | Remainder | ext4                  |

\* Separate `/boot` partition is optional:
  - Default - and recommended - is to include it in `/`
  - If included in `/`, 512 MiB is added to size of `/` when verifying
    sufficient install space on disk.
  - Larger default separate `/boot` partition size is to minimize risk of
    future kernel upgrade issues (or at least extend the time before it occurs)
    if too many old kernels accumulate.

Swap sizing:
- Disk < 16 GiB -> 512 MiB swap
- Disk 16 - 64 GiB -> 2 GiB swap
- Disk >= 64 GiB -> 4 GiB swap

## Config File Reference

See `example-config.toml` for all available options.

## Architecture

```
tkl-installer     Executable with argument parsing + install orchestration
tkl_installer/
  __init__.py     Module definition + basic info
  config.py       InstallerConfig dataclass + TOML loader
  disks.py        Block device probing (lsblk, blkid, pvs)
  installer.py    rootfs unpack, fstab, grub, extra commands
  interactive.py  Interactive prompts
  live.py         Live environment detection
  partitioner.py  Scheme calculation + sfdisk/LVM/mkfs
  runner.py       Subprocess execution with dry-run support
  ui_wrapper.py   Dialog output helpers
```

## TODO

- UEFI support (currently non-functional) - requires:
    - TKL ISOs with support UEFI boot (current ISOs only boot on legacy BIOS)
    - Implement complete UEFI install support in `tkl-installer`.
        - Include required UEFI grub package/s & install them during system
          install process.

- Config / defaults / unattended:
    - Perhaps the config file should be used for the default config?
        - if '--unattended', the config values would be the defaults if not
          passed as switches.
        - if interactive, conf values could be default values in UI. E.g.
          default input box text, default selection in lists, default button,
          etc & the user would still need to confirm their selection.
        - this would make updating defaults trivial as well as easy for users
          to view (perhaps even intuitive?).
        - it would also make it clear users who wish to do unattended install.
    - At initialization tkl-installer should support loading a config file from
      specific places (potentially related to above). Ideas:
        - a USB stick
        - a file within the ISO (but not in the squashfs)
        - a specific path (perhaps even http/s or other net location)
        - kernel cmdline setting which could point it to an arbitrary location.
    - Default swap size:
        - for systems with large RAM (>= 64GiB?) consider default to no swap?

- Disks selection:
    - LVM:
        - If multiple disks are discovered and LVM selected - provide option to
          create single VG spanning multiple disks.
        - Allow user to set LVM % free with 90% default - as per historic TKL
          default (currently uses 100%).
    - Dynamically set MIN_ROOT_MB based on (unpacked) squashfs size - rather
      than somewhat arbitrary defaults.

- UI/Dialog:
    - Progress display - implement clean & consistent progress indication.
    - Dynamically determine terminal size and adjust if required.

- Post install:
    - Ability to install updates (security only - or all available)
    - Ability to run Inithooks prior to reboot (set password, etc) - eliminates
      possibility that the user may have to reboot twice (once after install &
      another after updates installed).
    - Eliminate additional user input once `tkl-installer` exits if user
      chooses to reboot immediately. Currently user must (re)confirm that ISO
      is removed (on tty1) before reboot occurs.
        - Clunky but works ok if user is already on tty1, but installer appears
          to hang if `tkl-installer` is launched on an alternate tty.

- General / Packaging / Code:
    - Generate/include "proper" man pages.
    - Improved `--version` calculation/source (see comment in `tkl-installer`).
    - Resolve `TODO`s in code comments.
