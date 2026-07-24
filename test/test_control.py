import asyncio
import json
import os

import pytest

from rosmon2.control import (
    ControlError,
    ControlServer,
    session_socket_path,
    validate_session_name,
    verify_private_directory,
)


class FakeSupervisor:
    def __init__(self):
        self.listeners = []

    def add_event_listener(self, listener):
        self.listeners.append(listener)

    def remove_event_listener(self, listener):
        self.listeners.remove(listener)

    async def control_request(self, request):
        return {'ok': True, 'echo': request}


def test_session_socket_uses_private_runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setenv('ROSMON2_RUNTIME_DIR', str(tmp_path))
    assert session_socket_path('hardware') == tmp_path / 'hardware.sock'


def test_session_name_rejects_path_characters():
    try:
        validate_session_name('../hardware')
    except ValueError as exc:
        assert 'session names' in str(exc)
    else:
        raise AssertionError('unsafe session name was accepted')


def test_verify_private_directory_accepts_a_missing_or_private_path(tmp_path):
    verify_private_directory(tmp_path / 'not_created_yet')

    private = tmp_path / 'rt'
    private.mkdir()
    os.chmod(private, 0o700)
    verify_private_directory(private)


def test_verify_private_directory_rejects_a_world_accessible_path(tmp_path):
    shared = tmp_path / 'rt'
    shared.mkdir()
    os.chmod(shared, 0o777)

    with pytest.raises(ControlError) as excinfo:
        verify_private_directory(shared)

    assert 'accessible to other users' in str(excinfo.value)


def test_verify_private_directory_rejects_a_non_directory(tmp_path):
    plain = tmp_path / 'rt'
    plain.write_text('')

    with pytest.raises(ControlError):
        verify_private_directory(plain)


def test_control_server_rejects_a_world_accessible_runtime_directory(
        monkeypatch, tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    os.chmod(shared, 0o777)
    monkeypatch.setenv('ROSMON2_RUNTIME_DIR', str(shared))

    async def scenario():
        server = ControlServer(FakeSupervisor(), 'sess')
        with pytest.raises(ControlError):
            await server.start()

    asyncio.run(scenario())


def test_control_server_handles_requests_and_streams_events(monkeypatch, tmp_path):
    monkeypatch.setenv('ROSMON2_RUNTIME_DIR', str(tmp_path))

    async def scenario():
        supervisor = FakeSupervisor()
        server = ControlServer(supervisor, 'test')
        await server.start()
        assert server.path.exists()

        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(b'{"command":"status"}\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        assert response['ok']
        assert response['echo'] == {'command': 'status'}
        writer.close()
        await writer.wait_closed()

        event_reader, event_writer = await asyncio.open_unix_connection(str(server.path))
        event_writer.write(b'{"command":"events"}\n')
        await event_writer.drain()
        acknowledgement = json.loads(await event_reader.readline())
        assert acknowledgement['subscribed']
        server.publish({'event': 'node_started', 'sequence': 1})
        event = json.loads(await event_reader.readline())
        assert event == {'event': 'node_started', 'sequence': 1}
        event_writer.close()
        await event_writer.wait_closed()

        await server.close()
        assert not server.path.exists()

    asyncio.run(scenario())
