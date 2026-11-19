# -*- coding: utf-8 -*-
"""Run the installed game's original bytecode on a native Python 2.7 runtime.

The launcher supplies writable paths and macOS services. It does not translate,
reconstruct, or modify any game code objects.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import logging
import marshal
import os
import shutil
import sys
import time
import types

import mac_platform
import save_compat
import display_compat
import fast_forward_compat
import power_compat
import render_compat

_LOGGER = logging.getLogger('endless_atc.launcher')


class _FlushedStream(object):
    def __init__(self, stream):
        self.stream = stream

    def write(self, value):
        result = self.stream.write(value)
        self.stream.flush()
        return result

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _mkdir(path):
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            if not os.path.isdir(path):
                raise


def _same_file_content(left, right):
    if not os.path.isfile(right) or os.path.getsize(left) != os.path.getsize(right):
        return False
    def digest(path):
        result = hashlib.sha256()
        with open(path, 'rb') as stream:
            for block in iter(lambda: stream.read(64 * 1024), b''):
                result.update(block)
        return result.digest()
    return digest(left) == digest(right)


def _copy_tree(source, destination, replace=False):
    copied = []
    if not os.path.isdir(source):
        return copied
    if os.path.realpath(source) == os.path.realpath(destination):
        return copied
    if os.path.islink(destination):
        # Remove the generated link, never write through it into the source.
        os.unlink(destination)
    _mkdir(destination)
    for root, directories, files in os.walk(source):
        directories[:] = [name for name in directories if not name.startswith('.')]
        relative = os.path.relpath(root, source)
        target_root = destination if relative == '.' else os.path.join(destination, relative)
        _mkdir(target_root)
        for name in files:
            if name.startswith('.'):
                continue
            original = os.path.join(root, name)
            target = os.path.join(target_root, name)
            if os.path.exists(target) and (not replace or _same_file_content(original, target)):
                continue
            shutil.copy2(original, target)
            copied.append(os.path.relpath(target, destination))
    return copied


def prepare_game_directory(game_directory, import_directory=None):
    """Stage supplied assets and import user data without editing its source."""
    game_directory = os.path.abspath(os.path.expanduser(game_directory))
    source_assets = os.path.join(game_directory, 'assets')
    if not os.path.isdir(source_assets):
        raise IOError('The game directory must contain the original assets folder: ' + game_directory)
    destination = mac_platform.data_directory()
    copied_assets = _copy_tree(source_assets, os.path.join(destination, 'assets'), replace=True)
    source_data = import_directory or game_directory
    source_data = os.path.abspath(os.path.expanduser(source_data))
    copied_locations = _copy_tree(os.path.join(source_data, 'locations'),
                                  mac_platform.locations_directory(), replace=False)
    destination_saves = mac_platform.save_directory()
    source_saves = os.path.join(source_data, 'savegames')
    if os.path.realpath(source_saves) == os.path.realpath(destination_saves):
        save_report = {'imported': [], 'skipped': [], 'errors': [], 'same_directory': True}
    else:
        save_report = save_compat.import_legacy_saves(source_saves, destination_saves)
    report = {'data_directory': destination,
              'asset_source': source_assets,
              'import_source': source_data,
              'copied_assets': copied_assets,
              'copied_locations': copied_locations,
              'saves': save_report}
    _LOGGER.info('Prepared game data: %s', json.dumps(report, sort_keys=True))
    return destination, report


def load_original_code(payload_path):
    if sys.version_info[:2] != (2, 7):
        raise RuntimeError('Original game bytecode requires the bundled native Python 2.7 runtime')
    with open(payload_path, 'rb') as stream:
        payload = stream.read()
    objects = marshal.loads(payload)
    matches = [code for code in objects if isinstance(code, types.CodeType)
               and os.path.basename(code.co_filename.replace('\\', '/')) == 'eatc.py']
    if len(matches) != 1:
        raise ValueError('Expected exactly one original eatc.py game module')
    _LOGGER.info('Loaded unmodified original game bytecode: sha256=%s, %d bytes',
                 hashlib.sha256(payload).hexdigest(), len(payload))
    return matches[0]


def _schedule_smoke_exit(seconds, namespace, screenshot=None):
    """Capture and queue QUIT from the SDL rendering thread, never a timer."""
    import pygame
    status = {'display_verified': False, 'quit_posted': False, 'error': None}
    deadline = time.time() + max(1.0, seconds)

    def finish_if_ready():
        if status['quit_posted'] or time.time() < deadline:
            return
        try:
            surface = pygame.display.get_surface()
            if surface is None:
                raise RuntimeError('Smoke check could not find an initialized game display')
            colors = set(tuple(surface.get_at((x, y))) for x in range(0, surface.get_width(), max(1, surface.get_width() // 25))
                         for y in range(0, surface.get_height(), max(1, surface.get_height() // 25)))
            if len(colors) < 2:
                raise RuntimeError('Smoke check found no drawn game content')
            status['display_verified'] = True
            _LOGGER.info('Smoke check display=%s, window_points=%s, driver=%s, version=%s, airport=%s',
                         surface.get_size(), pygame.display.get_window_size(),
                         pygame.display.get_driver(), namespace.get('version'),
                         namespace.get('airport', namespace.get('location')))
            radar_font = namespace.get('font')
            _LOGGER.info('Game layout dpi=%s, control panel pixels=%s, radar text line pixels=%s',
                         namespace.get('dpi'), namespace.get('xoff'),
                         radar_font.get_linesize() if radar_font else None)
            if screenshot:
                pygame.image.save(surface, screenshot)
                _LOGGER.info('Saved smoke screenshot: %s', screenshot)
        except Exception as error:
            status['error'] = str(error)
            _LOGGER.exception('Could not finish smoke check')
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        status['quit_posted'] = True

    def hook(original):
        def checked_frame(*args, **kwargs):
            result = original(*args, **kwargs)
            finish_if_ready()
            return result
        return checked_frame

    pygame.display.flip = hook(pygame.display.flip)
    pygame.display.update = hook(pygame.display.update)
    # Static paused screens may not present another frame before the deadline.
    # Event processing is on the same SDL thread and can verify the last frame.
    pygame.event.get = hook(pygame.event.get)
    return status


def _configure_smoke_driver():
    import pygame
    if os.environ.get('SDL_VIDEODRIVER') == 'dummy':
        # SDL's headless driver has no hardware cursor. This test-only adapter
        # lets the original game render without pretending cursor support works.
        pygame.mouse.set_cursor = lambda *args, **kwargs: None


def _verify_smoke_status(status):
    if status is not None:
        if status['error'] or not status['display_verified'] or not status['quit_posted']:
            raise RuntimeError(status['error'] or 'Game exited before its smoke check completed')
        _LOGGER.info('Smoke check passed: rendered game content and processed quit event')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-dir', required=True,
                        help='Directory containing the original assets folder')
    parser.add_argument('--payload', required=True,
                        help='Unmodified PYTHONSCRIPT marshal stream')
    parser.add_argument('--import-from', default=os.environ.get('ENDLESS_ATC_IMPORT_FROM'),
                        help='Optional original Windows directory containing saves and locations')
    parser.add_argument('--smoke', nargs='?', const=5.0, type=float,
                        help='Run with dummy display/audio and quit after the given seconds')
    parser.add_argument('--screenshot', help='Save the game display during the smoke check')
    args = parser.parse_args(argv)
    if sys.stdout is not None:
        sys.stdout = _FlushedStream(sys.stdout)
    if sys.stderr is not None:
        sys.stderr = _FlushedStream(sys.stderr)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    payload_path = os.path.abspath(os.path.expanduser(args.payload))
    if args.smoke is not None:
        os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
        os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
        _configure_smoke_driver()
    directory, report = prepare_game_directory(args.game_dir, args.import_from)
    mac_platform.install_pyttsx_shim()
    if hasattr(mac_platform, 'install_clipboard_shim'):
        mac_platform.install_clipboard_shim()
    code = load_original_code(payload_path)
    namespace = sys.modules['__main__'].__dict__
    synthetic_file = os.path.join(directory, 'eatc.py')
    namespace['__file__'] = synthetic_file
    namespace['__name__'] = '__main__'
    namespace['__package__'] = None
    namespace['__endless_atc_data_directory__'] = directory
    # This is an embedded interpreter, not a frozen Python executable. The
    # original module should resolve resources from its supplied __file__.
    if hasattr(sys, 'frozen'):
        delattr(sys, 'frozen')
    display_compat.install_display_compat(namespace)
    render_compat.install_render_compat()
    power_compat.install_power_compat(namespace)
    fast_forward_compat.install_fast_forward_compat(namespace)
    sys.argv = [synthetic_file]
    os.chdir(directory)
    smoke_status = None
    if args.smoke is not None:
        smoke_status = _schedule_smoke_exit(args.smoke, namespace,
                            os.path.abspath(args.screenshot) if args.screenshot else None)
    _LOGGER.info('Starting original Endless ATC from %s', directory)
    # eval accepts an exec-mode code object on Python 2.7 and preserves the
    # __main__ identity required by existing GameData pickle instances.
    try:
        eval(code, namespace, namespace)
    except SystemExit as exit_request:
        if args.smoke is None or exit_request.code not in (None, 0):
            raise
    _verify_smoke_status(smoke_status)
    return 0


if __name__ == '__main__':
    sys.exit(main())
