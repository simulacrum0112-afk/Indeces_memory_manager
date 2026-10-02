"""Local OpenAI credential setup. No provider or Discord requests are made."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from .config import load_config
from .path_policy import validate_config_path
from .credentials import (CredentialError, _check_openai_secret_path, load_openai_key,
                          openai_secret_path, prompt_secret, save_openai_key,
                          validate_api_key)
from .lock import InstanceLock


class ApiKeyWizardError(ValueError):
    pass


def _receipt(output, message):
    """A broken terminal must not change the result of a credential commit."""
    try:
        output(message)
    except (Exception, KeyboardInterrupt):
        pass


def _vault_snapshot(path: Path):
    """Read bounded ciphertext only; reject nonregular credential paths."""
    _check_openai_secret_path(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            info = os.fstat(stream.fileno())
            _check_openai_secret_path(path)
            current = path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ApiKeyWizardError("credential_unsafe_path")
            contents = stream.read(70 * 1024 + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(contents) > 70 * 1024:
        raise ApiKeyWizardError("credential_file_too_large")
    return contents


def _restore_ciphertext(path: Path, contents):
    """Rollback interrupted saves with encrypted bytes, never a plaintext key."""
    _check_openai_secret_path(path)
    if _vault_snapshot(path) == contents:
        return
    if contents is None:
        path.unlink(missing_ok=True)
        return
    descriptor = temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        _check_openai_secret_path(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def configure_api_key(config_path: Path, input_fn=None, secret_fn=None, output=None) -> bool:
    input_fn = input_fn or input
    secret_fn = secret_fn or prompt_secret
    output = output or print
    lease = None
    committed = False
    try:
        path = validate_config_path(config_path)
        original = path.read_bytes()
        config = load_config(path)
        try:
            lease = InstanceLock(config.state_dir)
        except RuntimeError:
            output("API key setup unavailable: stop the running instance using this state directory first.")
            return False
        if path.read_bytes() != original:
            raise ApiKeyWizardError("configuration_changed_during_setup")
        vault = openai_secret_path(path)
        old_ciphertext = _vault_snapshot(vault)
        old_key = None
        if old_ciphertext is not None:
            try:
                old_key = load_openai_key(path)
            except CredentialError as error:
                output(f"Saved API key unavailable: {error.code}. Supply a replacement key to repair setup.")
        output("Indeces OpenAI API key wizard. Hidden input; no network validation or service startup.")
        if os.environ.get("OPENAI_API_KEY"):
            output("OPENAI_API_KEY is set: it takes priority over this saved key when starting. Remove it to use the saved key.")
        while True:
            key = secret_fn("OpenAI API key (hidden; Enter keeps saved key): ")
            if not key and old_key is not None:
                key = old_key
                break
            try:
                key = validate_api_key(key)
                break
            except CredentialError:
                output("Enter a nonempty API key without whitespace/control characters; it will stay hidden.")
        if path.read_bytes() != original or _vault_snapshot(vault) != old_ciphertext:
            raise ApiKeyWizardError("configuration_changed_during_setup")
        output(f"Review: provider=OpenAI; model={config.adapter.model}; API key supplied (hidden).")
        output("Save encrypted for the current Windows user. Public configuration and Discord token stay unchanged.")
        if input_fn("Save this API key? [y/N]: ").strip().lower() not in {"y", "yes"}:
            output("Cancelled; saved API key unchanged.")
            return False
        if path.read_bytes() != original or _vault_snapshot(vault) != old_ciphertext:
            raise ApiKeyWizardError("configuration_changed_during_setup")
        try:
            save_openai_key(path, key)
        except BaseException as error:
            try:
                _restore_ciphertext(vault, old_ciphertext)
            except BaseException:
                _receipt(output, "API key rollback incomplete. Re-run apikey setup before starting; the saved credential state is uncertain.")
                return False
            if isinstance(error, (EOFError, KeyboardInterrupt)):
                _receipt(output, "Cancelled; saved API key restored.")
                return False
            if isinstance(error, CredentialError):
                _receipt(output, f"API key setup failed: {error.code}. Previous saved key restored.")
            else:
                _receipt(output, "API key setup failed: credential_save_failed. Previous saved key restored.")
            return False
        committed = True
        _receipt(output, "Saved OpenAI API key. Use start when ready; setup did not make a connection.")
        return True
    except (EOFError, KeyboardInterrupt):
        if committed:
            _receipt(output, "API key was saved before cancellation. Use start when ready.")
            return True
        _receipt(output, "Cancelled; saved API key unchanged.")
        return False
    except CredentialError as error:
        _receipt(output, f"API key setup failed: {error.code}. Re-run apikey setup before starting.")
        return False
    except ApiKeyWizardError as error:
        _receipt(output, f"API key setup failed: {error}. Review local configuration and re-run apikey setup.")
        return False
    except Exception:
        if committed:
            # A failed receipt cannot undo an already committed credential.
            return True
        _receipt(output, "API key setup failed: check configuration format and local file permissions.")
        return False
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception:
                _receipt(output, "API key setup lock cleanup failed; close this Console before using start.")
