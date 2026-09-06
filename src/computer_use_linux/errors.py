class ComputerUseError(Exception):
    """Base class for expected, user-facing failures."""


class BackendUnavailable(ComputerUseError):
    """The requested backend cannot be used on this session."""


class SurfaceNotFound(ComputerUseError):
    """A requested capture surface does not exist."""


class SafetyRefusal(ComputerUseError):
    """An action or frame was refused by the safety policy."""


class CaptureTimeout(ComputerUseError):
    """A frame was not produced before the capture deadline."""


class InputError(ComputerUseError):
    """An input request is outside the selected surface or protocol."""

