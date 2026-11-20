"""Windows current-user DPAPI, shared by OAuth connectors and API providers."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class ProtectionError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data: bytes, *, decrypt: bool = False, entropy: bytes | None = None) -> bytes:
    if sys.platform != "win32":
        raise ProtectionError("Windows DPAPI is unavailable.")
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = _Blob(len(data), source_buffer), _Blob()
    context = None
    if entropy is not None:
        context_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
        context = ctypes.byref(_Blob(len(entropy), context_buffer))
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        operation.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN only: no prompt, and no machine-wide protection.
        if not operation(ctypes.byref(source), None, context, None, None, 1, ctypes.byref(result)):
            raise ProtectionError("Windows DPAPI could not protect or recover the credential.")
        return ctypes.string_at(result.data, result.size)
    except OSError:
        raise ProtectionError("Windows DPAPI is unavailable.") from None
    finally:
        ctypes.memset(source_buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel32.LocalFree(result.data)
