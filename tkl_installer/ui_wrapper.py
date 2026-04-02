"""Dialog-based terminal UI wrapper.

All interactive elements (menus, yes/no prompts, input boxes, progress
dialogs) go through the ``UI`` class so that the rest of the codebase
never imports ``dialog`` directly.

Non-interactive output (status lines that scroll behind the dialog
boxes) is written to stderr so it appears in the log file and systemd
journal only, keeping the dialog UI clean.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable, Generator, Iterator  # noqa: TC003
from typing import NoReturn

import dialog

from .disks import human_size
from .runner import (
    CommandOutput,
    run_apt_progress,
    run_command_progress,
    run_unsquashfs_progress,
)

log = logging.getLogger(__name__)

# defaults
TITLE = "TurnKey Installer"

# set max dialog box size based on terminal size
_MAX_WIDTH = 72
_MAX_HEIGHT = 20
with contextlib.suppress(OSError):
    _term_width, _term_height = os.get_terminal_size(0)
    _MAX_WIDTH = min(_MAX_WIDTH, _term_width)
    _MAX_HEIGHT = min(_MAX_HEIGHT, _term_height)

# set message and menu heights based on max dialog box size
_MSG_HEIGHT = min(10, _MAX_HEIGHT)
_MENU_HEIGHT = min(18, _MAX_HEIGHT)


def fatal(msg: str, code: int) -> NoReturn:
    """Show a fatal error dialog and exit.

    Convenience wrapper around ``UI().fatal()`` for use outside a ``UI``
    instance - e.g. in ``live.assert_live_system()``.

    Args:
        msg (str): Error message to display.
        code (int): Exit code passed to ``sys.exit()``.

    """
    UI().fatal(msg, code)


def _format_table(
    rows: list[tuple[str, ...]],
    headers: tuple[str, ...] | None = None,
    max_width: int = _MAX_WIDTH - 4,
) -> str:
    """Format a list of rows into a fixed-width plain-text table.

    Args:
        rows (list[tuple[str, ...]]): Data rows; all tuples must have
            the same length.
        headers (tuple[str, ...] | None): Optional header row; length
            must match ``rows``.  Defaults to ``None``.
        max_width (int): Maximum total line width in characters.
            Defaults to ``_MAX_WIDTH - 4``.

    Returns:
        str: Formatted table string suitable for display in a dialog
            text box.

    """
    if not rows:
        return ""

    if headers is not None:
        rows = [headers, *rows]

    # calculate minimum column widths from row items
    col_widths = [
        max(len(row[i]) for row in rows) for i in range(len(rows[0]))
    ]

    # distribute remaining space proportionally between columns
    num_cols = len(rows[0])
    separator_space = (num_cols - 1) * 3  # 3 chars between each column " | "
    available = max_width - separator_space
    total_min = sum(col_widths)

    if total_min < available:
        # distribute extra space proportionally to each column
        extra = available - total_min
        # allocate extra space to all but last column
        for i in range(num_cols - 1):
            bonus = int(extra * (col_widths[i] / total_min))
            col_widths[i] += bonus

    # format rows
    lines = []
    for row in rows:
        line = "   ".join(
            f"{item:<{col_widths[i]}}" for i, item in enumerate(row)
        )
        lines.append(line)

    # add separator after header if it exists
    if headers is not None:
        lines.insert(1, "-" * len(lines[0]))

    return "\n".join(lines)


class UI:
    """Opinionated wrapper around ``dialog.Dialog`` for the installer TUI.

    Widget dimensions are capped at sensible defaults and reduced
    automatically in smaller terminals.  All blocking methods loop or
    abort (via ``sys.exit``) rather than returning sentinel values, so
    callers never need to handle cancel/escape explicitly.

    Attributes:
        OK (str): ``dialog.Dialog.OK`` constant - truthy response code.
        CANCEL (str): ``dialog.Dialog.CANCEL`` constant.

    """

    def __init__(self) -> None:
        """Initialise the underlying ``dialog.Dialog`` instance."""
        self._d = dialog.Dialog(dialog="dialog")
        self._d.add_persistent_args(["--no-collapse"])
        self._d.set_background_title(f" {TITLE} ")

        self.OK = self._d.OK
        self.CANCEL = self._d.CANCEL

    def infobox(self, msg: str) -> None:
        """Display a non-blocking status message.

        Args:
            msg (str): Message to display.

        """
        log.info("[ui] %s", msg.strip())
        self._d.infobox(msg, width=_MAX_WIDTH)

    def msgbox(self, msg: str, title: str = "") -> None:
        """Display a blocking message the user must dismiss with OK.

        Args:
            msg (str): Message to display.
            title (str): Dialog title.  Defaults to ``TITLE``.

        """
        log.info("[ui] msgbox: %s", msg.strip())
        self._d.msgbox(
            msg,
            width=_MAX_WIDTH,
            height=_MSG_HEIGHT,
            title=title or TITLE,
        )

    def confirm(self, question: str, default: bool = True) -> bool:
        """Display a blocking Yes/No dialog.

        Args:
            question (str): Question to display.
            default (bool): Pre-selected button; ``True`` = Yes,
                ``False`` = No.  Defaults to ``True``.

        Returns:
            bool: ``True`` if the user chose Yes; ``False`` if they
                chose No or pressed Escape twice.

        """
        log.debug("[ui] confirm: %s (default=%s)", question, default)
        code = self._d.yesno(
            question,
            width=_MAX_WIDTH,
            height=_MSG_HEIGHT,
            defaultno=(not default),
        )
        result = code == self.OK
        log.debug("[ui] confirm result: %s", result)
        return result

    def prompt(
        self,
        question: str,
        default: str = "",
        validator: Callable[[str], tuple[bool, str]] | None = None,
    ) -> str:
        """Display a free-text input box, looping until validation passes.

        Args:
            question (str): Prompt text displayed above the input field.
            default (str): Pre-filled editable value.  Defaults to
                ``""``.
            validator (Callable | None): Optional callable that receives
                the user's input string and returns ``(ok, error_msg)``.
                When ``ok`` is ``False``, ``error_msg`` is shown in a
                ``msgbox`` and the prompt is repeated.  Defaults to
                ``None`` (no validation).

        Returns:
            str: Stripped input string entered by the user.

        Raises:
            SystemExit: If the user presses Escape to cancel.

        """
        log.debug("[ui] prompt: %s", question)
        while True:
            code, value = self._d.inputbox(
                question,
                width=_MAX_WIDTH,
                height=_MSG_HEIGHT,
                init=default,
            )
            if code != self.OK:
                self._abort("Cancelled by user.")
            value = value.strip()
            if validator is None:
                log.debug("[ui] prompt value: %r", value)
                return value
            ok, err = validator(value)
            if ok:
                log.debug("[ui] prompt value: %r", value)
                return value
            self.msgbox(f"Invalid input: {err}", title="Error")

    def prompt_size_mb(
        self,
        question: str,
        default_mb: int,
        max_size: int = 0,
    ) -> int:
        """Display a size input box and return the value in MiB.

        Accepts a number optionally followed by a multiplier suffix.
        The suffix is case-insensitive and binary (not metric):

        ============  ==============
        User input    Assumed value
        ============  ==============
        ``1TiB``      1 TiB
        ``4G``        4 GiB
        ``512``       512 MiB
        ``1028 MB``   1028 MiB
        ============  ==============

        Args:
            question (str): Prompt text displayed above the input field.
            default_mb (int): Pre-filled default value in MiB.
            max_size (int): Maximum allowed size in MiB.  ``0`` means no
                upper limit.  Defaults to ``0``.

        Returns:
            int: Validated size in MiB.

        """

        # internal helper funcs
        def _core_parser(raw: str) -> tuple[float, str]:
            """Parse a raw size string into a (value, multiplier_str) pair.

            Returns ``(0, error_message)`` on parse failure.
            """
            value: float = 0
            multiplier_str = "1"

            raw = raw.strip().upper()
            if raw.endswith(("T", "TB", "TIB")):
                raw = raw.rstrip("TIB")  # char group - NOT literal str
                multiplier_str = str(1024 * 1024)
            elif raw.endswith(("G", "GB", "GIB")):
                raw = raw.rstrip("GIB")  # as above
                multiplier_str = "1024"
            elif raw.endswith(("M", "MB", "MIB")):
                raw = raw.rstrip("MIB")  # as above
            try:
                value = float(raw.strip())
            except ValueError:
                return (
                    0,
                    "Enter a size like 512M, 4.5G, 1T or a plain number"
                    " (MiB).",
                )
            return value, multiplier_str

        def _validator(raw: str) -> tuple[bool, str]:
            """Validate a raw size string; return ``(ok, error_msg)``."""
            value, multiplier = _core_parser(raw)
            if value == 0:
                # 0 return value indicates failure in this case
                return False, multiplier
            value = value * int(multiplier)

            if max_size and value > max_size:
                return (
                    False,
                    f"Maximum size allowed is {human_size(max_size)}.",
                )
            if value < 1:
                return False, "Size must be at least 1 MiB."
            return True, ""

        # start of UI.prompt_size_mb() method main body
        msg = (
            f"{question}\n\nEnter size (e.g. 512M, 4G) or plain number (MiB):"
        )
        if max_size:
            msg = f"{msg[:-1]}, less than {human_size(max_size)}:"
        raw = self.prompt(
            msg,
            default=f"{default_mb}M",
            validator=_validator,
        )
        # re-use _core_parser for return validated value
        value, multiplier = _core_parser(raw)
        return int(value * int(multiplier))

    def prompt_sizes_mb(
        self,
        partitions: list[tuple[str, str, str, int]],
        disk_size_mb: int,
    ) -> list[int] | None:
        """Display a single form for setting multiple partition sizes at once.

        Each row shows the partition name, filesystem, and mount point as
        read-only labels with an editable size field pre-filled with the
        current value.  Uses ``dialog``'s ``mixedform`` widget.

        Args:
            partitions (list[tuple[str, str, str, int]]): Editable
                partitions as ``(name, fs, mount, current_size_mb)``
                tuples.  The '/' partition (root or LVM PV) is
                excluded here - it is calculated from the remainder.
            disk_size_mb (int): Total disk size in MiB, shown in the
                dialog header.

        Returns:
            list[int] | None: Ordered list of sizes in MiB matching the
                input list, or ``None`` if the user cancelled / pressed
                Back.

        """
        # Layout constants
        # ... columns: Name(label) | FS(label) | Mount(label) | Size(field)
        col_name = 1  # label start x
        col_fs = 14
        col_mount = 22
        col_size = 32  # editable field start x
        field_len = 12  # visible + input length of the size field

        # Build mixedform elements - 4 items per partition:
        #   3 read-only label columns: Name, FS & Mount point
        #   + partition size field - set by user
        # Each element consists of:
        #   label, row, col, item, item_row, item_col, field_len, input_len
        #   & attributes
        # attributes: 0 = normal editable, 2 = read-only
        elements: list[tuple] = []

        # Header row (read-only, row 1)
        for col, hdr in (
            (col_name, "Name"),
            (col_fs, "FS"),
            (col_mount, "Mount"),
            (col_size, "Size"),
        ):
            elements.append((hdr, 1, col, "", 1, col + 20, 0, 0, 2))

        for i, (name, fs, mount, size_mb) in enumerate(partitions):
            row = i + 2  # data rows start at 2 (1-indexed)
            default = f"{size_mb}M"
            mount_str = mount or "-"
            # read-only labels (attributes=2)
            elements.append((name, row, col_name, "", row, 1, 0, 0, 2))
            elements.append((fs, row, col_fs, "", row, 1, 0, 0, 2))
            elements.append((mount_str, row, col_mount, "", row, 1, 0, 0, 2))
            # editable size field (attributes=0)
            elements.append(
                (
                    "",
                    row,
                    col_size,
                    default,
                    row,
                    col_size,
                    field_len,
                    field_len,
                    0,
                ),
            )

        n_parts = len(partitions)
        form_h = min(
            n_parts + 4,
            _MAX_HEIGHT - 6,
        )  # +2 for header row + padding
        box_h = form_h + 7
        intro = (
            f"Total disk: {human_size(disk_size_mb)}\n"
            "Set partition sizes (e.g. 512M, 4G).\n\n"
            "The '/' partition will use all remaining space."
        )
        log.debug("[ui] prompt_sizes_mb: %d partitions", n_parts)

        # This "mixedform" returns a list of the writable elements - in this
        # case the partition size/s the user selected for each r/w value. I.e.:
        # - legacy BIOS with '/boot' in '/':
        #   - 1 value; swap
        # - legacy BIOS with separate '/boot' _or_ UEFI with '/boot' in '/':
        #   - 2 values; '/boot' _or_ 'UEFI' _and_ swap
        # - UEFI with separate '/boot':
        #   - 3 values; 'UEFI' _and_ '/boot' _and_ swap
        code, raw_sizes = self._d.mixedform(
            intro,
            elements,
            height=box_h,
            width=_MAX_WIDTH,
            form_height=form_h,
            title=TITLE,
        )
        if code != self.OK:
            return None

        log.debug("[ui] prompt_sizes_mb raw values: %s", raw_sizes)

        # Validate and parse each size string using the same logic as
        # prompt_size_mb. On any error, show a message and re-display the form.
        def _parse(raw: str) -> tuple[int, str]:
            """Parse a raw size string; return ``(size_mb, error)``."""
            raw = raw.strip().upper()
            multiplier = 1
            for suffix, mult in (
                (("T", "TB", "TIB"), 1024 * 1024),
                (("G", "GB", "GIB"), 1024),
                (("M", "MB", "MIB"), 1),
            ):
                if any(raw.endswith(s) for s in suffix):
                    raw = raw.rstrip("TGBIM")
                    multiplier = mult
                    break
            try:
                val = int(float(raw.strip()) * multiplier)
            except ValueError:
                return 0, "Enter sizes like 512M, 4G or a plain number (MiB)."
            if val < 1:
                return 0, "Size must be at least 1 MiB."
            return val, ""

        results: list[int] = []
        errors: list[str] = []
        for (name, _fs, _mount, _default), raw in zip(
            partitions,
            raw_sizes,
            strict=False,
        ):
            mb, err = _parse(raw)
            results.append(mb)
            if err:
                errors.append(f"{name}: {err}")

        if errors:
            self.msgbox("\n".join(errors), title="Invalid input")
            # Re-show with the values the user entered (updated defaults)
            updated = [
                (name, fs, mount, results[i] or size_mb)
                for i, (name, fs, mount, size_mb) in enumerate(partitions)
            ]
            return self.prompt_sizes_mb(updated, disk_size_mb)

        return results

    def choose(
        self,
        question: str,
        options: list[tuple[str, str]],
        default: int = 0,
    ) -> str:
        """Display a radio-button style menu and return the selected tag.

        Args:
            question (str): Question text displayed above the menu.
            options (list[tuple[str, str]]): Menu items as
                ``(tag, description)`` tuples.
            default (int): Index of the pre-selected option.
                Defaults to ``0``.

        Returns:
            str: Tag of the selected option.

        Raises:
            SystemExit: If the user cancels or presses Escape.

        """
        log.debug(
            "[ui] choose: %s options=%s",
            question,
            [o[0] for o in options],
        )
        default_tag = options[default][0] if options else ""
        code, tag = self._d.menu(
            question,
            width=_MAX_WIDTH,
            height=_MENU_HEIGHT,
            menu_height=min(len(options) + 2, _MENU_HEIGHT - 6),
            choices=options,
            default_item=default_tag,
        )
        if code != self.OK:
            self._abort("Cancelled by user.")
        log.debug("[ui] choose result: %s", tag)
        return tag

    def choose_from_list(
        self,
        question: str,
        items: tuple[str, ...] | list[str],
        default: int = 0,
    ) -> str:
        """Like ``choose()``, but accepts flat list; items are their own tags.

        Args:
            question (str): Question text displayed above the menu.
            items (tuple[str, ...] | list[str]): Menu item strings.
            default (int): Index of the pre-selected item.
                Defaults to ``0``.

        Returns:
            str: The selected item string.

        """
        opts = [(item, "") for item in items]
        return self.choose(question, opts, default)

    def show_table(
        self,
        rows: list[tuple[str, ...]],
        headers: tuple[str, ...] | None = None,
        title: str = "",
        yes_label: str = "OK",
        no_label: str = "Cancel",
        footer: str = "",
    ) -> bool:
        """Display a formatted table inside a confirmation dialog.

        Args:
            rows (list[tuple[str, ...]]): Data rows; all tuples must
                have the same length.
            headers (tuple[str, ...] | None): Optional header row;
                length must match ``rows``.  Defaults to ``None``.
            title (str): Dialog title.  Defaults to ``TITLE``.
            yes_label (str): Label for the affirmative button.
                Defaults to ``"OK"``.
            no_label (str): Label for the negative button.
                Defaults to ``"Cancel"``.
            footer (str): Optional note appended below the table.
                Defaults to ``""``.

        Returns:
            bool: ``True`` if the user pressed the yes button;
                ``False`` otherwise.

        """
        text = _format_table(rows, headers)
        if footer:
            text = f"{text}\n{footer}"
        log.debug("[ui] show_table: %d rows", len(rows))
        log.debug("[ui] show_table: rows=%s", rows)
        response = self._d.yesno(
            text,
            width=_MAX_WIDTH,
            height=min(len(rows) + 8, 24),
            title=title or TITLE,
            yes_label=yes_label,
            no_label=no_label,
        )
        log.info("[ui] user response: %s", response)
        return response == self.OK

    @contextlib.contextmanager
    def please_wait(self, msg: str) -> Generator:
        """Context manager that shows an infobox while background work runs.

        Displays a "please wait" infobox on entry. Clean exit logs success,
        an exception, shows a ``msgbox`` error and re-raises.

        Args:
            msg (str): Description of the work being done.

        Raises:
            Exception:
                Any exception/error raised by the background task.

        """
        self.infobox(f"  {msg}\n\n  Please wait...")
        try:
            yield
            log.info("[ui] done: %s", msg)
        except Exception:
            log.exception("[ui] failed: %s", msg)
            self.msgbox(f"\n:(  Step '{msg}' failed\n", title="Error")
            raise

    def progress(
        self,
        output: Iterator[CommandOutput],
        title: str = "",
        initial_text: str = "Please wait...",
    ) -> None:
        """Consume a ``CommandOutput`` iterator & display progress dialog.

        When ``percent`` values are present in the output the gauge
        advances; otherwise the text updates with each line and the
        gauge sits at 0%.

        Args:
            output (Iterator[CommandOutput]): Stream of command output
                items - see ``.runner.CommandOutput``.
            title (str): Dialog title.  Defaults to ``""``.
            initial_text (str): Text shown before any output arrives.
                Defaults to ``"Please wait..."``.

        """
        log.debug("Progress dialog started: %r", title)
        self._d.gauge_start(initial_text, title=title, percent=0)
        try:
            for item in output:
                if item.line:
                    log.debug("Command output: %s", item.line)
                if item.percent is not None:
                    self._d.gauge_update(
                        item.percent,
                        text=item.line or initial_text,
                        update_text=bool(item.line),
                    )
                elif item.line:
                    self._d.gauge_update(
                        0,
                        text=item.line,
                        update_text=True,
                    )
        except Exception:
            log.exception("Error during progress dialog %r", title)
            raise
        finally:
            self._d.gauge_stop()
            log.debug("Progress dialog finished: %r", title)

    def progress_apt_get(
        self,
        packages: list[str],
        apt_command: str,
        autoremove: bool = True,
        title: str = "{} Packages",
        check: bool = True,
    ) -> int | None:
        """Run an ``apt-get`` command with a live progress gauge dialog.

        Args:
            packages (list[str]): Package names to install, remove, or
                purge.
            apt_command (str): One of ``"install"``, ``"remove"``, or
                ``"purge"``.
            autoremove (bool): If ``True``, append ``--autoremove``.
                Defaults to ``True``.
            title (str): Dialog title template; ``{}`` is replaced with
                the action verb.  Defaults to ``"{} Packages"``.
            check (bool): If ``False``, exceptions are suppressed and
                the exit code is returned.  Defaults to ``True``.

        Returns:
            int | None: Subprocess exit code when ``check=False``;
                ``None`` on success.

        Raises:
            Exception: Any exception raised by the subprocess when
                ``check=True``.

        """
        apt_command = apt_command.lower()
        if apt_command in ("purge", "remove"):
            action = f"{apt_command[:-1].capitalize()}ing"
            done = f"{apt_command}d"
        elif apt_command == "install":
            action = "Installing"
            done = "installed"
        else:
            error_msg = f"unexpected apt command: {apt_command}"
            log.error(error_msg)
            fatal(error_msg, 999)
        log.info(
            "%s packages: %s (autoremove=%s)",
            action,
            ", ".join(packages),
            autoremove,
        )
        try:
            with run_apt_progress(
                packages,
                apt_command,
                autoremove,
                check=check,
            ) as output:
                self.progress(
                    output,
                    title=title.format(action),
                    initial_text=f"{action} packages...",
                )
        except Exception:
            log.exception(
                "apt-get %s failed for packages: %s",
                apt_command,
                packages,
            )
            raise
        log.info("Packages %s successfully: %s", done, ", ".join(packages))
        return None

    def progress_unsquashfs(
        self,
        squashfs_file: str,
        dest_dir: str,
        title: str = "Extracting Filesystem",
        check: bool = True,
    ) -> int | None:
        """Extract a squashfs file with a live progress gauge dialog.

        Args:
            squashfs_file (str): Path to the squashfs file to extract.
            dest_dir (str): Destination directory.
            title (str): Dialog title.  Defaults to
                ``"Extracting Filesystem"``.
            check (bool): If ``False``, exceptions are suppressed and
                the exit code is returned.  Defaults to ``True``.

        Returns:
            int | None: Subprocess exit code when ``check=False``;
                ``None`` on success.

        Raises:
            Exception: Any exception raised by the subprocess when
                ``check=True``.

        """
        log.info("Extracting %r to %r", squashfs_file, dest_dir)
        try:
            with run_unsquashfs_progress(
                squashfs_file,
                dest_dir,
                check=check,
            ) as output:
                self.progress(
                    output,
                    title=title,
                    initial_text="Extracting filesystem...",
                )
        except Exception:
            log.exception(
                "unsquashfs failed: %s -> %s",
                squashfs_file,
                dest_dir,
            )
            raise
        log.info("Extraction complete: %r -> %r", squashfs_file, dest_dir)
        return None

    def progress_command(
        self,
        cmd: list[str],
        title: str = "",
        initial_text: str = "Please wait...",
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> int | None:
        """Run an arbitrary command with a live-updating text progress dialog.

        No percent parsing - the gauge sits at 0% and the text updates
        with each output line.

        Args:
            cmd (list[str]): Command to run - not a shell string.
            title (str): Dialog title.  Defaults to ``""``.
            initial_text (str): Text shown before any output arrives.
                Defaults to ``"Please wait..."``.
            extra_env (dict[str, str] | None): Extra environment
                variables.  Defaults to ``None``.
            check (bool): If ``False``, exceptions are suppressed and
                the exit code is returned.  Defaults to ``True``.

        Returns:
            int | None: Subprocess exit code when ``check=False``;
                ``None`` on success.

        Raises:
            Exception: Any exception raised by the subprocess when
                ``check=True``.

        """
        log.info("Running command: %s", " ".join(cmd))
        try:
            with run_command_progress(
                cmd,
                extra_env=extra_env,
                check=check,
            ) as output:
                self.progress(output, title=title, initial_text=initial_text)
        except Exception:
            log.exception("Command failed: %s", cmd)
            raise
        log.info("Command completed: %s", " ".join(cmd))
        return None

    def step(self, msg: str) -> None:
        """Log a progress step message (log file and journal only).

        Args:
            msg (str): Step description.

        """
        log.info("  .  %s", msg)

    def ok(self, msg: str) -> None:
        """Log a success message (log file and journal only).

        Args:
            msg (str): Success description.

        """
        log.info("  OK:  %s", msg)

    def warn(self, msg: str) -> None:
        """Log a warning and display it to the user in a ``msgbox``.

        Args:
            msg (str): Warning message.

        """
        log.warning("  WARN:  %s", msg)
        self.msgbox(f"\n{msg}", title="Warning")

    def error(self, msg: str) -> None:
        """Log an error and display it to the user in a ``msgbox``.

        Args:
            msg (str): Error message.

        """
        log.error("  ERROR:  %s", msg)
        self.msgbox(f"\n{msg}", title="Error")

    def info(self, msg: str) -> None:
        """Log an informational message (log file and journal only).

        Args:
            msg (str): Informational message.

        """
        log.info("     %s", msg)

    def app_start(self) -> None:
        """Show a brief application startup infobox."""
        self.infobox(
            "  TurnKey Linux custom Live Installer\n\n  Starting up...",
        )

    def header(self, title: str) -> None:
        """Mark the start of a new install stage - logged and briefly shown.

        Args:
            title (str): Stage name.

        """
        log.info("=== %s ===", title)
        self.infobox(f"  Stage: {title}\n\n  Please wait...")

    def fatal(self, msg: str, code: int = 1) -> NoReturn:
        """Display a fatal error dialog and exit.

        Args:
            msg (str): Error message to display.
            code (int): Exit code passed to ``sys.exit()``.
                Defaults to ``1``.

        """
        log.error("FATAL: %s", msg)
        self._d.msgbox(
            f"Fatal error\n\n{msg}\n\nThe installer will now exit.",
            width=_MAX_WIDTH,
            height=_MSG_HEIGHT + 4,
            title="Fatal Error",
        )
        sys.exit(code)

    def _abort(self, reason: str) -> None:
        """Handle a user cancel/escape as a graceful abort.

        Args:
            reason (str): Message shown before exiting.

        """
        self.fatal(reason, code=0)
