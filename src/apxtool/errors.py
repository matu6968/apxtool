class ApxError(Exception):
    """An expected apxtool failure that can be shown without a traceback."""


class TransferError(ApxError):
    """A SCSI transfer failed or returned fewer bytes than requested."""

class DebugCommandError(ApxError):
    def __init__(self,status):
        self.status=status
        super().__init__(f'APXD command failed with status {status}')


class DeviceError(ApxError):
    """The selected SCSI device is not the expected Buildwin camera."""


class ProtocolUnavailable(ApxError):
    """The experimental APX payload is not installed or not responding."""


class UnsafeOperation(ApxError):
    """A destructive or experimental operation failed its explicit gates."""
