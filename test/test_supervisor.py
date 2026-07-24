import asyncio

import pytest

from launch_ros.actions import Node

from rosmon2.control import ControlError
from rosmon2.model import ProcessRecord, State
from rosmon2.supervisor import Supervisor


class _FakeContext:
    """Minimal launch context stand-in for the process event callbacks."""

    def __init__(self):
        self.asyncio_loop = None


class _FakeAction:
    """A restart action carrying the record link the supervisor sets."""

    def __init__(self, record):
        self._rosmon2_record = record


class _StartEvent:
    def __init__(self, action, cmd, record):
        self.action = action
        self.cmd = cmd
        self.cwd = None
        self.env = None
        self.pid = 4321
        self.process_name = record.display_name


class _UnnamedNode(Node):
    @property
    def node_name(self):
        return '/ur10e/<node_name_unspecified>'


def test_display_names_do_not_include_the_root_slash():
    assert Supervisor._normalize_display_name('/talker') == 'talker'
    assert Supervisor._normalize_display_name('/robot/talker') == 'robot/talker'
    assert Supervisor._normalize_display_name('talker') == 'talker'


def test_unnamed_node_uses_its_process_name():
    action = object.__new__(_UnnamedNode)
    assert Supervisor._display_name(action, 'move_group-5') == 'ur10e/move_group'


def test_process_counter_removal_preserves_hyphens_in_names():
    assert Supervisor._process_name_without_counter('camera-driver-12') == 'camera-driver'
    assert Supervisor._process_name_without_counter('camera-driver') == 'camera-driver'


def test_namespace_mode_can_inspect_and_stop_a_group(monkeypatch):
    supervisor = Supervisor('', [], ui=False)
    root = ProcessRecord(key=0, display_name='hardware_setup')
    move_group = ProcessRecord(key=1, display_name='ur10e/move_group')
    command_server = ProcessRecord(
        key=2, display_name='ur10e/ur_ros_rtde/command_server')
    supervisor.records.extend([root, move_group, command_server])
    supervisor.ui.set_records(supervisor.records)

    supervisor.handle_key('F5')
    assert supervisor.ui.namespace_mode
    # Root is key a; ur10e is key b.
    supervisor.handle_key('b')
    supervisor.handle_key('i')
    assert supervisor.ui.namespace_inspect == 'ur10e'
    assert supervisor.ui.visible_records() == [move_group, command_server]

    supervisor.handle_key('b')
    supervisor.handle_key('m')
    assert command_server.muted
    assert supervisor.ui.namespace_inspect == 'ur10e'

    supervisor.handle_key('\x7f')
    stopped = []
    monkeypatch.setattr(supervisor, 'stop', stopped.append)
    supervisor.handle_key('b')
    supervisor.handle_key('k')
    assert stopped == [move_group, command_server]


def test_namespace_mode_can_mute_and_unmute_a_group():
    supervisor = Supervisor('', [], ui=False)
    root = ProcessRecord(key=0, display_name='hardware_setup')
    move_group = ProcessRecord(key=1, display_name='ur10e/move_group')
    command_server = ProcessRecord(
        key=2, display_name='ur10e/ur_ros_rtde/command_server')
    supervisor.records.extend([root, move_group, command_server])
    supervisor.ui.set_records(supervisor.records)
    supervisor.handle_key('F5')

    # Root is key a; ur10e is key b.
    supervisor.handle_key('b')
    supervisor.handle_key('m')
    assert not root.muted
    assert move_group.muted
    assert command_server.muted

    supervisor.handle_key('b')
    supervisor.handle_key('u')
    assert not move_group.muted
    assert not command_server.muted


def test_node_search_filters_navigates_and_selects_full_names():
    supervisor = Supervisor('', [], ui=False)
    receiver = ProcessRecord(
        key=0, display_name='ur10e/ur_ros_rtde/robot_state_receiver')
    server = ProcessRecord(
        key=1, display_name='ur10e/ur_ros_rtde/command_server')
    camera = ProcessRecord(key=2, display_name='external/camera')
    supervisor.records.extend([receiver, server, camera])
    supervisor.ui.set_records(supervisor.records)

    supervisor.handle_key('/')
    for key in 'ur_ros_rtde':
        supervisor.handle_key(key)

    assert supervisor.ui.search_active
    assert supervisor.ui.search_matches() == [receiver, server]
    supervisor.handle_key('\t')
    supervisor.handle_key('\n')
    assert not supervisor.ui.search_active
    assert supervisor.ui.selected == 1

    supervisor.handle_key('m')
    assert server.muted
    assert not receiver.muted


def test_node_search_backspace_and_escape_cancel():
    supervisor = Supervisor('', [], ui=False)
    supervisor.records.append(ProcessRecord(key=0, display_name='robot/driver'))
    supervisor.ui.set_records(supervisor.records)

    supervisor.handle_key('/')
    supervisor.handle_key('x')
    supervisor.handle_key('\x7f')
    assert supervisor.ui.search_query == ''

    supervisor.handle_key('ESC')
    assert not supervisor.ui.search_active
    assert supervisor.ui.selected is None


