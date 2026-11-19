# -*- coding: utf-8 -*-
"""macOS services for the original Endless ATC Python runtime.

This module deliberately supports Python 2.7 as well as Python 3. The game
bytecode remains unchanged; its Linux speech API is supplied by an explicit
pyttsx shim backed by the speech service already included with macOS.
"""
from __future__ import unicode_literals

import atexit
import collections
import logging
import os
import re
import subprocess
import sys
import threading
import time
import types
import native_speech


try:
    text_type = unicode
except NameError:
    text_type = str

_LOGGER = logging.getLogger(__name__)


def _text(value):
    if isinstance(value, text_type):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return text_type(value)


def data_directory():
    """Return writable app data; the override supports isolated test runs."""
    override = os.environ.get('ENDLESS_ATC_DATA_DIR')
    directory = override or os.path.join(os.path.expanduser('~'), 'Library',
                                         'Application Support', 'Endless ATC')
    directory = os.path.abspath(os.path.expanduser(directory))
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError:
            if not os.path.isdir(directory):
                raise
    return directory


user_data_dir = data_directory


def _data_subdirectory(name):
    directory = os.path.join(data_directory(), name)
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError:
            if not os.path.isdir(directory):
                raise
    return directory


def save_directory():
    return _data_subdirectory('savegames')


def locations_directory():
    return _data_subdirectory('locations')


def resource_root():
    """Find resources in a frozen bundle or the development directory."""
    own_directory = os.path.dirname(os.path.abspath(__file__))
    candidates = [getattr(sys, '_MEIPASS', None),
                  os.environ.get('ENDLESS_ATC_RESOURCE_DIR'), own_directory,
                  os.path.dirname(own_directory),
                  os.path.dirname(os.path.dirname(own_directory))]
    for directory in candidates:
        if directory and os.path.isdir(os.path.join(directory, 'assets')):
            return os.path.abspath(directory)
    raise IOError('Cannot locate Endless ATC assets')


def resource_path(relative):
    root = resource_root()
    path = os.path.abspath(os.path.join(root, relative))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError('Resource path must remain inside the app resources')
    return path


def copy_to_clipboard(text):
    process = subprocess.Popen(['/usr/bin/pbcopy'], stdin=subprocess.PIPE)
    process.communicate(_text(text).encode('utf-8'))
    if process.returncode:
        raise IOError('macOS clipboard copy failed')


def install_clipboard_shim():
    """Redirect only the original game's Windows clipboard command.

    The bytecode constructs ``echo <coordinates>| clip`` on every desktop OS.
    Intercepting that exact form also keeps the coordinate string out of a shell.
    All unrelated subprocess calls retain their original behavior.
    """
    original_call = subprocess.call
    if getattr(original_call, '_endless_atc_clipboard_shim', False):
        return original_call

    def clipboard_call(command, *args, **kwargs):
        if (kwargs.get('shell') is True
                and isinstance(command, (str, text_type))
                and command.startswith('echo ') and command.endswith('| clip')):
            copy_to_clipboard(command[len('echo '):-len('| clip')])
            return 0
        return original_call(command, *args, **kwargs)

    clipboard_call._endless_atc_clipboard_shim = True
    subprocess.call = clipboard_call
    return clipboard_call


def open_path(path):
    return subprocess.Popen(['/usr/bin/open', os.path.abspath(path)])


class SpeechVoice(object):
    def __init__(self, name, locale):
        self.id = name
        self.name = name
        self.locale = locale
        # pyttsx's eSpeak driver uses a leading priority byte in this field.
        self.languages = ['\x05' + locale.replace('_', '-').lower()]
        self.gender = None
        self.age = None

    def __repr__(self):
        return 'SpeechVoice(%r, %r)' % (self.name, self.locale)


_voices = None
_voices_lock = threading.Lock()
_voice_mapping_source = None
_voice_mapping = {}


