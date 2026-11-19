"""Reuse a sleeping native speech helper, retaining the say command as fallback."""
import json
import os
import subprocess
import threading


def helper_path():
    source = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(source, '..', '..', 'MacOS', 'ATC Speech'),
                 os.path.join(source, '..', 'build', 'speech_service')):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.abspath(path)
    return None


class NativeSpeechSession(object):
    def __init__(self, path):
        self.path = path
        self.process = None
        self.disabled = False

    def request(self, command, properties):
        if self.process is None or self.process.poll() is not None:
            self.close()
            with open(os.devnull, 'wb') as errors:
                self.process = subprocess.Popen([self.path], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=errors)
        return _SpeechRequest(self, command, properties)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            process.wait()
            process.stdin.close()
            process.stdout.close()


class _SpeechRequest(object):
    def __init__(self, session, command, properties):
        self.session = session
        self.process = session.process
        self.command = command
        self.properties = properties
        self.returncode = None
        self.lock = threading.Lock()
        self.cancelled = False

    def communicate(self, payload):
        try:
            request = dict(self.properties, text=payload.decode('utf-8'))
            self.process.stdin.write((json.dumps(request) + '\n').encode('utf-8'))
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            response = json.loads(line.decode('utf-8'))
            if not response.get('ok'):
                raise IOError(response.get('error', 'native speech failed'))
            self.returncode = 0
            return b'', b''
        except (IOError, OSError, ValueError):
            # Serialize fallback creation with cancellation, just as the main
            # engine serializes speech startup with stop(). Never lose a purge.
            with self.lock:
                if self.cancelled:
                    self.returncode = -15
                    return b'', b''
                self.session.disabled = True
                self.session.close()
                self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = self.process.communicate(payload)
            self.returncode = self.process.returncode
            return output, error

    def poll(self):
        return self.process.poll()

    def terminate(self):
        with self.lock:
            self.cancelled = True
            if self.process.poll() is None:
                self.process.terminate()

    def kill(self):
        with self.lock:
            self.cancelled = True
            if self.process.poll() is None:
                self.process.kill()
