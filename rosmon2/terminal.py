"""Small ANSI terminal UI patterned after rosmon's interface."""

from math import cos, pi, sin
import os
import re
import shutil
import sys
import termios
import tty
from typing import Callable, Iterable, Optional

from .model import namespace_of, ProcessRecord, selection_key, State


ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SEVERITY_RE = re.compile(r'\[(DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]')
RGB_MATRIX = (
    (3.2406, -1.5372, -0.4986),
    (-0.9689, 1.8758, 0.0415),
    (0.0557, -0.2040, 1.0570),
)
KEY_SEQUENCES = {
    '\x1b[15~': 'F5', '\x1b[17~': 'F6', '\x1b[18~': 'F7', '\x1b[19~': 'F8',
    '\x1b[20~': 'F9', '\x1b[21~': 'F10',
    '\x1b[A': 'UP', '\x1b[B': 'DOWN',
    '\x1b[C': 'RIGHT', '\x1b[D': 'LEFT',
}
SEVERITY_STYLES = {
    'DEBUG': '\x1b[32m',
    'WARNING': '\x1b[33m',
    'ERROR': '\x1b[31m',
    'FATAL': '\x1b[1;31m',
}


def _hsluv_label_color(hue: float):
    """Return rosmon's HSLuv(H, 100, 20) process-label color."""
    lightness = 20.0
    hue_radians = hue / 360.0 * 2.0 * pi
    sin_hue = sin(hue_radians)
    cos_hue = cos(hue_radians)
    sub1 = (lightness + 16.0) ** 3 / 1560896.0
    sub2 = sub1 if sub1 > 0.008856 else lightness / 903.3
    max_chroma = float('inf')
    for m1, m2, m3 in RGB_MATRIX:
        top = (0.99915 * m1 + 1.05122 * m2 + 1.14460 * m3) * sub2
        right = 0.86330 * m3 - 0.17266 * m2
        left = 0.12949 * m3 - 0.38848 * m1
        bottom = (right * sin_hue + left * cos_hue) * sub2
        for boundary in (0.0, 1.0):
            chroma = lightness * (top - 1.05122 * boundary)
            chroma /= bottom + 0.17266 * sin_hue * boundary
            if 0.0 < chroma < max_chroma:
                max_chroma = chroma

    u_value = cos_hue * max_chroma
    v_value = sin_hue * max_chroma
    y_value = ((lightness + 16.0) / 116.0) ** 3
    var_u = u_value / (13.0 * lightness) + 0.19784
    var_v = v_value / (13.0 * lightness) + 0.46834
    x_value = -(9.0 * y_value * var_u)
    x_value /= (var_u - 4.0) * var_v - var_u * var_v
    z_value = (9.0 * y_value - 15.0 * var_v * y_value - var_v * x_value)
    z_value /= 3.0 * var_v

    def from_linear(component):
        if component <= 0.0031308:
            return 12.92 * component
        return 1.055 * component ** (1.0 / 2.4) - 0.055

    xyz = (x_value, y_value, z_value)
    rgb = [from_linear(sum(row[i] * xyz[i] for i in range(3)))
           for row in RGB_MATRIX]
    return tuple(max(0, min(255, int(component * 255.0))) for component in rgb)


