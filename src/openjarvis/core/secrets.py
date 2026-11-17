"""API keys kept out of config files.

On Windows a key is stored in the Windows Credential Manager (the same vault
browsers and Git use), as a generic credential named ``OpenJarvis/<name>``.
Elsewhere, or if the vault is unavailable, the ``<NAME>_API_KEY`` environment
variable is the only source. Keys are never written to ``config.toml``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_PREFIX = "OpenJarvis/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _env_name(name: str) -> str:
    return f"{name.upper().replace('-', '_')}_API_KEY"


if sys.platform == "win32":
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def _advapi() -> ctypes.WinDLL:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None
        return advapi

    def _vault_get(name: str) -> Optional[str]:
        advapi = _advapi()
        found = ctypes.POINTER(_CREDENTIAL)()
        if not advapi.CredReadW(
            _PREFIX + name, _CRED_TYPE_GENERIC, 0, ctypes.byref(found)
        ):
            return None
        try:
            cred = found.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-16-le") or None
        finally:
            advapi.CredFree(found)

    def _vault_set(name: str, value: str) -> None:
        blob = value.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIAL()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = _PREFIX + name
        cred.Comment = "OpenJarvis API key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "openjarvis"
        if not _advapi().CredWriteW(ctypes.byref(cred), 0):
            raise OSError(ctypes.get_last_error(), "Could not save the key")

    def _vault_delete(name: str) -> None:
        _advapi().CredDeleteW(_PREFIX + name, _CRED_TYPE_GENERIC, 0)

else:  # pragma: no cover - only the environment variable elsewhere

    def _vault_get(name: str) -> Optional[str]:
        return None

    def _vault_set(name: str, value: str) -> None:
        raise OSError("No secure key store on this platform; set " + _env_name(name))

    def _vault_delete(name: str) -> None:
        return None


def get_key(name: str) -> Optional[str]:
    """The ``name`` API key ("groq"): the environment variable wins, then
    the Windows Credential Manager. None when there's neither."""
    value = os.environ.get(_env_name(name), "").strip()
    if value:
        return value
    try:
        return _vault_get(name)
    except Exception as exc:
        logger.debug("Reading the %s key failed: %s", name, exc)
        return None


def set_key(name: str, value: str) -> None:
    """Store (or, with an empty value, remove) the ``name`` API key."""
    value = (value or "").strip()
    if not value:
        _vault_delete(name)
        return
    _vault_set(name, value)


def has_key(name: str) -> bool:
    return bool(get_key(name))


__all__ = ["get_key", "has_key", "set_key"]
