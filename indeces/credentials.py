"""Local provider credentials, encrypted for the current Windows user.

The only durable representation is a versioned DPAPI ciphertext. Credentials
never become part of configuration, scratch records, or exception messages.
Other platforms support session/environment tokens in the console, but this
module deliberately refuses a durable plaintext fallback.
"""
from __future__ import annotations

import ctypes
import getpass
import json
import os
from pathlib import Path
import stat
import tempfile
import warnings


_MAGIC = b"INDECES-DISCORD-DPAPI\x00\x01"
_ENTROPY = b"Indeces_memory_manager.discord.credentials.v1"
_OPENAI_MAGIC = b"INDECES-OPENAI-DPAPI\x00\x01"
_OPENAI_ENTROPY = b"Indeces_memory_manager.openai.credentials.v1"
_MAX_CIPHERTEXT = 64 * 1024
_CODES = frozenset({
    "invalid_token", "invalid_guild_id", "invalid_api_key", "unsupported_platform",
    "credential_protect_failed", "credential_unprotect_failed",
    "credential_save_failed", "credential_read_failed",
    "credential_corrupt", "credential_scope_mismatch", "credential_unsafe_path",
    "private_input_unavailable",
})


class CredentialError(ValueError):
    """A fixed, safe diagnostic code; no provider or input exception text."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "credential_corrupt"
        super().__init__(self.code)


def secret_path(config_path: Path) -> Path:
    return Path(config_path).with_suffix(".discord.secret")


def openai_secret_path(config_path: Path) -> Path:
    return Path(config_path).with_suffix(".openai.secret")


def prompt_secret(prompt: str) -> str:
    """Require private terminal input; never use getpass's echoed fallback."""
    failed = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        failed = True
    if failed:
        # Raise outside the handler so terminal errors cannot retain secrets.
        raise CredentialError("private_input_unavailable")


def validate_token(token: str) -> str:
    """Accept opaque printable ASCII tokens, without assuming a JWT format."""
    if not isinstance(token, str):
        raise CredentialError("invalid_token")
    token = token.strip(" ")
    if not 20 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
        raise CredentialError("invalid_token")
    return token


def validate_api_key(key: str) -> str:
    """Validate an opaque key without requiring a provider-specific prefix."""
    if not isinstance(key, str):
        raise CredentialError("invalid_api_key")
    key = key.strip(" ")
    if not 20 <= len(key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in key):
        raise CredentialError("invalid_api_key")
    return key


def _validate_guild_id(guild_id: str) -> str:
    if (not isinstance(guild_id, str) or not 1 <= len(guild_id) <= 20
            or any(c not in "0123456789" for c in guild_id)
            or not 0 < int(guild_id) < 2**64):
        raise CredentialError("invalid_guild_id")
    return guild_id


