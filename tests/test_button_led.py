"""Original host-only checks: GPIO, clock and radio are fakes throughout."""
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import quick_mode as q
from test_radio import Clock, Exchange


class FakeGPIO:
    def __init__(self):
        self.connected = True
        self.sl = SimpleNamespace(s=SimpleNamespace(settimeout=lambda n: None))
        self.actions = []
        self.levels = iter([1] * 10 + [0] * 2 + [1] * 2 + [0] * 10 + [1] * 10)

    def write(self, pin, value):
        self.actions.append(('write', pin, value))
        return 0

    def set_mode(self, pin, value):
        self.actions.append(('mode', pin, value))
        return 0

    def set_pull_up_down(self, pin, value):
        self.actions.append(('pull', pin, value))
        return 0

    def read(self, pin):
        self.actions.append(('read', pin))
        return next(self.levels)

    def stop(self):
        self.actions.append(('stop',))


class ButtonLedTests(unittest.TestCase):
    def opened(self, pi):
        module = SimpleNamespace(pi=lambda *a, **k: pi, OUTPUT=1, INPUT=0, PUD_UP=2)
        return patch.object(q.importlib, 'import_module', return_value=module)

    def test_gpio_lazy_import_no_hardware(self):
        code = '''
import sys
class Deny:
    def find_spec(self, name, *args):
        if name in ('pigpio', 'nrf24', 'gpiozero', 'rgb_test'):
            raise RuntimeError('forbidden import: ' + name)
sys.meta_path.insert(0, Deny())
import quick_mode
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_latches_before_modes_cleanup_and_common_ground_colors(self):
        pi = FakeGPIO()
        with self.opened(pi), patch.object(q.metadata, 'version', return_value='1.78'):
            with q.ButtonLed() as ui:
                for pin in (17, 27, 22):
                    self.assertLess(pi.actions.index(('write', pin, 0)),
                                    pi.actions.index(('mode', pin, 1)))
                ui.color((0, 0, 1))
                self.assertEqual(pi.actions[-4:], [('write',17,0), ('write',27,0),
                                                  ('write',22,0), ('write',22,1)])
            self.assertEqual(pi.actions[-4:], [('write',17,0), ('write',27,0),
                                              ('write',22,0), ('stop',)])
            self.assertIn(('pull',23,2), pi.actions)

    def test_startup_failure_cleans_partial_outputs(self):
        pi = FakeGPIO()
        pi.set_mode = lambda *args: -1
        with self.opened(pi), patch.object(q.metadata, 'version', return_value='1.78'):
            with self.assertRaises(OSError):
                with q.ButtonLed():
                    self.fail('must not enter')
        self.assertEqual(pi.actions[-2:], [('write',17,0), ('stop',)])

    def test_version_and_connection_fail_closed(self):
        with patch.object(q.metadata, 'version', return_value='wrong'), \
                patch.object(q.importlib, 'import_module') as importing:
            with self.assertRaises(RuntimeError):
                with q.ButtonLed():
                    pass
            importing.assert_not_called()
        pi = FakeGPIO()
        pi.connected = False
        with self.opened(pi), patch.object(q.metadata, 'version', return_value='1.78'):
            with self.assertRaises(OSError):
                with q.ButtonLed():
                    pass
        self.assertEqual(pi.actions, [('stop',)])

    def test_poll_debounce_bounce_held_start_and_no_callback(self):
        pi = FakeGPIO()
        # Held at boot, released, bouncing press, stable press, stable release.
        pi.levels = iter([0] * 8 + [1] * 10 + [0,1,0,1] + [0] * 10 + [1] * 10)
        clock = Clock()
        with self.opened(pi), patch.object(q.metadata, 'version', return_value='1.78'), \
                patch.object(q.time, 'monotonic', clock.now), \
                patch.object(q.time, 'sleep', side_effect=clock.sleep), \
                redirect_stderr(io.StringIO()):
            with q.ButtonLed() as ui:
                ui.wait()
        self.assertGreater(clock.value, 0.30)
        self.assertGreater(len([a for a in pi.actions if a[0] == 'read']), 30)

    def test_invalid_path_rx_and_missing_optin_before_gpio(self):
        with patch.object(q.ButtonLed, '__enter__') as opening, \
                redirect_stderr(io.StringIO()):
            args = ['tx','/not-present/file.txt','--rate','250k',
                    '--hardware-checked','--button-led']
            self.assertEqual(q.main(args), 1)
            with self.assertRaises(SystemExit):
                q.main(['rx','new.txt','--rate','250k','--hardware-checked','--button-led'])
            with self.assertRaises(SystemExit):
                q.main(['tx','data.txt','--rate','250k','--button-led'])
            opening.assert_not_called()

    def test_cli_waits_then_single_tx_receipt_success_or_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'in.txt'
                path.write_bytes(b'hello')
                clock = Clock()
                order = []
                ui = unittest.mock.MagicMock()
                ui.__enter__.return_value = ui
                ui.wait.side_effect = lambda: order.append('wait')
                radio = unittest.mock.MagicMock()
                radio.__enter__.return_value = Exchange(clock)
                def open_radio(*args):
                    self.assertEqual(order, ['wait'])
                    order.append('radio')
                    if fail:
                        raise OSError('unconfirmed')
                    return radio
                with patch.object(q, 'ButtonLed', return_value=ui), \
                        patch('radio_adapter.open_radio', side_effect=open_radio) as opening, \
                        patch.object(q.time, 'monotonic', clock.now), \
                        redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                    status = q.main(['tx',str(path),'--rate','250k',
                                     '--hardware-checked','--button-led'])
                self.assertEqual(status, int(fail))
                ui.wait.assert_called_once()
                opening.assert_called_once()
                ui.result.assert_called_once_with(not fail)
                ui.__exit__.assert_called_once()
                self.assertEqual('remote_saved_receipt' in stdout.getvalue(), not fail)

    def test_cancel_wait_does_not_open_radio_and_closes_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'in.txt'
            path.write_bytes(b'a')
            ui = unittest.mock.MagicMock()
            ui.__enter__.return_value = ui
            ui.wait.side_effect = KeyboardInterrupt()
            with patch.object(q, 'ButtonLed', return_value=ui), \
                    patch('radio_adapter.open_radio') as opening, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(q.main(['tx',str(path),'--rate','250k',
                                         '--hardware-checked','--button-led']),130)
            opening.assert_not_called()
            ui.result.assert_not_called()
            ui.__exit__.assert_called_once()

    def test_broken_socket_cleanup_disconnects_without_more_writes(self):
        pi = FakeGPIO()
        with self.opened(pi), patch.object(q.metadata, 'version', return_value='1.78'), \
                redirect_stderr(io.StringIO()) as stderr:
            with q.ButtonLed() as ui:
                ui.io.broken = True
                count = len(pi.actions)
            self.assertEqual(pi.actions[count:], [('stop',)])
            self.assertIn('cannot be verified', stderr.getvalue())

    def test_result_indicator_error_does_not_change_transfer_outcome(self):
        ui = q.ButtonLed()
        with patch.object(ui, 'color', side_effect=OSError('LED fault')), \
                redirect_stderr(io.StringIO()) as stderr:
            ui.result(True)
        self.assertIn('LED indication unavailable', stderr.getvalue())

    def test_colors_green_only_saved_and_red_unconfirmed(self):
        ui = q.ButtonLed()
        with patch.object(ui, 'color') as color, patch.object(q.time, 'sleep') as sleep:
            ui.result(True)
            color.assert_called_with((0,1,0))
            ui.result(False)
            color.assert_called_with((1,0,0))
            self.assertEqual(sleep.call_args_list, [unittest.mock.call(2)] * 2)

    def test_originals_still_off_without_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'in.txt'
            path.write_bytes(b'dry')
            with patch.object(q, 'ButtonLed') as gpio, redirect_stdout(io.StringIO()):
                self.assertEqual(q.main([str(path),'--dry-run']),0)
                gpio.assert_not_called()

    def test_debug_logs_observed_retry_progress_and_saved_without_content(self):
        import transfer as t
        clock = Clock()
        # Force a real first START retry with an observed lost reply.
        radio = Exchange(clock, drop_replies=((0, 1),))
        with redirect_stderr(io.StringIO()) as stderr:
            count = t.send_v1(b'PRIVATE_TEXT', 9, '250k',
                              q.LoggedTxRadio(radio, 12, True), clock.now)
        log = stderr.getvalue()
        self.assertEqual(count,3)
        self.assertIn('attempt=2',log)
        self.assertIn('peer accepted data',log)
        self.assertIn('NOT saved yet',log)
        self.assertIn('matching remote SAVED',log)
        self.assertNotIn('PRIVATE_TEXT',log)

    def test_no_saved_log_for_unrelated_receipt(self):
        import transfer as t
        radio = unittest.mock.MagicMock()
        observed = q.LoggedTxRadio(radio,0,True)
        request = next(t.v1_frames(b'',7,'250k'))
        radio.receive.return_value = t.packet(t.SAVED,8,1)
        with redirect_stderr(io.StringIO()) as stderr:
            observed.send(request)
            observed.receive(1)
        self.assertNotIn('matching remote SAVED',stderr.getvalue())
        self.assertIn('unrelated reply',stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
