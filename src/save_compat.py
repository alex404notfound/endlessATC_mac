"""Import original Endless ATC saves without deserializing or converting them.

This module deliberately supports both Python 2.7 and Python 3. The native port
runs the original Python 2.7 game bytecode, so the original protocol-0 save files
remain its native format. Importing only copies files that do not already exist
in the destination; it never replaces Mac progress or edits the source folder.
"""
from __future__ import print_function

import argparse
import datetime
import errno
import fcntl
import hashlib
import io
import json
import os
import pickletools
import tempfile


MAX_SAVE_BYTES = 16 * 1024 * 1024
EXPECTED_CLASS = "__main__ GameData"
try:
    _STRING_TYPES = (basestring,)
except NameError:
    _STRING_TYPES = (str, bytes)
_DATA_OPCODES = frozenset((
    "MARK", "STOP", "POP", "POP_MARK", "DUP", "FLOAT", "INT", "LONG",
    "NONE", "STRING", "UNICODE", "APPEND", "BUILD", "DICT", "GET", "INST",
    "LIST", "PUT", "SETITEM", "TUPLE",
))


class InvalidLegacySave(ValueError):
    """A file is not a supported original Endless ATC save."""


class _StackValidator(object):
    """The symbolic stack/memo checks from pickletools.dis, without formatting.

    Uses the bundled interpreter's opcode metadata; never creates pickle objects.
    """
    def __init__(self):
        self.stack = []
        self.memo = {}
        self.marks = []

    def feed(self, opcode, argument):
        stack, memo = self.stack, self.memo
        mark = pickletools.markobject
        before, after = opcode.stack_before, opcode.stack_after
        count = len(before)
        if mark in before or (opcode.name == 'POP' and stack and stack[-1] is mark):
            if not self.marks:
                raise InvalidLegacySave('no MARK exists on stack')
            self.marks.pop()
            while stack[-1] is not mark:
                stack.pop()
            stack.pop()
            count = before.index(mark) if mark in before else 0
        if opcode.name == 'PUT':
            if argument in memo:
                raise InvalidLegacySave('memo key is already defined')
            if not stack or stack[-1] is mark:
                raise InvalidLegacySave('cannot store empty stack or MARK in memo')
            memo[argument] = stack[-1]
        elif opcode.name == 'GET':
            if argument not in memo:
                raise InvalidLegacySave('memo key has never been stored into')
            after = [memo[argument]]
        if len(stack) < count:
            raise InvalidLegacySave('save opcode underflows its stack')
        if count:
            del stack[-count:]
        if mark in after:
            self.marks.append(None)
        stack.extend(after)


def validate_legacy_save(data):
    """Statically validate a legacy pickle; no game objects are instantiated.

    Only primitive protocol-0 data and the original GameData instance are
    accepted. Callable globals, REDUCE, persistent IDs, and other classes are
    rejected before the game gets a chance to read a copied save.
    """
    if len(data) > MAX_SAVE_BYTES:
        raise InvalidLegacySave("save exceeds the 16 MiB import limit")
    count = 0
    instances = 0
    builds = 0
    last_name = None
    last_position = -1
    stack = _StackValidator()
    try:
        for opcode, argument, position in pickletools.genops(data):
            if opcode.proto != 0 or opcode.name not in _DATA_OPCODES:
                raise InvalidLegacySave("unsupported save opcode: " + opcode.name)
            if count == 0 and opcode.name != "MARK":
                raise InvalidLegacySave("save does not begin with a GameData instance")
            if count == 1 and (opcode.name != "INST" or argument != EXPECTED_CLASS):
                raise InvalidLegacySave("save does not begin with a GameData instance")
            if opcode.name == "INST":
                if argument != EXPECTED_CLASS:
                    raise InvalidLegacySave("unsupported save class: " + argument)
                instances += 1
            if opcode.name == "BUILD":
                builds += 1
            stack.feed(opcode, argument)
            count += 1
            last_name, last_position = opcode.name, position
        if instances != 1 or builds != 1:
            raise InvalidLegacySave("save must contain exactly one GameData state")
        if last_name != "STOP" or last_position != len(data) - 1:
            raise InvalidLegacySave("save is incomplete or contains trailing data")
        if stack.stack:
            raise InvalidLegacySave('stack is not empty after STOP')
    except InvalidLegacySave:
        raise
    except Exception as error:
        raise InvalidLegacySave("malformed legacy save: " + str(error))
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "protocol": 0,
        "class": "__main__.GameData",
        "opcodes": count,
    }