def installed_voices():
    """Read the installed voice list once, without downloading any voices."""
    global _voices
    with _voices_lock:
        if _voices is None:
            process = subprocess.Popen(['/usr/bin/say', '-v', '?'],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            output, error = process.communicate()
            if process.returncode:
                raise IOError('Cannot list macOS speech voices: ' + _text(error))
            voices = []
            for line in _text(output).splitlines():
                match = re.match(r'^(.+?)\s+([a-z]{2,3}_[A-Z]{2})\s+#', line)
                if match:
                    voices.append(SpeechVoice(match.group(1).strip(),
                                              match.group(2)))
            _voices = tuple(voices)
        return list(_voices)


def _plain_speech(text):
    # Prevent user-provided airport/callsign text from becoming speech commands.
    return _text(text).replace('[[', '[ [').replace(']]', '] ]')


def _resolve_voice(value):
    global _voice_mapping_source
    if not value:
        return None
    value = _text(value)
    if not re.match(r'^english(?:[+-]([mf])([1-9]))?$', value):
        return value
    if _voices is None:
        installed_voices()
    with _voices_lock:
        if _voice_mapping_source is not _voices:
            _voice_mapping.clear()
            _voice_mapping_source = _voices
        if value not in _voice_mapping:
            _voice_mapping[value] = _resolve_voice_uncached(value)
        return _voice_mapping[value]


def _resolve_voice_uncached(value):
    """Map the game's eSpeak English variants onto installed Mac voices."""
    if not value:
        return None
    value = _text(value)
    variant = re.match(r'^english(?:[+-]([mf])([1-9]))?$', value)
    if not variant:
        return value
    voices = [voice for voice in _voices
              if voice.locale.startswith('en_')]
    if not voices:
        return None
    family, number = variant.groups()
    if family == 'f':
        preferred = ['Samantha', 'Moira', 'Karen', 'Tessa', 'Victoria', 'Serena']
    else:
        preferred = ['Daniel', 'Alex', 'Fred', 'Ralph', 'Oliver', 'Reed']
    choices = []
    for name in preferred:
        for voice in voices:
            if voice.name == name or voice.name.startswith(name + ' ('):
                if voice.name not in choices:
                    choices.append(voice.name)
    if not choices:
        choices = [voice.name for voice in voices]
    return choices[(int(number or '1') - 1) % len(choices)]


class MacSpeechEngine(object):
    """The subset of pyttsx used by the game's existing Linux speech path.

    One daemon worker owns all speech subprocesses. Queue insertion and stop()
    share its lock, so a cancellation cannot miss a subprocess starting up.
    """

    def __init__(self, *args, **kwargs):
        self._condition = threading.Condition()
        self._queue = collections.deque()
        self._current = None
        self._process = None
        self._closed = False
        self._next_id = 0
        self._properties = {'voice': None, 'rate': 175, 'volume': 1.0}
        self.last_error = None
        helper = kwargs.get('helper_path', native_speech.helper_path())
        self._native_session = native_speech.NativeSpeechSession(helper) if helper else None
        self._thread = threading.Thread(target=self._work,
                                        name='Endless ATC speech')
        self._thread.daemon = True
        self._thread.start()
        atexit.register(self.close)

    def getProperty(self, name):
        if name == 'voices':
            return installed_voices()
        with self._condition:
            return self._properties[name]

    def setProperty(self, name, value):
        if name == 'rate':
            value = max(40, min(700, int(value)))
        elif name == 'volume':
            value = max(0.0, min(1.0, float(value)))
        elif name == 'voice':
            value = _resolve_voice(value)
        else:
            raise KeyError(name)
        with self._condition:
            self._properties[name] = value

    def say(self, text, name=None):
        with self._condition:
            if self._closed:
                raise RuntimeError('The speech engine is closed')
            self._next_id += 1
            stream_id = self._next_id
            properties = self._properties.copy()
            self._queue.append((stream_id, _plain_speech(text), properties))
            self._condition.notifyAll()
            return stream_id

    def startLoop(self, useDriverLoop=True):
        # A dedicated worker supplies the external loop requested by the game.
        if useDriverLoop:
            self.runAndWait()

    def endLoop(self):
        self.stop()

    def iterate(self):
        # Original Linux code pumps pyttsx here; our worker pumps itself.
        return None

    def Speak(self, text, flags=1):
        # The game also uses SAPI's purge call on its Linux speech object.
        if flags & 2:
            self.stop()
        if not _text(text).strip():
            return 0
        stream_id = self.say(text)
        if not flags & 1:
            self.runAndWait()
        return stream_id

    def Pause(self):
        self.stop()

    def isBusy(self):
        with self._condition:
            return self._current is not None or bool(self._queue)

    def runAndWait(self):
        return self.wait_until_done()

    def wait_until_done(self, timeout=None):
        deadline = None if timeout is None else time.time() + timeout
        with self._condition:
            while self._current is not None or self._queue:
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self):
        with self._condition:
            self._queue.clear()
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            self._condition.notifyAll()

    cancel = stop

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self.stop()
            self._condition.notifyAll()
        if threading.current_thread() is not self._thread:
            self._thread.join(1.0)
        with self._condition:
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.kill()
                except OSError:
                    pass

        if self._native_session is not None:
            self._native_session.close()

    def _work(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                self._current = self._queue.popleft()
                stream_id, text, properties = self._current
                command = ['/usr/bin/say', '-r', str(properties['rate']), '-f', '-']
                if properties['voice']:
                    command.extend(['-v', properties['voice']])
                payload = '[[volm %.3f]]%s' % (properties['volume'], text)
                try:
                    if self._native_session is not None and not self._native_session.disabled:
                        try:
                            self._process = self._native_session.request(command, properties)
                        except OSError:
                            self._native_session.disabled = True
                            self._process = subprocess.Popen(command,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
                    else:
                        self._process = subprocess.Popen(command,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
                except (OSError, ValueError) as error:
                    self.last_error = error
                    self._current = None
                    self._condition.notifyAll()
                    _LOGGER.warning('Could not start macOS speech: %s', error)
                    continue
                process = self._process
            try:
                output, error = process.communicate(payload.encode('utf-8'))
                if process.returncode and process.returncode > 0:
                    self.last_error = IOError(_text(error).strip())
                    _LOGGER.warning('macOS speech failed: %s', self.last_error)
            except (IOError, OSError) as error:
                self.last_error = error
                _LOGGER.warning('macOS speech failed: %s', error)
            finally:
                with self._condition:
                    self._process = None
                    self._current = None
                    self._condition.notifyAll()


def install_pyttsx_shim():
    """Install the compatibility module before executing original bytecode."""
    module = types.ModuleType(str('pyttsx'))
    module.init = lambda *args, **kwargs: MacSpeechEngine()
    module.Engine = MacSpeechEngine
    sys.modules['pyttsx'] = module
    return module