def test_control_status_and_namespace_mute_are_structured():
    supervisor = Supervisor('', [], ui=False, control=False)
    root = ProcessRecord(
        key=0, display_name='hardware_setup', state=State.RUNNING, pid=100)
    driver = ProcessRecord(
        key=1, display_name='ur10e/driver', state=State.CRASHED, return_code=2)
    helper = ProcessRecord(
        key=2, display_name='ur10e/helper', state=State.RUNNING, pid=101)
    supervisor.records.extend([root, driver, helper])

    status = asyncio.run(supervisor.control_request({'command': 'status'}))
    assert status['summary'] == {
        'total': 3,
        'idle': 0,
        'running': 2,
        'crashed': 1,
        'waiting': 0,
    }
    ur10e = next(item for item in status['namespaces'] if item['name'] == 'ur10e')
    assert (ur10e['alive'], ur10e['dead']) == (1, 1)

    result = asyncio.run(supervisor.control_request({
        'command': 'mute',
        'namespace': '/ur10e',
    }))
    assert result['matched'] == 2
    assert not root.muted
    assert driver.muted and helper.muted


def test_control_wait_returns_when_target_is_already_in_state():
    supervisor = Supervisor('', [], ui=False, control=False)
    supervisor.records.append(ProcessRecord(
        key=0, display_name='ur10e/driver', state=State.RUNNING, pid=100))

    result = asyncio.run(supervisor.control_request({
        'command': 'wait',
        'node': '/ur10e/driver',
        'state': 'running',
        'timeout': 0,
    }))

    assert result['matched'] == 1
    assert result['nodes'][0]['name'] == '/ur10e/driver'


def test_control_wait_reports_a_target_that_never_matched():
    supervisor = Supervisor('', [], ui=False, control=False)
    supervisor.records.append(ProcessRecord(key=0, display_name='ur10e/driver'))

    with pytest.raises(ControlError) as excinfo:
        asyncio.run(supervisor.control_request({
            'command': 'wait',
            'node': '/ur10e/ghost',
            'state': 'running',
            'timeout': 0,
        }))

    assert 'no process ever matched' in str(excinfo.value)


def test_on_start_does_not_overwrite_a_restart_actions_command():
    # ExecuteProcess.execute() starts asynchronously, so ProcessStarted for a
    # gdb (`d`) run arrives after debug() has restored record.cmd.  The command
    # captured from launch for our own restart actions must be ignored, or the
    # temporary gdb wrapper would stick and every later restart would run gdb.
    supervisor = Supervisor('', [], ui=False, control=False)
    record = ProcessRecord(key=0, display_name='driver', cmd=['/bin/echo', 'hi'])
    supervisor.records.append(record)
    action = _FakeAction(record)
    supervisor._by_action[action] = record

    supervisor._on_start(
        _StartEvent(action, ['gdb', '--args', '/bin/echo', 'hi'], record),
        _FakeContext())

    assert record.cmd == ['/bin/echo', 'hi']
    assert record.pid == 4321
    assert record.state is State.RUNNING


def test_debug_restores_the_original_command_after_starting_gdb(monkeypatch):
    supervisor = Supervisor('', [], ui=False, control=False)
    record = ProcessRecord(key=0, display_name='driver', cmd=['/bin/echo', 'hi'])
    supervisor.records.append(record)
    monkeypatch.setattr('rosmon2.supervisor.shutil.which',
                        lambda name: '/usr/bin/gdb')
    started = {}
    monkeypatch.setattr(supervisor, 'start',
                        lambda rec: started.setdefault('cmd', list(rec.cmd)))

    supervisor.debug(record)

    assert started['cmd'][:2] == ['gdb', '--args']
    assert record.cmd == ['/bin/echo', 'hi']


def test_emit_event_isolates_a_failing_listener():
    supervisor = Supervisor('', [], ui=False, control=False)
    delivered = []

    def broken(_event):
        raise RuntimeError('subscriber went away')

    supervisor.add_event_listener(broken)
    supervisor.add_event_listener(delivered.append)

    event = supervisor._emit_event('control_action', action='stop')

    assert delivered == [event]


def test_close_log_releases_the_handle_and_is_idempotent(tmp_path):
    log_path = tmp_path / 'combined.log'
    supervisor = Supervisor('', [], ui=False, control=False, log_file=str(log_path))
    assert supervisor._log_handle is not None

    supervisor.close_log()
    assert supervisor._log_handle is None
    supervisor.close_log()


def test_failed_run_still_releases_the_log_handle(tmp_path):
    log_path = tmp_path / 'combined.log'
    supervisor = Supervisor(
        '/nonexistent/does_not_exist.launch.py', ['not-a-valid-arg'],
        ui=False, control=False, log_file=str(log_path))

    with pytest.raises(Exception):
        asyncio.run(supervisor.run())

    assert supervisor._log_handle is None
