import abc
from _typeshed import Incomplete
from typing import NamedTuple

class _VersionInfo(NamedTuple):
    major: Incomplete
    minor: Incomplete
    micro: Incomplete
    releasesuffix: Incomplete

class VersionInfo(_VersionInfo): ...

version_info: Incomplete
__version__: Incomplete

class error(Exception):
    message: Incomplete
    def __init__(self, message=None) -> None: ...
    def complete_message(self): ...
    ExceptionShortDescription: Incomplete

PythonDialogException = error

class ExecutableNotFound(error):
    ExceptionShortDescription: str

class PythonDialogBug(error):
    ExceptionShortDescription: str

class ProbablyPythonBug(error):
    ExceptionShortDescription: str

class BadPythonDialogUsage(error):
    ExceptionShortDescription: str

class PythonDialogSystemError(error):
    ExceptionShortDescription: str

class PythonDialogOSError(PythonDialogSystemError):
    ExceptionShortDescription: str

class PythonDialogIOError(PythonDialogOSError):
    ExceptionShortDescription: str

class PythonDialogErrorBeforeExecInChildProcess(PythonDialogSystemError):
    ExceptionShortDescription: str

class PythonDialogReModuleError(PythonDialogSystemError):
    ExceptionShortDescription: str

class UnexpectedDialogOutput(error):
    ExceptionShortDescription: str

class DialogTerminatedBySignal(error):
    ExceptionShortDescription: str

class DialogError(error):
    ExceptionShortDescription: str

class UnableToRetrieveBackendVersion(error):
    ExceptionShortDescription: str

class UnableToParseBackendVersion(error):
    ExceptionShortDescription: str

class UnableToParseDialogBackendVersion(UnableToParseBackendVersion):
    ExceptionShortDescription: str

class InadequateBackendVersion(error):
    ExceptionShortDescription: str

class BackendVersion(metaclass=abc.ABCMeta):
    @classmethod
    @abc.abstractmethod
    def fromstring(cls, s): ...
    @classmethod
    @abc.abstractmethod
    def __lt__(self, other): ...
    @abc.abstractmethod
    def __le__(self, other): ...
    @abc.abstractmethod
    def __eq__(self, other): ...
    @abc.abstractmethod
    def __ne__(self, other): ...
    @abc.abstractmethod
    def __gt__(self, other): ...
    @abc.abstractmethod
    def __ge__(self, other): ...

class DialogBackendVersion(BackendVersion):
    dotted_part: Incomplete
    rest: Incomplete
    def __init__(self, dotted_part_or_str, rest: str = "") -> None: ...
    @classmethod
    def fromstring(cls, s): ...
    def __lt__(self, other): ...
    def __le__(self, other): ...
    def __eq__(self, other): ...
    def __ne__(self, other): ...
    def __gt__(self, other): ...
    def __ge__(self, other): ...

def widget(func): ...
def retval_is_code(func): ...

