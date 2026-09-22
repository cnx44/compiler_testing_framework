"""Optional real-terminal checks: python test/tui_smoke.py [--campaign].

Requires pexpect in the invoking Python. The application uses the project venv.
--campaign also needs the configured ASPIS toolchain and permission to use GDB.
All generated files are isolated in a temporary directory.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

import pexpect


REPO = Path(__file__).resolve().parents[1]
WRAPPER = '''
from pathlib import Path
from src.front_end import AspisTui, main
original = AspisTui._key
def capture(self):
    self.screen.refresh()
    Path('screen.txt').write_text('\\n'.join(self.screen.instr(y, 0).decode('utf-8', 'replace') for y in range(self._height())))
    return original(self)
AspisTui._key = capture
main()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', action='store_true')
    options = parser.parse_args()
    env = dict(os.environ, TERM='xterm-256color', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(REPO))
    with tempfile.TemporaryDirectory(prefix='aspis-tui-smoke-') as directory:
        root = Path(directory)
        (root / 'main.c').write_text('#include <stdio.h>\nint G;\nint main(int argc, char **argv) {\n'
                                    ' if (argc < 2) return 2;\n G = argc;\n printf("%d\\n", G);\n return 0;\n}\n')
        (root / 'main.values').write_text('args[1] = sample\n')
        (root / 'campaign.cfg').write_text('name: smoke\nprograms: [main.c]\n'
            'data_protection: none\ncontrol_flow_protection: none\nexecution_repetitions: 1\n'
            'measure_overhead: false\nmeasurement_repetitions: 3\ninjection: global\nfunctions: [main(@G)]\n')
        (root / 'invalid.json').write_text('{"hello":"world"}')
        (root / 'results.json').write_text(json.dumps([{'name': 'sample', 'injections': {
            'latent_states': [{'variables': {f'v{i:02}': i for i in range(40)}}]}}]))
        children = []

        def screen():
            path = root / 'screen.txt'
            return path.read_text() if path.exists() else ''

        def wait(text, timeout=6):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if text in screen():
                    return screen()
                # Drain terminal output so a long redraw does not fill the PTY.
                for child in children:
                    try:
                        child.read_nonblocking(65536, timeout=0)
                    except (pexpect.TIMEOUT, pexpect.EOF):
                        pass
                time.sleep(.03)
            raise AssertionError(f'Missing {text!r}: {screen()}')

        def launch():
            (root / 'screen.txt').unlink(missing_ok=True)
            child = pexpect.spawn(str(REPO / '.venv/bin/python'), ['-c', WRAPPER], cwd=directory,
                                  env=env, dimensions=(24, 80), encoding='utf-8', timeout=6)
            children.append(child)
            wait('Campaign console')
            return child

        def send(keys, text, timeout=6):
            child.send(keys)
            return wait(text, timeout)

        def stop():
            child.send(':q')
            child.expect(pexpect.EOF)
            child.close()
            assert child.exitstatus == 0
            children.remove(child)

        try:
            child = launch()
            send('jj\r', 'campaign.cfg')
            send('j\r', 'Edit · campaign.cfg')
            rows = send('j' * 12, '› Delete test').splitlines()
            assert next(i for i, row in enumerate(rows) if '› Delete test' in row) < 21
            child.setwinsize(18, 60)
            time.sleep(.2)
            rows = send('k', '› Validate and save').splitlines()
            assert next(i for i, row in enumerate(rows) if '› Validate and save' in row) < 15
            send('\r', 'Configuration updated')
            saved = (root / 'campaign.cfg').read_text()
            for field in ('main(@G)', 'injection: global', 'measure_overhead: false', 'measurement_repetitions: 3'):
                assert field in saved
            stop()
            print('PASS: save preserves globals and overhead settings; actions visible at 80x24 and 60x18', flush=True)

            child = launch()
            send('jjjj\r', 'invalid.json')
            send('j\r', 'Campaign results must be a list')
            send('\r', 'Campaign console')
            stop()
            print('PASS: invalid result JSON is recoverable', flush=True)

            child = launch()
            send('jjj\r', 'Project settings')
            child.send('\r')
            child.expect('Ctrl-U clear')
            child.send('\x1b')
            child.expect('Project settings')
            child.send('\r')
            child.expect('Ctrl-U clear')
            stop()
            print('PASS: Esc cancels text input and :q exits', flush=True)

            child = launch()
            send('j\r', 'Source selection')
            send('\r', 'main.c')
            send('j\r', 'New configuration')
            send('j' * 11 + '\r', 'Choose where to save')
            child.send('s')
            child.expect('Ctrl-U clear')
            send('\r', 'Replace existing configuration')
            send('\r', 'Campaign console')
            assert (root / 'campaign.cfg').read_text() == saved
            stop()
            print('PASS: overwrite confirmation defaults to Cancel', flush=True)

            child = launch()
            send('jj\r', 'campaign.cfg')
            send('j\r', 'Edit · campaign.cfg')
            send('k\r', 'Cannot delete last test')
            assert (root / 'campaign.cfg').read_text() == saved
            stop()
            print('PASS: final-test deletion preserves the configuration', flush=True)

            child = launch()
            send('jjjj\r', 'results.json')
            send('jj\r', 'Campaign summary')
            child.setwinsize(18, 60)
            send('t', 'LATENT Inspector')
            send('j' * 80, 'v39')
            stop()
            print('PASS: long latent differences scroll to the last variable', flush=True)

            if options.campaign:
                for overhead in (False, True):
                    config = saved.replace('measure_overhead: false', f'measure_overhead: {str(overhead).lower()}')
                    (root / 'campaign.cfg').write_text(config)
                    child = launch()
                    send('\r', 'campaign.cfg')
                    send('j\r', 'Campaign summary', timeout=55)
                    log = (root / 'campaign.log').read_text()
                    assert 'ERROR:' not in log, log
                    summary = max(root.glob('campaign_summary_*.json'), key=lambda path: path.stat().st_mtime_ns)
                    result = json.loads(summary.read_text())[0]
                    assert result['injections']['total'] == 1
                    assert result['oracle']['measurement_repetitions'] == (3 if overhead else 1)
                    assert bool(result['difference']) == overhead
                    stop()
                    print(f'PASS: actual global-injection campaign, arguments, measure_overhead={overhead}', flush=True)
        finally:
            for child in children:
                child.terminate(force=True)


if __name__ == '__main__':
    main()
