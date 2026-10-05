"""``ansible-deploy trust``: scan an environment's SSH host key and record it.

The scanned fingerprints are written back into the committed environment config.
Only the ``host_key_fingerprints`` block is rewritten as text, so the rest of the
file — including its comments — survives byte for byte; PyYAML cannot round-trip
comments, and a full re-dump would silently destroy them.
"""

import copy
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode

from .config import ConfigurationError, load_configuration
from .redaction import Redactor
from .runner import AnsibleRunner

TRUST_ENVIRONMENTS = ("stage", "prod", "monitoring", "restore")
_FINGERPRINTS_KEY = "host_key_fingerprints"
_SERVER_KEY = "server"


@dataclass(frozen=True)
class _Region:
    """Lines of the environment config that hold the fingerprints block."""

    start: int
    stop: int
    indent: str
    newline: str


def environment_config_relative(environment: str) -> str:
    return f".deploy/environments/{environment}/config.yml"


def _environment_config_path(project_dir: Path, environment: str) -> Path:
    relative = environment_config_relative(environment)
    path = project_dir / relative
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"Environment configuration is missing: {relative}")
    return path


def trust_environment(
    project_dir: Path,
    environment: str,
    *,
    verbose: bool = False,
    print_only: bool = False,
    force: bool = False,
) -> int:
    """Scan the environment's SSH host key and record its fingerprints."""
    _, config = load_configuration(project_dir, environment)
    path = _environment_config_path(project_dir, environment)
    runner = AnsibleRunner(
        project_dir,
        Redactor([str(config.server.ssh_key)]),
        environment=environment,
        verbose=verbose,
    )
    runner.build_image()
    scanned = runner.scan_host_keys(config.server.host, config.server.ssh_port)
    for key in scanned:
        print(f"[FINGERPRINT] {key.key_type} {key.fingerprint}")
    discovered = sorted({key.fingerprint for key in scanned})
    configured = sorted(set(config.server.host_key_fingerprints))
    if configured and configured == discovered:
        print(f"[OK] {environment} host key fingerprints are unchanged")
        return 0
    if print_only:
        if configured:
            print(
                f"[WARN] scanned {environment} host key differs from the trusted "
                "fingerprints; nothing was written",
            )
        print(f"[PRINT] {environment_config_relative(environment)} was not modified")
        return 0
    if configured and not force:
        raise ConfigurationError(
            f"Scanned SSH host key does not match the fingerprints already trusted for "
            f"{environment}; the server may have been rebuilt or the connection "
            f"intercepted. Verify the key through the provider console, then rerun "
            f"'ansible-deploy trust {environment} --force' to overwrite it"
        )
    _write_fingerprints(path, discovered)
    print(f"[UPDATE] {environment_config_relative(environment)} {_FINGERPRINTS_KEY}")
    return 0


def _mapping_entries(node: MappingNode, *, label: str) -> list[tuple[str, ScalarNode, Node]]:
    entries: list[tuple[str, ScalarNode, Node]] = []
    seen: set[str] = set()
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or not isinstance(key.value, str):
            raise ConfigurationError(f"{label} must use explicit scalar keys")
        if key.value in seen:
            raise ConfigurationError(f"{label} contains duplicate key {key.value!r}")
        seen.add(key.value)
        entries.append((key.value, key, value))
    return entries


def _comment_or_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _fingerprints_region(text: str, lines: list[str]) -> _Region:
    """Locate the lines to replace, keeping surrounding comments outside the region."""
    try:
        document = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid environment configuration: {exc}") from exc
    if not isinstance(document, MappingNode):
        raise ConfigurationError("Environment configuration must be a mapping")
    top = _mapping_entries(document, label="Environment configuration")
    server_index = next(
        (index for index, entry in enumerate(top) if entry[0] == _SERVER_KEY), None
    )
    if server_index is None:
        raise ConfigurationError("Environment configuration must contain a server mapping")
    server_node = top[server_index][2]
    if not isinstance(server_node, MappingNode):
        raise ConfigurationError("Environment configuration must contain a server mapping")
    entries = _mapping_entries(server_node, label="Environment server")
    if not entries:
        raise ConfigurationError("Environment server mapping must declare keys")
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = " " * entries[0][1].start_mark.column
    after_server = (
        top[server_index + 1][1].start_mark.line
        if server_index + 1 < len(top)
        else len(lines)
    )
    index = next(
        (position for position, entry in enumerate(entries) if entry[0] == _FINGERPRINTS_KEY),
        None,
    )
    if index is None:
        # The key may be absent entirely; append it as the server mapping's last key.
        start = stop = after_server
    else:
        start = entries[index][1].start_mark.line
        stop = (
            entries[index + 1][1].start_mark.line
            if index + 1 < len(entries)
            else after_server
        )
        if stop <= start:
            raise ConfigurationError(
                f"Environment server {_FINGERPRINTS_KEY} must be one block-style key"
            )
    while stop - 1 >= start and stop - 1 < len(lines) and _comment_or_blank(lines[stop - 1]):
        stop -= 1
    return _Region(start=start, stop=stop, indent=indent, newline=newline)


def _fingerprints_block(fingerprints: list[str], region: _Region) -> str:
    if not fingerprints:
        return f"{region.indent}{_FINGERPRINTS_KEY}: []{region.newline}"
    items = "".join(
        f"{region.indent}  - {fingerprint}{region.newline}" for fingerprint in fingerprints
    )
    return f"{region.indent}{_FINGERPRINTS_KEY}:{region.newline}{items}"


def _only_fingerprints_changed(original: bytes, updated: bytes, fingerprints: list[str]) -> None:
    """Prove the rewritten file differs from the original in exactly one value."""
    try:
        before: Any = yaml.safe_load(original.decode("utf-8"))
        after: Any = yaml.safe_load(updated.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Invalid environment configuration: {exc}") from exc
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ConfigurationError("Environment configuration must be a mapping")
    expected = copy.deepcopy(before)
    server = expected.get(_SERVER_KEY)
    if not isinstance(server, dict):
        raise ConfigurationError("Environment configuration must contain a server mapping")
    server[_FINGERPRINTS_KEY] = fingerprints
    if after != expected:
        raise ConfigurationError("Environment configuration rewrite changed unrelated values")


def _write_fingerprints(path: Path, fingerprints: list[str]) -> None:
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeError:
        raise ConfigurationError("Environment configuration must be UTF-8") from None
    lines = text.splitlines(keepends=True)
    region = _fingerprints_region(text, lines)
    updated = "".join(
        [*lines[: region.start], _fingerprints_block(fingerprints, region), *lines[region.stop :]]
    ).encode("utf-8")
    _only_fingerprints_changed(original, updated, fingerprints)
    _atomic_replace(path, original, updated)


def _atomic_replace(path: Path, original: bytes, updated: bytes) -> None:
    """Replace the config in one step, never leaving a partially written file behind."""
    mode = path.stat().st_mode
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.candidate.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.read_bytes() != original:
            raise ConfigurationError("Environment configuration changed while it was rewritten")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if path.read_bytes() != updated:
        raise ConfigurationError("Environment configuration rewrite could not be verified")
