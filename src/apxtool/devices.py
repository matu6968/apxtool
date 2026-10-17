from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

from .errors import ApxError, DeviceError
from .protocol import build_enter_loader_cdb
from .transport import (
    Inquiry,
    LinuxSCSITransport,
    SCSITransport,
    inquiry,
    require_camera,
    require_complete,
    iter_sg_paths,
    wait_for_loader,
)


def get_identity(transport: SCSITransport) -> Inquiry:
    return inquiry(transport)


def enter_loader(
    path: str,
    *,
    timeout: float = 20.0,
    wait: Callable[..., str] = wait_for_loader,
    opener: Callable[[str], SCSITransport] = LinuxSCSITransport,
) -> str:
    with opener(path) as transport:  # type: ignore[attr-defined]
        identity = inquiry(transport)
        require_camera(identity)
        if identity.is_loader:
            return path
        transfer = transport.execute(build_enter_loader_cdb())
        require_complete(transfer, 0, "0xda enter-loader command")
    return wait(timeout=timeout, opener=opener)


def enter_loader_with_device(
    path: str,
    *,
    timeout: float = 20.0,
    opener: Callable[[str], SCSITransport] = LinuxSCSITransport,
    paths: Callable[[], list[str]] = iter_sg_paths,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, SCSITransport, Inquiry]:
    """Enter loader mode and keep its SCSI handle open for an immediate action."""
    if timeout <= 0:
        raise ValueError("loader timeout must be positive")
    validate_camera_path(path)
    dev = opener(path)
    try:
        identity = inquiry(dev)
        require_camera(identity)
        if identity.is_loader:
            return path, dev, identity
        require_complete(dev.execute(build_enter_loader_cdb()), 0, "0xda enter-loader command")
    except Exception:
        dev.close()
        raise
    dev.close()

    deadline = clock() + timeout
    while clock() < deadline:
        matches: list[tuple[str, SCSITransport, Inquiry]] = []
        for candidate_path in paths():
            candidate: SCSITransport | None = None
            try:
                candidate = opener(candidate_path)
                candidate_identity = inquiry(candidate)
            except (OSError, ApxError):
                if candidate is not None:
                    candidate.close()
                continue
            if candidate_identity.is_loader:
                matches.append((candidate_path, candidate, candidate_identity))
            else:
                candidate.close()

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            for _, candidate, _ in matches:
                candidate.close()
            raise DeviceError(
                "more than one Buildwin Video050Loader is present; select the camera's "
                "SCSI path explicitly"
            )
        sleeper(min(0.05, max(0.0, deadline - clock())))

    raise DeviceError(
        f"timed out after {timeout:g}s waiting for Buildwin Video050Loader; "
        "check USB re-enumeration and SCSI generic device permissions"
    )


def require_loader(identity: Inquiry) -> None:
    if not identity.is_loader:
        raise DeviceError("this operation requires Buildwin Video050Loader mode")


def validate_camera_path(path: str) -> None:
    if not path:
        raise ApxError("an explicit --device SCSI generic path is required")
    if not Path(path).exists():
        raise FileNotFoundError(path)
