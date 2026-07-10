"""Typed backend exceptions for status mapping without string matching."""


class ModelInvocationError(Exception):
    """Provider/model call failed."""


class OutputParseError(Exception):
    """Model output could not be parsed into the expected structure."""


class OutputContractValidationError(Exception):
    """Parsed output failed the declared output contract."""


class BackendInitializationError(Exception):
    """Backend could not be initialized or is unavailable."""


class BackendCapabilityError(Exception):
    """Request exceeds backend capabilities."""