def _absolute_path(path):
    # Preserve Unicode filesystem paths under Python 2.7, while also accepting
    # pathlib objects when the build-time utility runs under Python 3.
    if not isinstance(path, _STRING_TYPES):
        path = str(path)
    return os.path.abspath(os.path.expanduser(path))


def _directory(path):
    path = _absolute_path(path)
    if not os.path.isdir(path):
        try:
            os.makedirs(path, 0o700)
        except OSError as error:
            if error.errno != errno.EEXIST or not os.path.isdir(path):
                raise
    return path


def _write_new_file(path, data):
    """Publish a complete file atomically, without replacing an existing path."""
    descriptor, temporary = tempfile.mkstemp(prefix=".save-import-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link is atomic and fails if another launch created the
            # destination meanwhile. Both paths are on the same filesystem.
            os.link(temporary, path)
        except OSError as error:
            if error.errno == errno.EEXIST:
                return False
            raise
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _update_manifest(path, source, imported):
    # Atomic rename prevents torn writes; the lock also prevents lost updates
    # when independent game launches import different slots concurrently.
    descriptor = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            _update_manifest_locked(path, source, imported)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update_manifest_locked(path, source, imported):
    manifest = {"version": 1, "imports": []}
    if os.path.isfile(path):
        try:
            with io.open(path, "r", encoding="utf-8") as stream:
                previous = json.load(stream)
            if previous.get("version") == 1 and isinstance(previous.get("imports"), list):
                manifest = previous
            else:
                raise ValueError('unsupported import manifest structure')
        except (IOError, ValueError, TypeError, AttributeError):
            # Preserve a malformed manifest rather than overwriting evidence.
            raise IOError("existing legacy import manifest is unreadable: " + path)
    manifest["imports"].append({
        "source_directory": source,
        "imported_at_utc": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "files": imported,
    })
    data = (json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".save-manifest-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_legacy_saves(source_dir, destination_dir):
    """Copy missing save slots into the Mac data folder, with original backups.

    Pass the platform helper's save_directory() as destination_dir. Repeated
    calls are safe: existing destination paths are always skipped, including
    newer game progress. The source folder is read-only throughout this call.

    Returns lists of imported/skipped names and an errors mapping; individual
    bad files do not prevent valid slots from being imported. Missing source
    directories are reported and do not cause a startup failure.
    """
    source = _absolute_path(source_dir)
    destination = _absolute_path(destination_dir)
    source_real = os.path.realpath(source)
    destination_real = os.path.realpath(destination)
    if destination_real == source_real or destination_real.startswith(source_real + os.sep):
        raise ValueError("the import destination must be outside the original save folder")
    result = {
        "source_directory": source,
        "destination_directory": destination,
        "imported": [],
        "skipped": [],
        "errors": {},
        "missing_source": not os.path.isdir(source),
    }
    if result["missing_source"]:
        return result
    _directory(destination)
    imported_records = []
    for name in sorted(os.listdir(source)):
        if not name.startswith("savegame"):
            continue
        original = os.path.join(source, name)
        target = os.path.join(destination, name)
        if os.path.lexists(target):
            result["skipped"].append(name)
            continue
        if not os.path.isfile(original) or os.path.islink(original):
            result["errors"][name] = "source is not a regular, non-symlink save file"
            continue
        try:
            with open(original, "rb") as stream:
                data = stream.read(MAX_SAVE_BYTES + 1)
            metadata = validate_legacy_save(data)
            backup_directory = _directory(os.path.join(destination, "legacy-originals"))
            backup_name = name + "." + metadata["sha256"][:16]
            backup = os.path.join(backup_directory, backup_name)
            if not _write_new_file(backup, data):
                with open(backup, "rb") as stream:
                    if hashlib.sha256(stream.read()).hexdigest() != metadata["sha256"]:
                        raise IOError("existing original backup has different contents")
            if not _write_new_file(target, data):
                result["skipped"].append(name)
                continue
            result["imported"].append(name)
            metadata["name"] = name
            metadata["backup"] = os.path.join("legacy-originals", backup_name)
            imported_records.append(metadata)
        except (IOError, OSError, InvalidLegacySave) as error:
            result["errors"][name] = str(error)
    if imported_records:
        manifest = os.path.join(destination, "legacy-import.json")
        try:
            _update_manifest(manifest, source, imported_records)
            result["manifest_path"] = manifest
        except (IOError, OSError) as error:
            result["errors"]["legacy-import.json"] = str(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="original Windows savegames directory")
    parser.add_argument("destination", help="Mac savegames directory")
    arguments = parser.parse_args(argv)
    result = import_legacy_saves(arguments.source, arguments.destination)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
