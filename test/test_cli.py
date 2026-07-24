import os
import stat

import pytest

from rosmon2.cli import _default_log_file, resolve_launch_spec


def test_resolve_file_and_arguments(tmp_path):
    launch_file = tmp_path / 'example.launch.py'
    launch_file.write_text('')
    path, arguments = resolve_launch_spec([str(launch_file), 'answer:=42'])
    assert path == str(launch_file.resolve())
    assert arguments == ['answer:=42']


def test_rejects_too_many_specifiers():
    with pytest.raises(ValueError):
        resolve_launch_spec(['one', 'two', 'three'])


def test_default_log_file_is_unique_and_private():
    first = _default_log_file()
    second = _default_log_file()
    try:
        assert first != second
        assert os.path.basename(first).startswith('rosmon2_')
        assert first.endswith('.log')
        info = os.lstat(first)
        # A real file rosmon2 created itself, readable and writable only by us,
        # rather than a predictable name an attacker could pre-plant.
        assert stat.S_ISREG(info.st_mode)
        assert info.st_mode & 0o077 == 0
    finally:
        os.unlink(first)
        os.unlink(second)
