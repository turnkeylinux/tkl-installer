Update for initial TKL v19.0 beta builds.

* Functionality:
    - Make ISO ejection code more robust.
    - Disable live-tools asking to eject cd/usb/iso - tkl-installer already asks.
    - Remove tkl-installer and deps from installed system as last step.
    - Retry unmounting rootfs if it fails initially (probably redundant...).
    - Run 'swapoff' before removing LVM - swap is likely an LV and may be loaded.

* General code/repo updates:
    - Minor linting fixes.
    - Update gitignore to include common build and cache files.
