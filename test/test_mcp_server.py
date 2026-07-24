import io

from rosmon2 import mcp_server


def test_mcp_main_skips_blank_keepalive_lines(monkeypatch, capsys):
    monkeypatch.setattr(
        'sys.stdin',
        io.StringIO('\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n'))

    mcp_server.main()

    replies = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(replies) == 1
    assert '"error"' not in replies[0]
    assert '"id":1' in replies[0]


def test_mcp_advertises_rosmon2_tools():
    response = mcp_server._response({
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/list',
    })
    names = {tool['name'] for tool in response['result']['tools']}
    assert {
        'rosmon2_status',
        'rosmon2_logs',
        'rosmon2_wait',
        'rosmon2_start',
        'rosmon2_stop',
        'rosmon2_restart',
        'rosmon2_mute',
        'rosmon2_unmute',
    } <= names


def test_mcp_status_calls_control_session(monkeypatch):
    requests = []

    class FakeClient:
        def __init__(self, session, timeout):
            requests.append(('client', session, timeout))

        def request(self, request):
            requests.append(('request', request))
            return {'ok': True, 'nodes': []}

    monkeypatch.setattr(mcp_server, 'ControlClient', FakeClient)
    response = mcp_server._response({
        'jsonrpc': '2.0',
        'id': 2,
        'method': 'tools/call',
        'params': {
            'name': 'rosmon2_status',
            'arguments': {'session': 'hardware', 'namespace': '/ur10e'},
        },
    })

    assert not response['result']['isError']
    assert response['result']['structuredContent']['ok']
    assert requests == [
        ('client', 'hardware', 10),
        ('request', {'namespace': '/ur10e', 'command': 'status'}),
    ]


def test_mcp_mutation_requires_one_explicit_target():
    response = mcp_server._response({
        'jsonrpc': '2.0',
        'id': 3,
        'method': 'tools/call',
        'params': {'name': 'rosmon2_stop', 'arguments': {}},
    })
    assert response['result']['isError']
    assert 'exactly one' in response['result']['structuredContent']['error']