class Dialog:
    OK: str
    CANCEL: str
    ESC: str
    EXTRA: str
    HELP: str
    DIALOG_OK: Incomplete
    DIALOG_CANCEL: Incomplete
    DIALOG_ESC: Incomplete
    DIALOG_EXTRA: Incomplete
    DIALOG_HELP: Incomplete
    DIALOG_ITEM_HELP: Incomplete
    @property
    def DIALOG_ERROR(self): ...
    DIALOGRC: Incomplete
    compat: Incomplete
    autowidgetsize: Incomplete
    dialog_persistent_arglist: Incomplete
    use_stdout: bool
    pass_args_via_file: bool
    cached_backend_version: Incomplete
    def __init__(
        self,
        dialog: str = "dialog",
        DIALOGRC=None,
        compat: str = "dialog",
        use_stdout=None,
        *,
        autowidgetsize: bool = False,
        pass_args_via_file=None,
    ) -> None: ...
    @classmethod
    def dash_escape(cls, args): ...
    @classmethod
    def dash_escape_nf(cls, args): ...
    def add_persistent_args(self, args) -> None: ...
    def set_background_title(self, text) -> None: ...
    def setBackgroundTitle(self, text) -> None: ...
    def setup_debug(
        self,
        enable,
        file=None,
        always_flush: bool = False,
        *,
        expand_file_opt: bool = False,
    ) -> None: ...
    def clear(self) -> None: ...
    def backend_version(self): ...
    def maxsize(self, **kwargs): ...
    @widget
    def buildlist(
        self,
        text,
        height: int = 0,
        width: int = 0,
        list_height: int = 0,
        items=[],
        **kwargs,
    ): ...
    @widget
    def calendar(
        self,
        text,
        height=None,
        width: int = 0,
        day: int = -1,
        month: int = -1,
        year: int = -1,
        **kwargs,
    ): ...
    @widget
    def checklist(
        self,
        text,
        height=None,
        width=None,
        list_height=None,
        choices=[],
        **kwargs,
    ): ...
    @widget
    def form(
        self,
        text,
        elements,
        height: int = 0,
        width: int = 0,
        form_height: int = 0,
        **kwargs,
    ): ...
    @widget
    def passwordform(
        self,
        text,
        elements,
        height: int = 0,
        width: int = 0,
        form_height: int = 0,
        **kwargs,
    ): ...
    @widget
    def mixedform(
        self,
        text,
        elements,
        height: int = 0,
        width: int = 0,
        form_height: int = 0,
        **kwargs,
    ): ...
    @widget
    def dselect(self, filepath, height: int = 0, width: int = 0, **kwargs): ...
    @widget
    def editbox(self, filepath, height: int = 0, width: int = 0, **kwargs): ...
    def editbox_str(self, init_contents, *args, **kwargs): ...
    @widget
    def fselect(self, filepath, height: int = 0, width: int = 0, **kwargs): ...
    def gauge_start(
        self,
        text: str = "",
        height=None,
        width=None,
        percent: int = 0,
        **kwargs,
    ) -> None: ...
    def gauge_update(
        self, percent, text: str = "", update_text: bool = False
    ) -> None: ...
    def gauge_iterate(*args, **kwargs) -> None: ...
    @widget
    @retval_is_code
    def gauge_stop(self): ...
    @widget
    @retval_is_code
    def infobox(self, text, height=None, width=None, **kwargs): ...
    @widget
    def inputbox(
        self, text, height=None, width=None, init: str = "", **kwargs
    ): ...
    @widget
    def inputmenu(
        self,
        text,
        height: int = 0,
        width=None,
        menu_height=None,
        choices=[],
        **kwargs,
    ): ...
    @widget
    def menu(
        self,
        text,
        height=None,
        width=None,
        menu_height=None,
        choices=[],
        **kwargs,
    ): ...
    @widget
    @retval_is_code
    def mixedgauge(
        self,
        text,
        height: int = 0,
        width: int = 0,
        percent: int = 0,
        elements=[],
        **kwargs,
    ): ...
    @widget
    @retval_is_code
    def msgbox(self, text, height=None, width=None, **kwargs): ...
    @widget
    @retval_is_code
    def pause(
        self, text, height=None, width=None, seconds: int = 5, **kwargs
    ): ...
    @widget
    def passwordbox(
        self, text, height=None, width=None, init: str = "", **kwargs
    ): ...
    @widget
    @retval_is_code
    def progressbox(
        self,
        file_path=None,
        file_flags=...,
        fd=None,
        text=None,
        height=None,
        width=None,
        **kwargs,
    ): ...
    @widget
    @retval_is_code
    def programbox(
        self,
        file_path=None,
        file_flags=...,
        fd=None,
        text=None,
        height=None,
        width=None,
        **kwargs,
    ): ...
    @widget
    def radiolist(
        self,
        text,
        height=None,
        width=None,
        list_height=None,
        choices=[],
        **kwargs,
    ): ...
    @widget
    def rangebox(
        self,
        text,
        height: int = 0,
        width: int = 0,
        min=None,
        max=None,
        init=None,
        **kwargs,
    ): ...
    @widget
    @retval_is_code
    def scrollbox(self, text, height=None, width=None, **kwargs): ...
    @widget
    @retval_is_code
    def tailbox(self, filepath, height=None, width=None, **kwargs): ...
    @widget
    @retval_is_code
    def textbox(self, filepath, height=None, width=None, **kwargs): ...
    @widget
    def timebox(
        self,
        text,
        height=None,
        width=None,
        hour: int = -1,
        minute: int = -1,
        second: int = -1,
        **kwargs,
    ): ...
    @widget
    def treeview(
        self,
        text,
        height: int = 0,
        width: int = 0,
        list_height: int = 0,
        nodes=[],
        **kwargs,
    ): ...
    @widget
    @retval_is_code
    def yesno(self, text, height=None, width=None, **kwargs): ...
