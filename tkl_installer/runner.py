"""Robust subprocess execution helpers.

All shell interaction goes through this module so that error handling,
logging, and dry-run mode are centralised.
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# When True, destructive commands are logged but not executed.
DRY_RUN: bool = False


class RunError(RuntimeError):
    """Raised when a command exits non-zero and ``check=True``.

    Attributes:
        cmd (list[str]): The command that was run.
        returncode (int): The process exit code.
        stderr (str): Captured stderr output, if available.

    """

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        """Initialise with command details and build human-readable message."""
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Command failed (exit {returncode}): {shlex.join(cmd)}\n{stderr}",
        )


@dataclass
class CommandOutput:
    """A single line of output emitted by a running command.

    Used by progress-aware runner context managers to stream output to
    the UI - see ``.runner.run_command_progress``,
    ``.runner.run_apt_progress``, and ``.runner.run_unsquashfs_progress``.

    Attributes:
        line (str): One line of process output (stripped).
        percent (int | None): Parsed progress percentage, or ``None``
            if the line does not contain progress information.

    """

    line: str
    percent: int | None = None


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = 300,
    destructive: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command via ``subprocess``.

    All arguments after ``cmd`` are keyword-only.

    Args:
        cmd (list[str]): Command as a list of strings - not a shell
            string.
        check (bool): Raise ``RunError`` on non-zero exit.
            Defaults to ``True``.
        capture (bool): Capture stdout and stderr and attach them to
            the returned object.  Defaults to ``False``.
        input_text (str | None): Text to pass to the process on stdin.
            Defaults to ``None``.
        env (dict[str, str] | None): Full environment override.
            Defaults to ``None`` (inherit current environment).
        timeout (int | None): Seconds before ``TimeoutExpired`` is
            raised.  Defaults to ``300``.
        destructive (bool): If ``True`` and ``DRY_RUN`` is set, skip
            execution and log only.  Defaults to ``False``.

    Returns:
        subprocess.CompletedProcess: Completed subprocess result object.

    Raises:
        RunError: If ``check=True`` and the command exits non-zero, or
            if the executable is not found.
        TypeError: If any argument after ``cmd`` is passed positionally.

    """
    if destructive and DRY_RUN:
        log.info("[DRY-RUN] Would run: %s", shlex.join(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    log.debug("run: %s", shlex.join(cmd))

    kwargs: dict = {
        "timeout": timeout,
        "env": env,
        "check": False,  # disable subprocess check; check return code later
        "text": True,
        "encoding": "utf-8",
    }
    if capture or input_text is not None:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input_text is not None:
        kwargs["input"] = input_text

    try:
        result = subprocess.run(cmd, **kwargs)  # noqa: PLW1510
        # supress PLW1510 (subprocess-run-without-check) as we explicitly check
        # the return code and handle that.
    except FileNotFoundError as e:
        raise RunError(cmd, 127, str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise RunError(cmd, -1, f"Timed out after {timeout}s") from e

    if check and result.returncode != 0:
        stderr = result.stderr if capture else ""
        raise RunError(cmd, result.returncode, stderr)

    return result


def run_output(cmd: list[str], **kwargs: Any) -> str:  # noqa: ANN401
    # ANN401: impractical to type **kwargs more precisely here
    """Run a command and return stripped stdout as a string.

    Args:
        cmd (list[str]): Command as a list of strings - not a shell
            string.
        **kwargs: Additional keyword arguments forwarded to ``run()``.

    Returns:
        str: Stripped stdout from the command.

    Raises:
        RunError: If the command exits non-zero (override with
            ``check=False``).

    """
    kwargs["capture"] = True
    kwargs.setdefault("check", True)
    result = run(cmd, **kwargs)
    return result.stdout.strip()


def run_lines(cmd: list[str], **kwargs: Any) -> list[str]:  # noqa: ANN401
    # ANN401: impractical to type **kwargs more precisely here
    """Run a command and return non-empty stdout lines as a list.

    Args:
        cmd (list[str]): Command as a list of strings - not a shell
            string.
        **kwargs: Additional keyword arguments forwarded to
            ``run_output()``.

    Returns:
        list[str]: Non-empty output lines (blank lines omitted).

    Raises:
        RunError: If the command exits non-zero (override with
            ``check=False``).

    """
    return [
        line for line in run_output(cmd, **kwargs).splitlines() if line.strip()
    ]


# Progress-aware runner helpers
# -----------------------------


def _launch(
    cmd: list[str],
    extra_env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    merge_stderr: bool = False,
) -> subprocess.Popen[str]:
    """Start a subprocess and return the ``Popen`` handle.

    Args:
        cmd (list[str]): Command to run - not a shell string.
        extra_env (dict[str, str] | None): Additional environment
            variables merged with the current environment.
            Defaults to ``None``.
        pass_fds (tuple[int, ...]): Extra file descriptors to keep open
            in the child process - in addition to stdin/stdout/stderr.
            Defaults to ``()``.
        merge_stderr (bool): If ``True``, redirect stderr into stdout.
            Defaults to ``False``.

    Returns:
        subprocess.Popen[str]: Running subprocess handle.

    """
    env = {**os.environ, **(extra_env or {})}
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        pass_fds=pass_fds,
    )


def _finalise(
    process: subprocess.Popen[str],
    cmd: list[str],
    stderr_lines: list[str],
    check: bool,
) -> int:
    """Wait for a process to exit and optionally raise on failure.

    Args:
        process (subprocess.Popen[str]): Running subprocess handle.
        cmd (list[str]): The command being run - used only if
            ``RunError`` is raised.
        stderr_lines (list[str]): Accumulated stderr lines - used only
            if ``RunError`` is raised.
        check (bool): If ``True``, raise ``RunError`` on non-zero exit.

    Returns:
        int: Process exit code.

    Raises:
        RunError: If ``check=True`` and the process exits non-zero.

    """
    process.wait()
    if check and process.returncode != 0:
        raise RunError(cmd, process.returncode, "\n".join(stderr_lines))
    return process.returncode


@contextmanager
def run_command_progress(
    cmd: list[str],
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> Generator[Iterator[CommandOutput]]:  # type: ignore[type-arg]
    """Context manager that runs command & yields a ``CommandOutput`` iterator.

    No percent parsing - the gauge sits at 0% and the text updates with
    each output line.  Useful for arbitrary commands where progress
    cannot be inferred.

    Args:
        cmd (list[str]): Command to run - not a shell string.
        extra_env (dict[str, str] | None): Extra environment variables.
            Defaults to ``None``.
        check (bool): Raise ``RunError`` on non-zero exit.
            Defaults to ``True``.

    Yields:
        Iterator[CommandOutput]: Stream of output lines - see
            ``.runner.CommandOutput``.

    Raises:
        RunError: If ``check=True`` and the command exits non-zero.

    """
    process = _launch(cmd, extra_env=extra_env)
    stderr_lines: list[str] = []

    def _iter() -> Iterator[CommandOutput]:
        assert process.stdout is not None  # noqa: S101
        for line in process.stdout:
            yield CommandOutput(line=line.strip())
        if process.stderr:
            stderr_lines.extend(process.stderr.read().splitlines())

    try:
        yield _iter()
    finally:
        _finalise(process, cmd, stderr_lines, check)


@contextmanager
def run_apt_progress(
    packages: list[str],
    apt_command: str,
    auto_remove: bool = False,
    check: bool = True,
) -> Generator[Iterator[CommandOutput]]:  # type: ignore[type-arg]
    """Context manager that runs ``apt-get`` and yields progress output.

    Parses ``APT::Status-Fd`` to produce ``CommandOutput`` items with
    ``percent`` set.

    Args:
        packages (list[str]): Package names to pass to ``apt-get``.
        apt_command (str): One of ``"install"``, ``"remove"``, or
            ``"purge"``.
        auto_remove (bool): If ``True``, append ``--autoremove``.
            Defaults to ``False``.
        check (bool): Raise ``RunError`` on non-zero exit.
            Defaults to ``True``.

    Yields:
        Iterator[CommandOutput]: Stream of progress items - see
            ``.runner.CommandOutput``.

    Raises:
        RunError: If ``check=True`` and ``apt-get`` exits non-zero, or
            if ``apt_command`` is not one of the accepted values.

    """
    cmd = [
        "apt-get",
        apt_command,
        "--yes",
        "--autoremove" if auto_remove else "",
        "-o",
        "Dpkg::Progress=true",
        "-o",
        "Dpkg::Progress-Fancy=0",
        *packages,
    ]
    if apt_command not in ("remove", "purge", "install"):
        raise RunError(
            cmd,
            999,
            f"{apt_command} is not an accepted apt command",
        )
    read_fd, write_fd = os.pipe()
    process = _launch(
        [*cmd, "-o", f"APT::Status-Fd={write_fd}"],
        extra_env={"DEBIAN_FRONTEND": "noninteractive"},
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    stderr_lines: list[str] = []

    def _iter() -> Iterator[CommandOutput]:
        with os.fdopen(read_fd, "r") as pipe:
            for line in map(str.strip, pipe):
                parts = line.split(":", 3)
                if len(parts) == 4 and parts[0] in ("pmstatus", "dlstatus"):
                    try:
                        yield CommandOutput(
                            line=parts[3],
                            percent=int(float(parts[2])),
                        )
                        continue
                    except ValueError:
                        pass
                yield CommandOutput(line=line)
        if process.stderr:
            stderr_lines.extend(process.stderr.read().splitlines())

    try:
        yield _iter()
    finally:
        _finalise(process, cmd, stderr_lines, check)


@contextmanager
def run_unsquashfs_progress(
    squashfs_file: str,
    dest_dir: str,
    check: bool = True,
) -> Generator[Iterator[CommandOutput]]:  # type: ignore[type-arg]
    """Context manager that runs ``unsquashfs`` and yields progress output.

    Parses the ``[==== ]  N/M  P%`` progress lines emitted by
    ``unsquashfs`` to populate ``CommandOutput.percent``.

    Args:
        squashfs_file (str): Path to the squashfs file to extract.
        dest_dir (str): Target directory to extract into.
        check (bool): Raise ``RunError`` on non-zero exit.
            Defaults to ``True``.

    Yields:
        Iterator[CommandOutput]: Stream of progress items - see
            ``.runner.CommandOutput``.

    Raises:
        RunError: If ``check=True`` and ``unsquashfs`` exits non-zero.

    """
    cmd = ["unsquashfs", "-f", "-d", dest_dir, squashfs_file]
    process = _launch(cmd, merge_stderr=True)
    stderr_lines: list[str] = []

    def _iter() -> Iterator[CommandOutput]:
        assert process.stdout is not None  # noqa: S101
        for line in map(str.strip, process.stdout):
            match = re.search(r"\[[\s=\-/\\|]+\]\s+\d+/\d+\s+(\d+)%", line)
            percent = int(match.group(1)) if match else None
            if not match:
                stderr_lines.append(line)
            yield CommandOutput(line=line, percent=percent)

    try:
        yield _iter()
    finally:
        _finalise(process, cmd, stderr_lines, check)


def command_exists(name: str) -> bool:
    """Return ``True`` if ``name`` is available on ``PATH``.

    Args:
        name (str): Executable name to search for.

    Returns:
        bool: ``True`` if the command is found on ``PATH``.

    """
    return shutil.which(name) is not None


def require_commands(*names: str) -> None:
    """Raise ``RuntimeError`` if any of the given commands are missing.

    Called early in startup so the installer fails fast with a clear
    message rather than mid-install.

    Args:
        *names (str): One or more executable names to check.

    Raises:
        RuntimeError: If one or more commands are not found on ``PATH``.

    """
    missing = [n for n in names if not command_exists(n)]
    if missing:
        raise RuntimeError(
            "Required tools are not available: " + ", ".join(missing),
        )