class TerminalUI:
    """Render streaming logs with a persistent rosmon-style status bar."""

    RESET = '\x1b[0m'
    # Exact true-color styles from rosmon's UI. Its packed 0xBBGGRR values
    # 0x404000, 0x606000, and 0xC8C8C8 become the RGB values below.
    BAR = '\x1b[48;2;0;64;64m\x1b[38;2;255;255;255m'
    BAR_KEY = '\x1b[48;2;0;96;96m\x1b[38;2;255;255;255m'
    RUNNING = '\x1b[38;2;0;0;0m\x1b[48;2;24;178;24m'
    CRASHED = '\x1b[38;2;0;0;0m\x1b[48;2;178;24;24m'
    PARTIAL = '\x1b[38;2;0;0;0m\x1b[48;2;200;200;0m'
    WAITING = '\x1b[38;2;0;0;0m\x1b[48;2;178;104;24m'
    IDLE = '\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m'
    NODE_SELECTED = '\x1b[38;2;0;0;0m\x1b[48;2;135;206;250m'
    SEARCH_SELECTED = '\x1b[38;2;0;0;0m\x1b[48;2;0;178;178m'
    KEY = '\x1b[38;2;0;0;0m\x1b[48;2;200;200;200m'
    MUTED_KEY = '\x1b[38;2;255;255;255m\x1b[48;2;165;0;0m'
    STATE_STYLES = {
        State.RUNNING: RUNNING,
        State.CRASHED: CRASHED,
        State.WAITING: WAITING,
        State.IDLE: IDLE,
    }

    def __init__(self, enabled: bool, on_key: Callable[[str], None],
                 output_enabled: bool = True):
        self.enabled = bool(enabled and sys.stdin.isatty() and sys.stdout.isatty())
        self.output_enabled = output_enabled
        self.on_key = on_key
        # Label colours and the label column width depend only on the record
        # list but are needed for every output line, so they are cached and
        # rebuilt whenever the records change rather than recomputed per line.
        self._label_colors = {}
        self._label_width = 8
        self._records: Iterable[ProcessRecord] = []
        self.selected: Optional[int] = None
        self.namespace_mode = False
        self.namespace_inspect: Optional[str] = None
        self.search_active = False
        self.search_query = ''
        self.search_selected = 0
        self.warn_only = False
        self._saved_termios = None
        self._status_lines = 0
        self._buffer = ''
        self._loop = None
        self._escape_timer = None
        self._started = False

    @property
    def records(self) -> Iterable[ProcessRecord]:
        return self._records

    @records.setter
    def records(self, records: Iterable[ProcessRecord]) -> None:
        self._records = records
        self._rebuild_label_cache()

    def start(self, loop) -> None:
        """Enter raw input mode and register the keyboard reader."""
        if not self.enabled or self._started:
            return
        self._saved_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        # stdin/stdout/stderr commonly share one open file description on a
        # pseudo-terminal.  Making stdin nonblocking therefore also makes
        # launch's output writes nonblocking, which can fail with EAGAIN and
        # shut down the complete LaunchService.  add_reader() only invokes
        # _read_input when data is ready, so blocking terminal I/O is safe.
        os.set_blocking(sys.stdout.fileno(), True)
        self._loop = loop
        loop.add_reader(sys.stdin.fileno(), self._read_input)
        sys.stdout.write('\x1b[?25l')
        sys.stdout.flush()
        self._started = True

    def close(self, loop=None) -> None:
        """Restore the user's terminal even when launch was interrupted."""
        if not self._started:
            return
        if loop is not None:
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None
        self._erase_status()
        # Restoring the terminal must not raise: run() calls close() from a
        # finally block, where an exception would mask the real failure and
        # skip the remaining cleanup.
        try:
            sys.stdout.write(self.RESET + '\x1b[?25h')
            sys.stdout.flush()
        except OSError:
            pass
        if self._saved_termios is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
            except termios.error:
                pass
        self._started = False

    def set_records(self, records: Iterable[ProcessRecord]) -> None:
        self.records = records
        self.redraw()

    def _rebuild_label_cache(self) -> None:
        """Recompute the per-process label colour and label column width."""
        records = list(self.records)
        total = max(1, len(records))
        self._label_colors = {
            record.display_name: _hsluv_label_color(index * 360.0 / total)
            for index, record in enumerate(records)
        }
        self._label_width = max(
            [len(record.display_name) for record in records] + [8])

    @staticmethod
    def namespace_for(record: ProcessRecord) -> str:
        """Return the top-level namespace containing a process."""
        return namespace_of(record.display_name)

    def namespaces(self):
        """Return stable namespace groups represented by the current records."""
        values = {self.namespace_for(record) for record in self.records}
        return sorted(values, key=lambda value: (value != '/', value))

    def records_in_namespace(self, namespace: str):
        """Return every process recursively grouped under a top-level namespace."""
        return [record for record in self.records
                if self.namespace_for(record) == namespace]

    def visible_records(self):
        """Return nodes visible in normal or namespace inspection mode."""
        if self.namespace_mode and self.namespace_inspect is not None:
            return self.records_in_namespace(self.namespace_inspect)
        return list(self.records)

    def search_matches(self):
        """Return visible nodes whose full names contain the search query."""
        return [record for record in self.visible_records()
                if self.search_query in record.display_name]

    @staticmethod
    def namespace_counts(records):
        """Return running and non-running process counts for a namespace."""
        alive = sum(record.state is State.RUNNING for record in records)
        return alive, len(records) - alive

    @classmethod
    def namespace_style(cls, records):
        """Color a namespace green, yellow, or red from its live/dead counts."""
        alive, dead = cls.namespace_counts(records)
        if dead == 0:
            return cls.RUNNING
        if alive == 0:
            return cls.CRASHED
        return cls.PARTIAL

    @classmethod
    def state_style(cls, state: State):
        """Return the status color for one process state."""
        return cls.STATE_STYLES[state]

    def log(self, source: str, text: str, is_stderr: bool = False,
            severity: Optional[str] = None) -> None:
        """Print one or more process output lines above the status bar."""
        clean = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = clean.splitlines()
        self.log_lines(source, lines,
                       [self._severity(line, severity, is_stderr) for line in lines])

    def log_lines(self, source: str, lines, severities) -> None:
        """Print already split and classified output above the status bar.

        The supervisor splits process output and resolves severities once and
        feeds the result to the log file, the event stream, and the terminal,
        so none of that work is repeated per consumer.
        """
        if not self.output_enabled:
            return
        self._erase_status()
        width = max(self._label_width, len(source))
        label = f'{source:>{width}}:'
        if self.enabled:
            background = self._label_color(source)
            if background is None:
                label = '\x1b[38;2;178;178;178m' + label + self.RESET
            else:
                red, green, blue = background
                label = (
                    f'\x1b[48;2;{red};{green};{blue}m'
                    '\x1b[38;2;255;255;255m' + label + self.RESET
                )
        write = sys.stdout.write
        for line, line_severity in zip(lines, severities):
            if self.warn_only and line_severity not in ('WARNING', 'ERROR', 'FATAL'):
                continue
            if self.enabled:
                style = SEVERITY_STYLES.get(line_severity)
                if style:
                    line = style + line + self.RESET
            write(f'{label} {line}\n')
        sys.stdout.flush()
        self.redraw()

    def notice(self, text: str, error: bool = False) -> None:
        self.log('rosmon2', text, severity='ERROR' if error else 'INFO')

    @staticmethod
    def _severity(line: str, explicit: Optional[str], is_stderr: bool) -> str:
        """Determine severity without assuming all ROS stderr output is an error."""
        # Stripping ANSI codes allocates a copy of every line, so only pay for
        # it when the line actually carries an escape sequence.
        match = SEVERITY_RE.search(ANSI_RE.sub('', line) if '\x1b' in line else line)
        value = match.group(1) if match else explicit
        if value == 'WARN':
            value = 'WARNING'
        if value:
            return value.upper()
        return 'ERROR' if is_stderr else 'INFO'

    def _label_color(self, source: str):
        """Return a stable foreground/background pair for a process label."""
        # Framework messages are plain gray, as rosmon's own messages are.
        return self._label_colors.get(source)

    def redraw(self) -> None:
        if not self.enabled or not self._started:
            return
        self._erase_status()
        columns = max(40, shutil.get_terminal_size((100, 24)).columns)
        sep = '\x1b[38;2;0;64;64m' + ('▂' * columns) + self.RESET
        showing_namespaces = self.namespace_mode and self.namespace_inspect is None
        if self.search_active:
            menu = self.BAR + f' Searching for: {self.search_query}'
        elif self.selected is None:
            menu = self._menu_item(
                'A-Z', 'Namespace actions' if showing_namespaces else 'Node actions')
            menu += self._menu_item(
                'F5', 'Node mode' if self.namespace_mode else 'Namespace mode')
            if self.namespace_mode and self.namespace_inspect is not None:
                menu += self._menu_item('Backspace', 'Namespaces')
            menu += self._menu_item('F6', 'Start all')
            menu += self._menu_item('F7', 'Stop all')
            menu += self._menu_item('F8', 'Toggle WARN+ only')
            menu += self._menu_item('F9', 'Mute all')
            menu += self._menu_item('F10', 'Unmute all')
            menu += self._menu_item('/', 'Node search')
            if self.warn_only:
                menu += ' \x1b[30;45m ! WARN+ output only ! ' + self.RESET
            if any(r.muted for r in self.records):
                menu += ' \x1b[30;43m ! Caution: Nodes muted ! ' + self.RESET
        else:
            if showing_namespaces:
                namespaces = self.namespaces()
                if self.selected >= len(namespaces):
                    self.selected = None
                    return self.redraw()
                namespace = namespaces[self.selected]
                count = len(self.records_in_namespace(namespace))
                menu = self.BAR + f" Namespace '{namespace}' has {count} node(s). Actions:"
                menu += self._menu_item('s', 'start all')
                menu += self._menu_item('k', 'stop all')
                menu += self._menu_item('i', 'inspect')
                menu += self._menu_item('m', 'mute namespace')
                menu += self._menu_item('u', 'unmute namespace')
            else:
                records = self.visible_records()
                if self.selected >= len(records):
                    self.selected = None
                    return self.redraw()
                record = records[self.selected]
                menu = self.BAR + (
                    f" Node '{record.display_name}' is {record.state.value}. Actions:")
                menu += self._menu_item('s', 'start')
                menu += self._menu_item('k', 'stop')
                menu += self._menu_item('d', 'debug')
                menu += self._menu_item('u' if record.muted else 'm',
                                        'unmute' if record.muted else 'mute')
        menu = self._fit(menu, columns)

        if self.search_active:
            entries = [(record.display_name, self.state_style(record.state), record.muted)
                       for record in self.search_matches()]
        elif showing_namespaces:
            entries = []
            for namespace in self.namespaces():
                members = self.records_in_namespace(namespace)
                alive, dead = self.namespace_counts(members)
                entries.append((
                    f'{namespace} [{alive}:{dead}]',
                    self.namespace_style(members),
                    bool(members) and all(record.muted for record in members),
                ))
        else:
            entries = [(record.display_name, self.state_style(record.state), record.muted)
                       for record in self.visible_records()]

        blocks = []
        line = ''
        # Track the visible width as blocks are appended.  Re-measuring the
        # accumulated line with _visible_len() made every redraw quadratic in
        # the number of processes, and a redraw happens for every output line.
        line_width = 0
        for index, (display_name, state_style, muted) in enumerate(entries):
            if self.search_active:
                name = display_name.lstrip('/')
                max_name_length = max(1, columns - 2)
                if len(name) > max_name_length:
                    name = name[:max_name_length - 1] + '…'
                label = f' {name} '
                style = self.SEARCH_SELECTED if self.search_selected == index else ''
                block = style + label + self.RESET
                plain_len = len(label)
                if line_width + plain_len + 1 > columns and line:
                    blocks.append(line)
                    line, line_width = block, plain_len
                else:
                    line += (' ' if line else '') + block
                    line_width += plain_len + (1 if line_width else 0)
                continue

            key = selection_key(index)
            key_text = key if key is not None else ' '
            key_style = self.MUTED_KEY if muted else self.KEY
            name = display_name if showing_namespaces else display_name.lstrip('/')
            # Keep the complete process name whenever it fits.  The status
            # area already wraps blocks onto additional rows, so shortening
            # every name to 13 characters only hides useful node identity.
            max_name_length = max(1, columns - 3)
            if len(name) > max_name_length:
                name = name[:max_name_length - 1] + '…'
            selected = self.selected == index
            label = f'[{name}]' if selected and not showing_namespaces else f' {name} '
            label_style = (
                self.NODE_SELECTED
                if selected and not showing_namespaces else state_style
            )
            block = key_style + key_text + label_style + label + self.RESET
            plain_len = 1 + len(label)
            if line_width + plain_len + 1 > columns and line:
                blocks.append(line)
                line, line_width = block, plain_len
            else:
                line += (' ' if line else '') + block
                line_width += plain_len + (1 if line_width else 0)
        if line:
            blocks.append(line)
        if not blocks:
            message = ' no matching nodes ' if self.search_active else ' waiting for processes '
            blocks = [self.IDLE + message + self.RESET]

        lines = [sep, menu] + blocks
        sys.stdout.write('\n'.join(lines) + '\n')
        sys.stdout.write(f'\x1b[{len(lines)}A\r')
        sys.stdout.flush()
        self._status_lines = len(lines)

    def _menu_item(self, key: str, label: str) -> str:
        return f'{self.BAR_KEY} {key}:{self.BAR} {label} {self.RESET}'

    def _erase_status(self) -> None:
        if self.enabled and self._status_lines:
            sys.stdout.write('\r\x1b[J')
            self._status_lines = 0

    def _read_input(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 64).decode(errors='ignore')
        except (BlockingIOError, OSError):
            return
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None
        self._buffer += data
        while self._buffer:
            matched = False
            for sequence, name in KEY_SEQUENCES.items():
                if self._buffer.startswith(sequence):
                    self._buffer = self._buffer[len(sequence):]
                    self.on_key(name)
                    matched = True
                    break
            if matched:
                continue
            if self._buffer == '\x1b':
                if self._loop is None:
                    self._flush_escape()
                    continue
                self._escape_timer = self._loop.call_later(0.03, self._flush_escape)
                break
            # A read can end part-way through a function key.  Wait for the
            # rest instead of emitting the fragment as individual keystrokes;
            # F5-F10 are five bytes, so a fixed length check is not enough.
            if any(sequence.startswith(self._buffer) for sequence in KEY_SEQUENCES):
                break
            char, self._buffer = self._buffer[0], self._buffer[1:]
            self.on_key(char)

    def _flush_escape(self) -> None:
        """Emit a standalone Escape after allowing time for an arrow sequence."""
        self._escape_timer = None
        if self._buffer == '\x1b':
            self._buffer = ''
            self.on_key('ESC')

    @staticmethod
    def _visible_len(text: str) -> int:
        return len(ANSI_RE.sub('', text))

    @staticmethod
    def _truncate_ansi(text: str, columns: int) -> str:
        """Truncate visible text while retaining embedded ANSI styles."""
        output = []
        position = 0
        visible = 0
        for match in ANSI_RE.finditer(text):
            chunk = text[position:match.start()]
            remaining = columns - visible
            if remaining <= 0:
                break
            output.append(chunk[:remaining])
            visible += min(len(chunk), remaining)
            if len(chunk) > remaining:
                break
            output.append(match.group())
            position = match.end()
        else:
            remaining = columns - visible
            if remaining > 0:
                output.append(text[position:position + remaining])
        return ''.join(output)

    def _fit(self, text: str, columns: int) -> str:
        plain = ANSI_RE.sub('', text)
        if len(plain) <= columns:
            return text + self.BAR + (' ' * (columns - len(plain))) + self.RESET
        # The status remains useful on narrow terminals even without every action.
        return self._truncate_ansi(text, columns) + self.RESET