def _crypt(data: bytes, *, protect: bool, entropy: bytes | None = None) -> bytes:
    """Call DPAPI with application entropy and all UI disabled."""
    if os.name != "nt":
        raise CredentialError("unsupported_platform")
    # The default remains the original Discord entropy, preserving old files.
    entropy_bytes = _ENTROPY if entropy is None else entropy
    code = "credential_protect_failed" if protect else "credential_unprotect_failed"
    # ctypes APIs are initialized inside the call so imports remain portable.
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    output = Blob()
    free = None
    failed = False
    result = None
    try:
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        free = kernel.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        entropy_buffer = (ctypes.c_ubyte * len(entropy_bytes)).from_buffer_copy(entropy_bytes)
        source = Blob(len(data), source_buffer)
        entropy_blob = Blob(len(entropy_bytes), entropy_buffer)
        method = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
        description_type = wintypes.LPCWSTR if protect else ctypes.POINTER(wintypes.LPWSTR)
        method.argtypes = [ctypes.POINTER(Blob), description_type, ctypes.POINTER(Blob),
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        method.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN = 1. No LOCAL_MACHINE flag: current user.
        if not method(ctypes.byref(source), None, ctypes.byref(entropy_blob), None, None,
                      1, ctypes.byref(output)):
            failed = True
        elif not output.pbData or not 0 < output.cbData <= _MAX_CIPHERTEXT:
            failed = True
        else:
            result = ctypes.string_at(output.pbData, output.cbData)
    except Exception:
        failed = True
    finally:
        if free is not None and output.pbData:
            free(ctypes.cast(output.pbData, ctypes.c_void_p))
    if failed or result is None:
        raise CredentialError(code)
    return result


def _protect(data: bytes) -> bytes:
    return _crypt(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    return _crypt(data, protect=False)


def _protect_openai(data: bytes) -> bytes:
    return _crypt(data, protect=True, entropy=_OPENAI_ENTROPY)


def _unprotect_openai(data: bytes) -> bytes:
    return _crypt(data, protect=False, entropy=_OPENAI_ENTROPY)


def _check_secret_path(path: Path) -> None:
    """Reject symbolic links and special files without opening them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise CredentialError("credential_unsafe_path")
    # Windows junctions/reparse points may not present as ordinary symlinks.
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise CredentialError("credential_unsafe_path")


def save_discord_token(config_path: Path, guild_id: str, token: str) -> None:
    guild_id = _validate_guild_id(guild_id)
    token = validate_token(token)
    path = secret_path(config_path)
    temporary: str | None = None
    fd: int | None = None
    failure: str | None = None
    try:
        _check_secret_path(path)
        payload = json.dumps({"version": 1, "guild_id": guild_id, "token": token},
                             separators=(",", ":")).encode("utf-8")
        ciphertext = _protect(payload)
        if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= _MAX_CIPHERTEXT:
            raise CredentialError("credential_protect_failed")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        stream = os.fdopen(fd, "wb")
        fd = None  # The stream now owns the descriptor.
        with stream:
            stream.write(_MAGIC + ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        _check_secret_path(path)
        os.replace(temporary, path)
        temporary = None
    except CredentialError as error:
        failure = error.code
    except Exception:
        failure = "credential_save_failed"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    # Raise outside the handler so even __context__ cannot retain input errors.
    if failure is not None:
        raise CredentialError(failure)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError("credential_corrupt")
        result[key] = value
    return result


def load_discord_token(config_path: Path, guild_id: str) -> str | None:
    guild_id = _validate_guild_id(guild_id)
    path = secret_path(config_path)
    failure: str | None = None
    token = None
    fd: int | None = None
    try:
        _check_secret_path(path)
        # O_NOFOLLOW on POSIX prevents a symlink swap between lstat and open.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        stream = os.fdopen(fd, "rb")
        fd = None
        with stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CredentialError("credential_unsafe_path")
            contents = stream.read(len(_MAGIC) + _MAX_CIPHERTEXT + 1)
        if not contents.startswith(_MAGIC) or not 0 < len(contents) - len(_MAGIC) <= _MAX_CIPHERTEXT:
            raise CredentialError("credential_corrupt")
        plaintext = _unprotect(contents[len(_MAGIC):])
        if not isinstance(plaintext, bytes) or not 0 < len(plaintext) <= 16 * 1024:
            raise CredentialError("credential_corrupt")
        raw = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(raw, dict) or set(raw) != {"version", "guild_id", "token"}
                or type(raw["version"]) is not int or raw["version"] != 1):
            raise CredentialError("credential_corrupt")
        stored_guild = _validate_guild_id(raw["guild_id"])
        token = validate_token(raw["token"])
        if stored_guild != guild_id:
            raise CredentialError("credential_scope_mismatch")
    except CredentialError as error:
        failure = error.code
    except (UnicodeError, json.JSONDecodeError, TypeError):
        failure = "credential_corrupt"
    except OSError:
        failure = "credential_read_failed"
    except Exception:
        failure = "credential_unprotect_failed"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if failure is not None:
        raise CredentialError(failure)
    return token


def _check_openai_secret_path(path: Path) -> None:
    """Reject special targets and reparse/symlink parents before file access."""
    _check_secret_path(path)
    for parent in path.absolute().parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or parent.is_symlink()
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise CredentialError("credential_unsafe_path")


def save_openai_key(config_path: Path, key: str) -> None:
    """Persist only DPAPI ciphertext, atomically, without changing config."""
    key = validate_api_key(key)
    path = openai_secret_path(config_path)
    temporary: str | None = None
    fd: int | None = None
    failure: str | None = None
    try:
        _check_openai_secret_path(path)
        payload = json.dumps({"version": 1, "provider": "openai", "api_key": key},
                             separators=(",", ":")).encode("utf-8")
        ciphertext = _protect_openai(payload)
        if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= _MAX_CIPHERTEXT:
            raise CredentialError("credential_protect_failed")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        stream = os.fdopen(fd, "wb")
        fd = None
        with stream:
            stream.write(_OPENAI_MAGIC + ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        _check_openai_secret_path(path)
        os.replace(temporary, path)
        temporary = None
    except CredentialError as error:
        failure = error.code
    except Exception:
        failure = "credential_save_failed"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    if failure is not None:
        raise CredentialError(failure)


def _reject_json_constant(value):
    raise CredentialError("credential_corrupt")


def load_openai_key(config_path: Path) -> str | None:
    """Load a bounded, strictly versioned OpenAI payload; absence is normal."""
    path = openai_secret_path(config_path)
    failure: str | None = None
    key = None
    fd: int | None = None
    try:
        _check_openai_secret_path(path)
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        stream = os.fdopen(fd, "rb")
        fd = None
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CredentialError("credential_unsafe_path")
            # Windows lacks O_NOFOLLOW: check the path again before any read.
            _check_openai_secret_path(path)
            current = path.lstat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise CredentialError("credential_unsafe_path")
            contents = stream.read(len(_OPENAI_MAGIC) + _MAX_CIPHERTEXT + 1)
        if (not contents.startswith(_OPENAI_MAGIC)
                or not 0 < len(contents) - len(_OPENAI_MAGIC) <= _MAX_CIPHERTEXT):
            raise CredentialError("credential_corrupt")
        plaintext = _unprotect_openai(contents[len(_OPENAI_MAGIC):])
        if not isinstance(plaintext, bytes) or not 0 < len(plaintext) <= 16 * 1024:
            raise CredentialError("credential_corrupt")
        raw = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=_reject_json_constant)
        if (not isinstance(raw, dict) or set(raw) != {"version", "provider", "api_key"}
                or type(raw["version"]) is not int or raw["version"] != 1
                or raw["provider"] != "openai"):
            raise CredentialError("credential_corrupt")
        key = validate_api_key(raw["api_key"])
        if key != raw["api_key"]:
            raise CredentialError("credential_corrupt")
    except CredentialError as error:
        failure = "credential_corrupt" if error.code == "invalid_api_key" else error.code
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        failure = "credential_corrupt"
    except OSError:
        failure = "credential_read_failed"
    except Exception:
        failure = "credential_unprotect_failed"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if failure is not None:
        raise CredentialError(failure)
    return key
