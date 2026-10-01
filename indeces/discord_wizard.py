"""Stopped-service Discord setup. No Gateway or model requests are made here."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from .config import load_config, snowflake
from .credentials import CredentialError, _check_secret_path, load_discord_token, prompt_secret, save_discord_token, secret_path, validate_token
from .lock import InstanceLock


class WizardError(ValueError):
    pass


def _complete(text):
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def _comment(line):
    # Only the final line of a complete value is used. Reject ambiguous triple
    # strings rather than guessing where a source comment starts.
    if '"""' in line or "'''" in line:
        raise WizardError("unsupported_multiline_string")
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return " " + line[index:].rstrip("\r\n")
    return ""


def patch_config(contents: bytes, guild_id: str, channels: tuple[str, ...]) -> bytes:
    """Replace only identity/scope values; check semantic equality of the rest.

    TOML headers/keys may be quoted and channel arrays may span lines. Prefix
    parsing keeps table-like text inside multiline strings out of the scanner.
    Unknown layouts fail before either persisted file is touched.
    """
    text = contents.decode("utf-8")
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    targets = {((), "name"): "Indeces", (("discord",), "guild_id"): guild_id,
               (("discord",), "channel_ids"): list(channels)}
    edits = []
    found = set()
    section = ()
    first_header = len(lines)
    discord_end = None
    key_pattern = re.compile(r'''^(\s*(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')\s*=\s*)''')
    for index, line in enumerate(lines):
        if not _complete("".join(lines[:index])):
            continue
        if re.match(r"^\s*\[.*\]\s*(?:#.*)?$", line.rstrip("\r\n")):
            try:
                tree = tomllib.loads(line)
                path = []
                while isinstance(tree, dict) and len(tree) == 1:
                    key, tree = next(iter(tree.items()))
                    path.append(key)
                if tree != {}:
                    raise WizardError("unsupported_table_layout")
            except tomllib.TOMLDecodeError:
                raise WizardError("unsupported_table_layout") from None
            first_header = min(first_header, index)
            if section == ("discord",):
                discord_end = index
            section = tuple(path)
            continue
        match = key_pattern.match(line)
        if not match:
            continue
        lhs = match.group(1)
        probe = tomllib.loads(lhs + '"probe"')
        key = next(iter(probe))
        target = (section, key)
        if target not in targets:
            continue
        end = index + 1
        while end <= len(lines) and not _complete("".join(lines[index:end])):
            end += 1
        if end > len(lines):
            raise WizardError("unsupported_value_layout")
        value = json.dumps(targets[target], ensure_ascii=False)
        suffix = _comment(lines[end - 1])
        edits.append((index, end, lhs + value + suffix + newline))
        found.add(target)
    if section == ("discord",):
        discord_end = len(lines)
    if discord_end is None:
        raise WizardError("unsupported_discord_layout")
    additions = ""
    for target, value in targets.items():
        if target in found:
            continue
        assignment = f"{target[1]} = {json.dumps(value, ensure_ascii=False)}{newline}"
        if target[0] == ():
            edits.append((first_header, first_header, assignment))
        else:
            additions += assignment
    if additions:
        prefix = newline if discord_end and not lines[discord_end - 1].endswith(("\n", "\r")) else ""
        edits.append((discord_end, discord_end, prefix + additions))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    result = "".join(lines)
    expected = copy.deepcopy(before)
    expected["name"] = "Indeces"
    expected["discord"]["guild_id"] = guild_id
    expected["discord"]["channel_ids"] = list(channels)
    if tomllib.loads(result) != expected:
        raise WizardError("configuration_scope_changed")
    return result.encode("utf-8")


def _atomic_write(path: Path, contents: bytes):
    temporary = None
    fd = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        stream = os.fdopen(fd, "wb")
        fd = None
        with stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _vault_snapshot(path):
    _check_secret_path(path)
    try:
        with path.open("rb") as stream:
            contents = stream.read(65537)
    except FileNotFoundError:
        return None
    if len(contents) > 65536:
        raise WizardError("credential_file_too_large")
    return contents


def _restore(path, contents):
    if contents is None:
        path.unlink(missing_ok=True)
    elif not path.exists() or path.read_bytes() != contents:
        _atomic_write(path, contents)


