"""Local, bounded diagnostics without image data, paths or exception messages."""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import threading
import traceback

from .metadata import APP_NAME, APP_VERSION, APP_LICENSE

_logger = logging.getLogger('sigmarker')
_logger.addHandler(logging.NullHandler())
_logger.propagate = False


def log_directory():
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'state')
    return base / APP_NAME / 'logs'


def environment_info():
    """User-controlled copy; never includes usernames, input paths or network tokens."""
    from PyQt6.QtCore import qVersion, PYQT_VERSION_STR
    return '\n'.join((
        f'{APP_NAME} V{APP_VERSION}', f'License: {APP_LICENSE}',
        f'Windows: {platform.win32_ver()[0]} {platform.win32_ver()[1]}',
        f'Architecture: {platform.machine()}', f'Python: {platform.python_version()}',
        f'Qt: {qVersion()}; PyQt: {PYQT_VERSION_STR}',
        f'Packaged: {bool(getattr(sys, "frozen", False))}',
    ))


def record_error(operation, error):
    # Do not format exception text or source lines: both can contain private data.
    frames = traceback.extract_tb(error.__traceback__)
    locations = ' > '.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames)
    _logger.error('%s: %s; %s', operation, type(error).__name__, locations)


def configure():
    """Install best-effort logging before Qt loads; failure must not prevent startup."""
    if any(isinstance(handler, RotatingFileHandler) for handler in _logger.handlers):
        return
    try:
        folder = log_directory()
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / 'sigmarker.log', maxBytes=512 * 1024,
                                      backupCount=2, encoding='utf-8')
    except OSError:
        return
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.info('%s V%s; Python %s; %s', APP_NAME, APP_VERSION,
                 platform.python_version(), platform.machine())
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def exception_hook(kind, error, tb):
        record_error('unhandled', error)
        previous_hook(kind, error, tb)

    def thread_hook(args):
        record_error('thread', args.exc_value)
        previous_thread_hook(args)

    sys.excepthook = exception_hook
    threading.excepthook = thread_hook
