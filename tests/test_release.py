"""Release metadata and privacy guarantees; no real clipboard or user log access."""

import logging
from logging.handlers import RotatingFileHandler
import sys

from sigmarker import diagnostics
from sigmarker.metadata import APP_VERSION


def test_error_log_omits_private_exception_text_and_source(tmp_path):
    logger = logging.getLogger('sigmarker')
    handler = logging.FileHandler(tmp_path / 'test.log', encoding='utf-8')
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        try:
            raise ValueError('private-signature.png?token=private-token')
        except ValueError as error:
            diagnostics.record_error('load', error)
        handler.flush()
        content = (tmp_path / 'test.log').read_text(encoding='utf-8')
        assert 'load: ValueError' in content and 'test_release.py:' in content
        assert 'private-signature' not in content and 'private-token' not in content
        assert str(tmp_path) not in content and 'raise ValueError' not in content
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def test_diagnostics_copy_has_version_without_document_or_user_details():
    content = diagnostics.environment_info()
    assert f'SigMarker V{APP_VERSION}' in content and 'Qt:' in content
    assert 'AGPL-3.0-only' in content
    assert 'token' not in content.lower() and 'Users' not in content


def test_unwritable_log_directory_does_not_block_startup(monkeypatch):
    class Unwritable:
        def mkdir(self, **kwargs):
            raise PermissionError('synthetic failure')
    monkeypatch.setattr(diagnostics, 'log_directory', lambda: Unwritable())
    original_hook = sys.excepthook
    diagnostics.configure()
    assert sys.excepthook is original_hook


def test_log_rotation_and_exception_hooks_use_isolated_directory(tmp_path, monkeypatch):
    import threading
    logger = logging.getLogger('sigmarker')
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    monkeypatch.setattr(diagnostics, 'log_directory', lambda: tmp_path)
    monkeypatch.setattr(sys, 'excepthook', lambda *_: None)
    monkeypatch.setattr(threading, 'excepthook', lambda *_: None)
    try:
        diagnostics.configure()
        diagnostics.configure()
        handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        handler = handlers[0]
        assert handler.maxBytes == 512 * 1024 and handler.backupCount == 2
        try:
            raise RuntimeError('private-token')
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        handler.flush()
        text = (tmp_path / 'sigmarker.log').read_text(encoding='utf-8')
        assert APP_VERSION in text and 'unhandled: RuntimeError' in text
        assert 'private-token' not in text
    finally:
        for handler in logger.handlers[:]:
            if handler not in previous_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(previous_level)