def configure_discord(config_path: Path, input_fn=None, secret_fn=None, output=None) -> bool:
    input_fn = input_fn or input
    secret_fn = secret_fn or prompt_secret
    output = output or print
    path = Path(config_path).resolve()
    lease = None
    committed = False
    try:
        original = path.read_bytes()
        config = load_config(path)
        lease = InstanceLock(config.state_dir)
        if path.read_bytes() != original:
            raise WizardError("configuration_changed_during_setup")
        vault = secret_path(path)
        old_ciphertext = _vault_snapshot(vault)
        old_token = None
        if old_ciphertext is not None:
            try:
                old_token = load_discord_token(path, config.discord.guild_id)
            except CredentialError as error:
                output(f"Saved token unavailable: {error.code}. Supply a replacement token to repair setup.")
        output("Indeces Discord bridge wizard. Guild ID means SERVER ID, not Application/Bot ID.")
        output("Enable Discord Developer Mode, then Copy Server ID. No connection is made during setup.")
        while True:
            value = input_fn(f"Guild ID [{config.discord.guild_id or 'required'}]: ").strip() or config.discord.guild_id
            try:
                guild_id = snowflake(value)
                break
            except ValueError:
                output("Enter a positive ASCII Discord snowflake ID (less than 2^64).")
        while True:
            value = input_fn("Channel IDs (comma/space separated; Enter keeps current; * means all): ").strip()
            try:
                channels = config.discord.channel_ids if not value else (() if value == "*" else
                    tuple(dict.fromkeys(snowflake(x) for x in re.split(r"[,\s]+", value) if x)))
                break
            except ValueError:
                output("Each channel ID must be a positive ASCII Discord snowflake ID.")
        while True:
            token = secret_fn("Bot token (hidden; Enter keeps saved token): ")
            if not token and old_token is not None:
                token = old_token
                break
            try:
                token = validate_token(token)
                break
            except CredentialError:
                output("Enter a nonempty Bot token without whitespace/control characters; it will stay hidden.")
        # Prepare and validate the full reviewable configuration before asking to save.
        updated = patch_config(original, guild_id, channels)
        output(f"Review: name=Indeces; Guild={guild_id}; channels={', '.join(channels) or 'all mentioned channels'}")
        output("Bot token: supplied (hidden). Save encrypted for the current Windows user. No service starts here.")
        if input_fn("Save this bridge configuration? [y/N]: ").strip().lower() not in {"y", "yes"}:
            output("Cancelled; configuration and saved token unchanged.")
            return False
        if path.read_bytes() != original or _vault_snapshot(vault) != old_ciphertext:
            raise WizardError("configuration_changed_during_setup")
        # Credential writes are atomic themselves. If the public config replacement
        # fails, restore the prior ciphertext; scope binding also guards crash splits.
        try:
            save_discord_token(path, guild_id, token)
            _atomic_write(path, updated)
        except BaseException as error:
            try:
                _restore(vault, old_ciphertext)
                _restore(path, original)
            except BaseException:
                output("Rollback incomplete. Re-run discord setup before starting the service.")
                return False
            if isinstance(error, (KeyboardInterrupt, EOFError)):
                output("Cancelled; configuration and saved token restored.")
                return False
            if isinstance(error, CredentialError):
                raise CredentialError(error.code) from None
            raise WizardError("configuration_save_failed") from None
        committed = True
        output("Saved. Use start to connect; OpenAI API key is requested separately if absent from the environment.")
        return True
    except (EOFError, KeyboardInterrupt):
        if committed:
            output("Setup was saved before cancellation. Use start when ready.")
            return True
        output("Cancelled; configuration and saved token unchanged.")
        return False
    except CredentialError as error:
        output(f"Bridge setup failed: {error.code}. Existing public configuration was not changed.")
        return False
    except WizardError as error:
        output(f"Bridge setup failed: {error}. Review local configuration and re-run discord setup.")
        return False
    except RuntimeError:
        output("Bridge setup unavailable: stop the running instance using this state directory first.")
        return False
    except (ValueError, OSError):
        output("Bridge setup failed: check configuration format and local file permissions.")
        return False
    finally:
        if lease is not None:
            lease.close()
