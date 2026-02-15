# gui.py (with explicit "Manual outputs" group for per-channel control)
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import sys
import struct
import time
import threading
from typing import Optional, List, Dict, Any
import json
import os
import tempfile

from PyQt5.QtCore import (
    Qt, QObject, QThread, pyqtSignal, QTimer, QSettings, QSize
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QComboBox, QLabel, QAction, QToolBar, QGroupBox, QCheckBox,
    QTextEdit, QDoubleSpinBox, QLineEdit, QMessageBox, QScrollArea, QFrame,
    QSizePolicy,
)
from PyQt5.QtGui import QFontDatabase, QIcon, QPixmap, QPainter, QColor, QFont, QIcon

import serial
import serial.tools.list_ports

from . import transcode

def make_serial_exclusive(**kwargs):
    """Create a pyserial Serial() with best-effort OS-level exclusivity (POSIX).
    Falls back gracefully on platforms/pyserial versions that don't support it."""
    try:
        return serial.Serial(exclusive=True, **kwargs)
    except TypeError:
        s = serial.Serial(**kwargs)
        # Some pyserial builds expose .exclusive attribute (POSIX only).
        try:
            s.exclusive = True
        except Exception:
            pass
        return s



def create_app_icon() -> QIcon:
    """Create a simple in-code app icon so the dock/taskbar icon isn't blank.

    This avoids shipping an external .icns/.ico file and works on Windows/macOS/Linux.
    """
    size = 256
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    # Blue circle
    p.setBrush(QColor(0, 122, 255))  # macOS-ish accent blue
    p.setPen(Qt.NoPen)
    margin = int(size * 0.08)
    p.drawEllipse(margin, margin, size - 2*margin, size - 2*margin)

    # White "N"
    font = QFont()
    font.setBold(True)
    font.setPointSize(int(size * 0.55))
    p.setFont(font)
    p.setPen(QColor(255, 255, 255))
    p.drawText(pm.rect(), Qt.AlignCenter, "N")

    p.end()
    return QIcon(pm)


# -----------------------
# Cross-process device settings lock
# -----------------------
# We use a per-device lock file so multiple GUI instances can coordinate operations
# like deleting a device's saved labels/groups. This is advisory and only effective
# between ndpulsegen GUI instances (which is exactly what we need here).

class InterProcessLock:
    """Simple cross-platform inter-process file lock.

    - macOS/Linux: fcntl.flock
    - Windows: msvcrt.locking

    This is an *advisory* lock, intended to coordinate between our own GUI instances.
    """

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        # Open (and keep open) for duration of lock.
        self._fh = open(self.lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    # Lock 1 byte from start
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), mode, 1)
                except OSError:
                    return False
            else:
                import fcntl
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(self._fh.fileno(), flags)
                except OSError:
                    return False
            return True
        except Exception:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self):
        ok = self.acquire(blocking=True)
        if not ok:
            raise RuntimeError("Unable to acquire lock")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def device_settings_lock_path(serial_number: int) -> str:
    base = os.path.join(tempfile.gettempdir(), "ndpulsegen_locks")
    return os.path.join(base, f"device_{int(serial_number)}.lock")


class SerialWorker(QObject):
    messageReceived = pyqtSignal(dict)
    devicestate = pyqtSignal(dict)
    powerlinestate = pyqtSignal(dict)
    devicestate_extras = pyqtSignal(dict)
    notification = pyqtSignal(dict)
    echo = pyqtSignal(dict)
    easyprint = pyqtSignal(dict)
    internalError = pyqtSignal(dict)
    bytesDropped = pyqtSignal(int, float)
    errorOccurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, ser: serial.Serial, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ser = ser
        self._running = True

    def stop(self):
        self._running = False
        try:
            self.ser.cancel_read()
        except Exception:
            pass

    def run(self):
        try:
            while self._running:
                try:
                    b = self.ser.read(1)
                except serial.serialutil.SerialException as ex:
                    self.errorOccurred.emit(str(ex))
                    break
                if not b:
                    continue
                ts = time.time()
                msg_id = b[0]
                dinfo = transcode.msgin_decodeinfo.get(msg_id)
                if not dinfo:
                    self.bytesDropped.emit(msg_id, ts)
                    continue
                remaining = dinfo["message_length"] - 1
                try:
                    payload = self.ser.read(remaining)
                except serial.serialutil.SerialException as ex:
                    self.errorOccurred.emit(str(ex))
                    break
                if len(payload) != remaining:
                    self.bytesDropped.emit(msg_id, ts)
                    continue
                try:
                    decoded = dinfo["decode_function"](payload)
                except Exception as ex:
                    self.errorOccurred.emit(f"Decode failed for id {msg_id}: {ex}")
                    continue
                decoded["timestamp"] = ts
                decoded["message_type"] = dinfo["message_type"]
                self.messageReceived.emit(decoded)
                mtype = dinfo["message_type"]
                if mtype == "devicestate":
                    self.devicestate.emit(decoded)
                elif mtype == "powerlinestate":
                    self.powerlinestate.emit(decoded)
                elif mtype == "devicestate_extras":
                    self.devicestate_extras.emit(decoded)
                elif mtype == "notification":
                    self.notification.emit(decoded)
                elif mtype == "echo":
                    self.echo.emit(decoded)
                elif mtype == "print":
                    self.easyprint.emit(decoded)
                elif mtype == 'error':
                    self.internalError.emit(decoded)
        finally:
            self.finished.emit()


class PulseGenerator(QObject):
    devicestate = pyqtSignal(dict)
    powerlinestate = pyqtSignal(dict)
    devicestate_extras = pyqtSignal(dict)
    notification = pyqtSignal(dict)
    echo = pyqtSignal(dict)
    easyprint = pyqtSignal(dict)
    internalError = pyqtSignal(dict)
    bytesDropped = pyqtSignal(int, float)
    errorOccurred = pyqtSignal(str)
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ser = make_serial_exclusive()
        self.ser.timeout = 0.1
        self.ser.write_timeout = 1
        self.ser.baudrate = 12000000

        self._write_lock = threading.Lock()
        self._thread: Optional[QThread] = None
        self._worker: Optional[SerialWorker] = None

        self._disconnecting_due_to_error = False

        self._valid_vid = 1027
        self._valid_pid = 24592

        self.serial_number_save: Optional[int] = None
        self.device_type: Optional[int] = None
        self.firmware_version: Optional[str] = None
        self.hardware_version: Optional[str] = None
        self._device_settings_lock: Optional[InterProcessLock] = None

    def is_open(self) -> bool:
        return bool(self.ser and self.ser.is_open)


    def _handle_serial_error(self, message: str) -> None:
        """Handle a serial-layer failure (USB unplug, port reset, etc).

        Ensures we transition to the disconnected state exactly once.
        """
        # Avoid re-entrancy / multiple disconnect cascades
        if self._disconnecting_due_to_error:
            return
        self._disconnecting_due_to_error = True

        # Surface the error to the UI
        try:
            self.errorOccurred.emit(message)
        except Exception:
            pass

        # Defer the actual disconnect onto the Qt event loop to avoid threading issues
        try:
            QTimer.singleShot(0, self.disconnect)
        except Exception:
            # If QTimer isn't available for some reason, fall back
            try:
                self.disconnect()
            except Exception:
                pass


    def _start_reader(self):
        self._thread = QThread()
        self._worker = SerialWorker(self.ser)
        self._worker.moveToThread(self._thread)
        self._worker.devicestate.connect(self.devicestate)
        self._worker.powerlinestate.connect(self.powerlinestate)
        self._worker.devicestate_extras.connect(self.devicestate_extras)
        self._worker.notification.connect(self.notification)
        self._worker.echo.connect(self.echo)
        self._worker.easyprint.connect(self.easyprint)
        self._worker.internalError.connect(self.internalError)
        self._worker.bytesDropped.connect(self.bytesDropped)
        self._worker.errorOccurred.connect(self._handle_serial_error)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.connected.emit(self.ser.port)

    def _stop_reader(self):
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(1500)
        self._worker = None
        self._thread = None

    def disconnect(self):
        try:
            self._stop_reader()
        finally:
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
            finally:
                # Release per-device settings lock (if held)
                try:
                    if self._device_settings_lock is not None:
                        self._device_settings_lock.release()
                except Exception:
                    pass
                self._device_settings_lock = None
                self._disconnecting_due_to_error = False
                self.disconnected.emit()

    def connect(self, serial_number: Optional[int] = None, port: Optional[str] = None) -> bool:
        if self.is_open():
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass
            return True

        target_port = None
        device_meta = None
        if port is None:
            devices = self.get_connected_devices()["validated_devices"]
            for d in devices:
                if (serial_number is not None and d.get("serial_number") == serial_number) or (
                    serial_number is None and port is None
                ):
                    target_port = d["comport"]
                    device_meta = d
                    break
        if port is not None and target_port is None:
            target_port = port
        if not target_port:
            return False

        self._disconnecting_due_to_error = False
        self.ser.port = target_port
        self.ser.open()
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # Populate device metadata immediately using an echo on the already-open connection
        ok_echo, meta = self._echo_on_open_serial(timeout_s=1.0)
        if ok_echo and meta:
            self.serial_number_save = meta.get('serial_number')
            self.device_type = meta.get('device_type')
            self.firmware_version = meta.get('firmware_version')
            self.hardware_version = meta.get('hardware_version')
            # Hold a per-device settings lock while connected so other GUI instances
            # can safely avoid deleting/editing settings for this device concurrently.
            try:
                sn_lock = int(self.serial_number_save) if self.serial_number_save is not None else None
            except Exception:
                sn_lock = None
            if sn_lock is not None:
                lk = InterProcessLock(device_settings_lock_path(sn_lock))
                if lk.acquire(blocking=False):
                    self._device_settings_lock = lk
                else:
                    # Someone else is using this device's settings (another GUI instance).
                    # Don't proceed.
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self._device_settings_lock = None
                    self.errorOccurred.emit('Device settings are in use by another instance.')
                    return False
            # Update UI immediately (MainWindow.on_echo)
            try:
                self.echo.emit({
                    'serial_number': self.serial_number_save,
                    'device_type': self.device_type,
                    'firmware_version': self.firmware_version,
                    'hardware_version': self.hardware_version,
                })
            except Exception:
                pass
        self._start_reader()
        # Note: serial_number_save is populated from the device echo above.

        if device_meta:
            if self.serial_number_save is None:
                self.serial_number_save = device_meta.get("serial_number")
            if self.device_type is None:
                self.device_type = device_meta.get("device_type")
            if self.firmware_version is None:
                self.firmware_version = device_meta.get("firmware_version")
            if self.hardware_version is None:
                self.hardware_version = device_meta.get("hardware_version")
        return True

    def get_connected_devices(self) -> Dict[str, Any]:
        validated_devices = []
        unvalidated = []
        comports = list(serial.tools.list_ports.comports())
        valid_ports = []
        for cp in comports:
            if getattr(cp, "vid", None) == self._valid_vid and getattr(cp, "pid", None) == self._valid_pid:
                if sys.platform == 'linux' and cp.location.endswith('.0'):
                        # ignore JTAG interface incorrectly exposed as a serial port
                        continue
                valid_ports.append(cp)
        for cp in valid_ports:
            ok, meta = self._try_handshake(cp.device)
            if ok and meta:
                meta["comport"] = cp.device
                validated_devices.append(meta)
            else:
                unvalidated.append(cp.device)
        return {"validated_devices": validated_devices, "unvalidated_devices": unvalidated}

    def _try_handshake(self, port: str, timeout_s: float = 1.0):
        s = make_serial_exclusive()
        s.port = port
        s.baudrate = self.ser.baudrate
        s.timeout = 0.2
        s.write_timeout = 0.5
        try:
            s.open()
        except Exception:
            return False, None
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
            check_byte = bytes([209])
            s.write(transcode.encode_echo(check_byte))
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                b = s.read(1)
                if not b:
                    continue
                msg_id = b[0]
                dinfo = transcode.msgin_decodeinfo.get(msg_id)
                if not dinfo:
                    continue
                remaining = dinfo["message_length"] - 1
                payload = s.read(remaining)
                if len(payload) != remaining:
                    continue
                decoded = dinfo["decode_function"](payload)
                if dinfo["message_type"] == "echo" and decoded.get("echoed_byte") == check_byte:
                    return True, {
                        "device_type": decoded.get("device_type"),
                        "hardware_version": decoded.get("hardware_version"),
                        "firmware_version": decoded.get("firmware_version"),
                        "serial_number": decoded.get("serial_number"),
                    }
            return False, None
        finally:
            try:
                s.close()
            except Exception:
                pass

    
    def _echo_on_open_serial(self, timeout_s: float = 1.0):
        """
        Send an echo request on the already-open self.ser and synchronously parse the response.
        Returns (ok, meta_dict) where meta_dict includes device_type/hardware_version/firmware_version/serial_number.
        Note: This runs *before* the reader thread starts, so it won't race with the worker.
        """
        if not self.is_open():
            return False, None
        try:
            # Clear any stale bytes
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass

            check_byte = bytes([209])
            self.ser.write(transcode.encode_echo(check_byte))
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                b = self.ser.read(1)
                if not b:
                    continue
                msg_id = b[0]
                dinfo = transcode.msgin_decodeinfo.get(msg_id)
                if not dinfo:
                    continue
                remaining = dinfo["message_length"] - 1
                payload = self.ser.read(remaining)
                if len(payload) != remaining:
                    continue
                decoded = dinfo["decode_function"](payload)
                if dinfo["message_type"] == "echo" and decoded.get("echoed_byte") == check_byte:
                    return True, {
                        "device_type": decoded.get("device_type"),
                        "hardware_version": decoded.get("hardware_version"),
                        "firmware_version": decoded.get("firmware_version"),
                        "serial_number": decoded.get("serial_number"),
                    }
            return False, None
        except Exception:
            return False, None

    def write_command(self, encoded_command: bytes):
        if not self.is_open():
            raise serial.serialutil.PortNotOpenError("Serial port is not open")
        with self._write_lock:
            try:
                self.ser.write(encoded_command)
            except (serial.serialutil.SerialException, OSError) as ex:
                self._handle_serial_error(str(ex))
                raise

    def write_echo(self, byte_to_echo: bytes):
        self.write_command(transcode.encode_echo(byte_to_echo))

    def write_device_options(
        self,
        final_address=None,
        run_mode=None,
        accept_hardware_trigger=None,
        trigger_out_length=None,
        trigger_out_delay=None,
        notify_on_main_trig_out=None,
        notify_when_run_finished=None,
        software_run_enable=None,
    ):
        self.write_command(
            transcode.encode_device_options(
                final_address,
                run_mode,
                accept_hardware_trigger,
                trigger_out_length,
                trigger_out_delay,
                notify_on_main_trig_out,
                notify_when_run_finished,
                software_run_enable,
            )
        )

    def write_powerline_trigger_options(self, trigger_on_powerline=None, powerline_trigger_delay=None):
        self.write_command(
            transcode.encode_powerline_trigger_options(trigger_on_powerline, powerline_trigger_delay)
        )

    def write_action(
        self,
        trigger_now=False,
        disable_after_current_run=False,
        disarm=False,
        request_state=False,
        request_powerline_state=False,
        request_state_extras=False,
    ):
        self.write_command(
            transcode.encode_action(
                trigger_now,
                disable_after_current_run,
                disarm,
                request_state,
                request_powerline_state,
                request_state_extras,
            )
        )

    def write_general_debug(self, message: bytes):
        self.write_command(transcode.encode_general_debug(message))

    def write_static_state(self, state: List[bool]):
        self.write_command(transcode.encode_static_state(state))

    def write_instructions(self, instructions: List[bytes]):
        if hasattr(transcode, "encode_instructions"):
            self.write_command(transcode.encode_instructions(instructions))
        else:
            for instr in instructions:
                self.write_command(instr)

class TickSpinBox(QDoubleSpinBox):
    """
    Time spinbox backed by FPGA clock ticks (10 ns).

    - Arrow/wheel stepping: exactly 1 tick.
    - Manual typed edits snap to nearest tick on commit (Enter or focus out).
    - Units displayed inside the box using Qt suffix.
    - No digit grouping.
    """

    def __init__(self, parent=None, unit_scale: float = 1.0, decimals: int = 0, unit: str = ""):
        super().__init__(parent)

        self.unit_scale = float(unit_scale)     # display units -> seconds
        self.clock_period = 10e-9               # 10 ns float for Qt range/step
        self._tick_s = Decimal("1e-8")          # 10 ns exact

        self.setDecimals(int(decimals))
        self.setSuffix(f" {unit}" if unit else "")

        # Step size = 1 tick in display units
        self.setSingleStep(self.clock_period / self.unit_scale)

        # Don't constantly commit while typing
        self.setKeyboardTracking(False)

        # Robust snapping hook for manual edits (Enter OR focus loss)
        self.editingFinished.connect(self._snap_from_editor_text)

    # ---------- parsing / snapping ----------

    def _editor_text_to_units_decimal(self):
        """
        Read the current lineEdit text, strip suffix/spaces, parse as Decimal in display units.
        Returns None if parsing fails.
        """
        le = self.lineEdit()
        if le is None:
            return None

        t = le.text().strip()

        suf = self.suffix()
        if suf and t.endswith(suf):
            t = t[:-len(suf)].rstrip()

        # Allow users to type spaces/commas casually
        t = t.replace(" ", "").replace(",", "")

        if not t:
            return Decimal("0")

        try:
            return Decimal(t)
        except InvalidOperation:
            return None

    def _snap_units_to_ticks(self, units_value: Decimal) -> Decimal:
        """Snap Decimal display-units value to nearest whole tick, return in display units."""
        seconds = units_value * Decimal(str(self.unit_scale))
        ticks = (seconds / self._tick_s).to_integral_value(rounding=ROUND_HALF_UP)
        snapped_seconds = ticks * self._tick_s
        snapped_units = snapped_seconds / Decimal(str(self.unit_scale))
        return snapped_units

    def _snap_from_editor_text(self) -> None:
        """
        Called on editingFinished.
        Parse what the user typed, snap to nearest tick, clamp to range, then display.
        """
        units_dec = self._editor_text_to_units_decimal()
        if units_dec is None:
            # If parsing failed, revert to last valid value
            self.setValue(self.value())
            return

        snapped_units = self._snap_units_to_ticks(units_dec)

        # Clamp in *units* to Qt min/max
        min_u = Decimal(str(self.minimum()))
        max_u = Decimal(str(self.maximum()))
        if snapped_units < min_u:
            snapped_units = min_u
        elif snapped_units > max_u:
            snapped_units = max_u

        # This updates display (Qt will append suffix)
        self.setValue(float(snapped_units))

    # ---------- enforce tick stepping ----------

    def stepBy(self, steps: int) -> None:
        """Enforce stepping in integer ticks."""
        ticks = self.get_ticks()
        ticks_new = ticks + int(steps)

        min_t = self._min_ticks()
        max_t = self._max_ticks()
        if ticks_new < min_t:
            ticks_new = min_t
        elif ticks_new > max_t:
            ticks_new = max_t

        self.set_value_from_ticks(ticks_new)

    def _min_ticks(self) -> int:
        units_val = Decimal(str(self.minimum()))
        seconds = units_val * Decimal(str(self.unit_scale))
        ticks = (seconds / self._tick_s).to_integral_value(rounding=ROUND_HALF_UP)
        return int(ticks)

    def _max_ticks(self) -> int:
        units_val = Decimal(str(self.maximum()))
        seconds = units_val * Decimal(str(self.unit_scale))
        ticks = (seconds / self._tick_s).to_integral_value(rounding=ROUND_HALF_UP)
        return int(ticks)

    # ---------- your existing API ----------

    def set_ticks_range(self, min_ticks: int, max_ticks: int):
        min_val = (min_ticks * self.clock_period) / self.unit_scale
        max_val = (max_ticks * self.clock_period) / self.unit_scale
        self.setRange(min_val, max_val)

    def get_ticks(self) -> int:
        units_val = Decimal(str(self.value()))
        seconds = units_val * Decimal(str(self.unit_scale))
        ticks = (seconds / self._tick_s).to_integral_value(rounding=ROUND_HALF_UP)
        return int(ticks)

    def set_value_from_ticks(self, ticks: int):
        seconds = Decimal(int(ticks)) * self._tick_s
        units = seconds / Decimal(str(self.unit_scale))
        self.setValue(float(units))



class MainWindow(QMainWindow):
    POLL_MS = 100

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pulse Generator Controller")
        self.resize(1220, 840)

        self.settings = QSettings("ndpulsegen", "gui")
        self.pg = PulseGenerator(self)

        self.pg.devicestate.connect(self.on_devicestate)
        self.pg.powerlinestate.connect(self.on_powerlinestate)
        self.pg.devicestate_extras.connect(self.on_devicestate_extras)
        self.pg.notification.connect(self.on_notification)
        self.pg.echo.connect(self.on_echo)
        self.pg.easyprint.connect(self.on_easyprint)
        self.pg.internalError.connect(self.on_internal_error)
        self.pg.bytesDropped.connect(self.on_bytes_dropped)
        self.pg.errorOccurred.connect(self.on_error)
        self.pg.connected.connect(self.on_connected)
        self.pg.disconnected.connect(self.on_disconnected)

        monofont = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        # Status bar
        self.connStatusLabel = QLabel("Disconnected")
        self.statusBar().addPermanentWidget(self.connStatusLabel)
        self.statusBar().showMessage("Ready", 2000)

        # Toolbar
        toolbar = QToolBar("Main")
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        self.refreshAction = QAction("Refresh Devices", self)
        self.refreshAction.triggered.connect(self.check_devices)
        toolbar.addAction(self.refreshAction)
        self.connectAction = QAction("Connect", self)
        self.connectAction.triggered.connect(self.connect_device)
        toolbar.addAction(self.connectAction)
        self.disconnectAction = QAction("Disconnect", self)
        self.disconnectAction.triggered.connect(self.disconnect_device)
        toolbar.addAction(self.disconnectAction)

        self.disconnectAction.setEnabled(False)

        # Device chooser
        self.deviceComboBox = QComboBox()
        self.deviceComboBox.currentIndexChanged.connect(self.on_device_selection_changed)

        # ---- Manual outputs group (top half) ----
        channelGrid = QGridLayout()
        self.channelWidgets = []  # list of (QLineEdit, QPushButton)
        for i in range(24):
            container = QWidget()
            vbox = QVBoxLayout(container)
            vbox.setContentsMargins(2, 2, 2, 2)
            vbox.setSpacing(2)
            label_edit = QLineEdit()
            label_edit.setPlaceholderText(f"Ch {i}")
            label_edit.setText("")
            label_edit.editingFinished.connect(
                lambda i=i, e=label_edit: self.save_channel_label(i, e)
            )
            vbox.addWidget(label_edit)

            btn = QPushButton(str(i))
            btn.setCheckable(True)
            btn.clicked.connect(self.make_toggle_handler(i))
            vbox.addWidget(btn)

            self.channelWidgets.append((label_edit, btn))
            row, col = divmod(i, 8)
            channelGrid.addWidget(container, row, col)

        manualBox = QGroupBox("Manual outputs")
        manualLayout = QVBoxLayout(manualBox)
        manualLayout.addLayout(channelGrid)

# ---- Channel groups (pattern presets, dynamic) ----
        self.groupConfigs = []  # list of dicts describing each group row

        self.groupsBox = QGroupBox("Channel groups")
        self.groupsLayout = QVBoxLayout(self.groupsBox) # Main layout for the box

        # Use a QGridLayout for perfect alignment of headers and rows
        self.groupsGrid = QGridLayout()
        self.groupsGrid.setSpacing(4)
        
        # Define column stretches so the first 3 columns expand equally
        self.groupsGrid.setColumnStretch(0, 1) # Name
        self.groupsGrid.setColumnStretch(1, 1) # Active High
        self.groupsGrid.setColumnStretch(2, 1) # Active Low
        self.groupsGrid.setColumnStretch(3, 0) # Actions (Fixed width)
        self.groupsGrid.setColumnStretch(4, 0) # Separator (Fixed width)
        self.groupsGrid.setColumnStretch(5, 0) # Remove (Fixed width)

        # -- Headers (Row 0) --
        self.groupsGrid.addWidget(self._make_header_label("Name"), 0, 0)
        self.groupsGrid.addWidget(self._make_header_label("Active High"), 0, 1)
        self.groupsGrid.addWidget(self._make_header_label("Active Low"), 0, 2)

        self.groupsLayout.addLayout(self.groupsGrid)

        # "Add group" button
        self.addGroupBtn = QPushButton("Add group")
        self.addGroupBtn.clicked.connect(self.add_group)

        # --- WRAPPER FOR ADD GROUP (Matches Remove button structure) ---
        self.addGroupWrapper = QWidget()
        addGroupLayout = QHBoxLayout(self.addGroupWrapper)
        addGroupLayout.setContentsMargins(0, 0, 0, 0)
        addGroupLayout.setAlignment(Qt.AlignCenter) # Center alignment
        addGroupLayout.addWidget(self.addGroupBtn)
        
        # We start with the Add button at row 1
        self.addGroupRow = 1
        self._place_add_group_button(self.addGroupRow)

        # Groups/labels are loaded per-device when a device is selected.



        # ---- Bottom: two columns ----
        # Status
        statusBox = QGroupBox("Status")
        statusLayout = QGridLayout(statusBox)
        statusLayout.addWidget(QLabel("Running:"), 0, 0)
        self.runningIndicator = QLabel()
        self.runningIndicator.setFixedSize(16, 16)
        statusLayout.addWidget(self.runningIndicator, 0, 1)
        statusLayout.addWidget(QLabel("Run enable - Software:"), 1, 0)
        self.softwareRunEnable = QLabel()
        self.softwareRunEnable.setFixedSize(16, 16)
        statusLayout.addWidget(self.softwareRunEnable, 1, 1)
        statusLayout.addWidget(QLabel("Run enable - Hardware:"), 2, 0)
        self.hardwareRunEnable = QLabel()
        self.hardwareRunEnable.setFixedSize(16, 16)
        statusLayout.addWidget(self.hardwareRunEnable, 2, 1)
        statusLayout.addWidget(QLabel("Current address:"), 3, 0)
        self.currentAddrLabel = QLabel("—")
        statusLayout.addWidget(self.currentAddrLabel, 3, 1)
        statusLayout.addWidget(QLabel("Final address:"), 4, 0)
        self.finalAddrLabel = QLabel("—")
        statusLayout.addWidget(self.finalAddrLabel, 4, 1)
        statusLayout.addWidget(QLabel("Total run time:"), 5, 0)
        self.runTimeLabel = QLabel("—")
        self.runTimeLabel.setFont(monofont)
        statusLayout.addWidget(self.runTimeLabel, 5, 1)

        # Synchronisation
        syncBox = QGroupBox("Synchronisation")
        syncLayout = QGridLayout(syncBox)
        syncLayout.addWidget(QLabel("Reference Clock:"), 0, 0)
        self.refClockLabel = QLabel("—")
        syncLayout.addWidget(self.refClockLabel, 0, 1)
        syncLayout.addWidget(QLabel("Powerline frequency (Hz):"), 1, 0)
        self.freqLabel = QLabel("—")
        syncLayout.addWidget(self.freqLabel, 1, 1)

        # Device info
        infoBox = QGroupBox("Device info")
        infoLayout = QGridLayout(infoBox)
        infoLayout.addWidget(QLabel("Serial number:"), 0, 0)
        self.snLabel = QLabel("—")
        infoLayout.addWidget(self.snLabel, 0, 1)
        infoLayout.addWidget(QLabel("Device type:"), 1, 0)
        self.devTypeLabel = QLabel("—")
        infoLayout.addWidget(self.devTypeLabel, 1, 1)
        infoLayout.addWidget(QLabel("Firmware version:"), 2, 0)
        self.fwLabel = QLabel("—")
        infoLayout.addWidget(self.fwLabel, 2, 1)
        infoLayout.addWidget(QLabel("Hardware version:"), 3, 0)
        self.hwLabel = QLabel("—")
        infoLayout.addWidget(self.hwLabel, 3, 1)
        infoLayout.addWidget(QLabel("Port:"), 4, 0)
        self.portLabel = QLabel("—")
        infoLayout.addWidget(self.portLabel, 4, 1)

        # Trigger in
        inBox = QGroupBox("Trigger in")
        inLayout = QGridLayout(inBox)
        inLayout.addWidget(QLabel("Accept hardware trigger:"), 0, 0)
        self.acceptHwCombo = QComboBox()
        self.acceptHwCombo.addItems(["never", "always", "single_run", "once"])
        self.acceptHwCombo.currentTextChanged.connect(self.on_accept_hw_changed)
        inLayout.addWidget(self.acceptHwCombo, 0, 1)
        inLayout.addWidget(QLabel("Wait for powerline:"), 1, 0)
        self.waitCheckbox = QCheckBox()
        self.waitCheckbox.stateChanged.connect(self.on_wait_changed)
        inLayout.addWidget(self.waitCheckbox, 1, 1)

        # -- UPDATED: Delay Spinbox (ms) --
        inLayout.addWidget(QLabel("Delay after powerline:"), 2, 0)
        # 1ns resolution in ms requires 6 decimals (0.000 001 ms)
        self.delaySpin = TickSpinBox(unit_scale=1e-3, decimals=6, unit="ms")
        # Range: 0 to 4194303 ticks
        self.delaySpin.set_ticks_range(0, 4194303)
        self.delaySpin.valueChanged.connect(self.on_delay_changed)
        inLayout.addWidget(self.delaySpin, 2, 1)

        # Trigger out
        outBox = QGroupBox("Trigger out")
        outLayout = QGridLayout(outBox)
        
        # -- UPDATED: Duration Spinbox (µs) --
        outLayout.addWidget(QLabel("Duration:"), 0, 0)
        # 1ns resolution in µs requires 3 decimals (0.001 µs)
        self.trigOutLenSpin = TickSpinBox(unit_scale=1e-6, decimals=3, unit="µs")
        # Range: 0 to 255 ticks
        self.trigOutLenSpin.set_ticks_range(0, 255)
        self.trigOutLenSpin.valueChanged.connect(self.on_trigout_len_changed)
        outLayout.addWidget(self.trigOutLenSpin, 0, 1)

        # -- UPDATED: Delay Spinbox (s) --
        outLayout.addWidget(QLabel("Delay:"), 1, 0)
        # 1ns resolution in s requires 9 decimals (0.000 000 001 s)
        self.trigOutDelaySpin = TickSpinBox(unit_scale=1.0, decimals=9, unit="s")
        # Range: 0 to 72057594037927935 ticks
        # Note: Standard float precision (53 bits) cannot distinguish 10ns steps
        # at the high end of this range (56 bits). 
        # Ideally, we would stick to ticks, but for GUI convenience we use the float approximation.
        # Since the Step Size is enforced by TickSpinBox, it should behave reasonably well.
        self.trigOutDelaySpin.set_ticks_range(0, 72057594037927935)
        self.trigOutDelaySpin.valueChanged.connect(self.on_trigout_delay_changed)
        outLayout.addWidget(self.trigOutDelaySpin, 1, 1)

        # Notifications
        notifBox = QGroupBox("Notifications")
        notifLayout = QGridLayout(notifBox)
        notifLayout.addWidget(QLabel("Notify when finished:"), 0, 0)
        self.notifyFinishedCheckbox = QCheckBox()
        self.notifyFinishedCheckbox.stateChanged.connect(self.on_notify_finished_changed)
        notifLayout.addWidget(self.notifyFinishedCheckbox, 0, 1)
        notifLayout.addWidget(QLabel("Notify on main trig out:"), 1, 0)
        self.notifyMainTrigOutCheckbox = QCheckBox()
        self.notifyMainTrigOutCheckbox.stateChanged.connect(self.on_notify_main_trig_out_changed)
        notifLayout.addWidget(self.notifyMainTrigOutCheckbox, 1, 1)
        notifLayout.addWidget(QLabel("Incoming Notifications"), 2, 0)
        self.notifLog = QTextEdit()
        self.notifLog.setReadOnly(True)
        notifLayout.addWidget(self.notifLog, 3, 0, 1, 2)

        # Two columns
        leftCol = QVBoxLayout()
        leftCol.addWidget(statusBox)
        leftCol.addWidget(syncBox)
        leftCol.addWidget(infoBox)
        leftCol.addStretch(1)
        rightCol = QVBoxLayout()
        rightCol.addWidget(inBox)
        rightCol.addWidget(outBox)
        rightCol.addWidget(notifBox)
        rightCol.addStretch(1)
        bottomCols = QHBoxLayout()
        bottomCols.addLayout(leftCol, 1)
        bottomCols.addLayout(rightCol, 1)

        # Central layout
        centralLayout = QVBoxLayout()
        # Device chooser + forget button
        deviceRow = QHBoxLayout()
        deviceRow.addWidget(self.deviceComboBox, 1)
        self.forgetDeviceButton = QPushButton('Forget device')
        self.forgetDeviceButton.setToolTip('Delete saved channel/group names for the selected device')
        self.forgetDeviceButton.clicked.connect(self.forget_selected_device)
        deviceRow.addWidget(self.forgetDeviceButton)
        centralLayout.addLayout(deviceRow)
        centralLayout.addWidget(manualBox)
        centralLayout.addWidget(self.groupsBox)
        centralLayout.addLayout(bottomCols)



        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        central = QWidget()
        central.setLayout(centralLayout)
        scroll.setWidget(central)

        self.setCentralWidget(scroll)

        # Timer
        self.request_timer = QTimer(self)
        self.request_timer.setInterval(self.POLL_MS)
        self.request_timer.timeout.connect(self.poll_status)

        # Initial device scan
        self.check_devices()

        # If exactly one *connected* device is present at startup, auto-connect to it
        connected_indices = []
        for i in range(self.deviceComboBox.count()):
            d = self.deviceComboBox.itemData(i)
            if isinstance(d, dict) and d.get("connected") is True:
                connected_indices.append(i)
        if len(connected_indices) == 1:
            self.deviceComboBox.setCurrentIndex(connected_indices[0])
            self.connect_device()

        # Load labels/groups for the currently selected device (even if not connected)
        self.on_device_selection_changed(self.deviceComboBox.currentIndex())



    # ---- Per-device settings helpers ----
    def _current_serial(self) -> Optional[str]:
        idx = self.deviceComboBox.currentIndex()
        if idx < 0:
            return None
        dev = self.deviceComboBox.itemData(idx)
        if isinstance(dev, dict):
            sn = dev.get("serial_number")
            return str(sn) if sn not in (None, "") else None
        return None

    def _device_key(self, subkey: str) -> str:
        """Return a QSettings key namespaced to the currently selected device serial."""
        sn = self._current_serial()
        if not sn:
            # No device selected: don't mix settings between devices.
            return ""
        return f"devices/{sn}/{subkey}"

    def _get_known_serials(self) -> List[str]:
        raw = self.settings.value("known_serials", "", type=str)
        if not raw:
            return []
        try:
            vals = json.loads(raw)
        except Exception:
            return []
        if not isinstance(vals, list):
            return []
        out: List[str] = []
        for v in vals:
            if v is None:
                continue
            sv = str(v).strip()
            if sv:
                out.append(sv)
        # de-dup while preserving order
        seen = set()
        uniq = []
        for sv in out:
            if sv in seen:
                continue
            seen.add(sv)
            uniq.append(sv)
        return uniq

    def _set_known_serials(self, serials: List[str]) -> None:
        # Store as JSON list
        self.settings.setValue("known_serials", json.dumps(serials))

    def _remember_serial(self, sn: Optional[str]) -> None:
        if not sn:
            return
        serials = self._get_known_serials()
        if sn not in serials:
            serials.append(sn)
            self._set_known_serials(serials)


    def _migrate_legacy_settings_to_device(self, sn: str) -> None:
        """One-time migration: if per-device keys are empty, copy legacy global keys."""
        # Migrate channel labels
        any_device_label = False
        for i in range(24):
            dk = f"devices/{sn}/channels/{i}"
            if self.settings.value(dk, "", type=str):
                any_device_label = True
                break

        if not any_device_label:
            for i in range(24):
                legacy = self.settings.value(f"channels/{i}", "", type=str)
                if legacy:
                    self.settings.setValue(f"devices/{sn}/channels/{i}", legacy)

        # Migrate groups
        device_groups_key = f"devices/{sn}/channel_groups"
        if not self.settings.value(device_groups_key, "", type=str):
            legacy_groups = self.settings.value("channel_groups", "", type=str)
            if legacy_groups:
                self.settings.setValue(device_groups_key, legacy_groups)


    def save_channel_label(self, channel_index: int, edit: QLineEdit) -> None:
        key = self._device_key(f"channels/{channel_index}")
        if not key:
            return
        self.settings.setValue(key, edit.text())

    def load_channel_labels_for_current_device(self) -> None:
        sn = self._current_serial()
        # Update placeholders to include the serial (nice UX), but keep it subtle.
        for i, (label_edit, _) in enumerate(self.channelWidgets):
            label_edit.setPlaceholderText(f"Ch {i}")
            if not sn:
                label_edit.setText("")
                continue
            key = f"devices/{sn}/channels/{i}"
            saved = self.settings.value(key, "", type=str)
            if saved is None:
                saved = ""
            # Avoid moving cursor / triggering odd focus behaviour if user is actively editing.
            old_block = label_edit.blockSignals(True)
            label_edit.setText(saved)
            label_edit.blockSignals(old_block)

    def clear_groups_ui(self) -> None:
        # Remove all existing group rows
        for cfg in list(self.groupConfigs):
            for w in cfg.get("widgets", []):
                self.groupsGrid.removeWidget(w)
                w.deleteLater()
        self.groupConfigs = []
        # Re-place Add Group button row
        self.addGroupRow = 1
        self._place_add_group_button(self.addGroupRow)

    def on_device_selection_changed(self, idx: int) -> None:
        # Called when the device dropdown changes (or when we explicitly refresh devices).
        sn = self._current_serial()
        if sn:
            self._remember_serial(sn)
            self._migrate_legacy_settings_to_device(sn)

        # Swap labels/groups to match selected serial.
        self.load_channel_labels_for_current_device()
        self.clear_groups_ui()
        self.load_groups_from_settings()

    def forget_selected_device(self) -> None:
        """Delete saved channel/group names for the selected device from QSettings.

        Safety rules:
        - You cannot delete settings for the device this GUI instance is currently connected to.
        - If another ndpulsegen GUI instance is connected to that device, it should be holding
          a per-device settings lock, and we will refuse to delete.
        """
        dev = self.deviceComboBox.currentData()
        if not isinstance(dev, dict):
            self.statusBar().showMessage("No device selected.", 3000)
            return
        sn = dev.get("serial_number")
        if sn is None:
            self.statusBar().showMessage("Selected item has no serial number.", 4000)
            return
        try:
            sn_int = int(sn)
        except Exception:
            self.statusBar().showMessage("Invalid serial number.", 4000)
            return

        # Don't allow deleting settings for the device we're currently connected to.
        if self.pg.is_open():
            cur_sn = getattr(self.pg, "serial_number_save", None)
            try:
                if cur_sn is not None and int(cur_sn) == sn_int:
                    self.statusBar().showMessage("Disconnect before forgetting the connected device.", 5000)
                    return
            except Exception:
                pass

        # Try to acquire the per-device lock. If another instance is using the device/settings, refuse.
        lk = InterProcessLock(device_settings_lock_path(sn_int))
        if not lk.acquire(blocking=False):
            self.statusBar().showMessage("Cannot forget: device settings are in use by another instance.", 6000)
            return

        try:
            # Remove device-specific settings subtree
            self.settings.remove(f"devices/{sn_int}")

            # Also remove from known serials list
            serials = self._get_known_serials()
            serials = [s for s in serials if str(s) != str(sn_int)]
            self._set_known_serials(serials)

            self.settings.sync()
            self.statusBar().showMessage(f"Forgot device {sn_int}.", 4000)
        finally:
            lk.release()

        # Refresh list/UI
        self.check_devices()



    # Helpers
    @staticmethod
    def _set_indicator(widget: QLabel, on: bool):
        widget.setStyleSheet(
            "background-color: green; border-radius: 8px;"
            if on
            else "background-color: red; border-radius: 8px;"
        )


    def _update_tickspin_from_device(self, spin: 'TickSpinBox', ticks: int) -> None:
        """Update a TickSpinBox from device state without fighting user typing.

        When the user is actively editing (spin or its lineEdit has focus), skip the update
        so the typed text isn't immediately overwritten by the 100 ms poll loop.
        """
        try:
            le = spin.lineEdit()
            if spin.hasFocus() or (le is not None and le.hasFocus()):
                return
        except Exception:
            pass

        old = spin.blockSignals(True)
        try:
            spin.set_value_from_ticks(ticks)
        finally:
            spin.blockSignals(old)


    def _state_to_bools(self, state_val) -> List[bool]:
        """
        Convert the 'state' field from decode_devicestate into a 24-element bool list.

        decode_devicestate currently returns a NumPy array of bits, but we also
        support an int bitfield for robustness.
        """
        if isinstance(state_val, int):
            return [bool((state_val >> i) & 1) for i in range(24)]
        # assume iterable / NumPy array
        return [bool(state_val[i]) for i in range(24)]

    def _apply_state_to_buttons(self, state_bools: List[bool]):
        for i, (_, btn) in enumerate(self.channelWidgets):
            old = btn.blockSignals(True)
            btn.setChecked(bool(state_bools[i]))
            btn.blockSignals(old)

    def _make_header_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        return lbl

    def _place_add_group_button(self, row_idx):
            """Helper to move the 'Add group' button to a specific row in the grid."""
            # Vertical separator for the add row
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setFrameShadow(QFrame.Sunken)
            
            # Track the wrapper (not the button) so we can remove it later
            self.add_group_widgets = [sep, self.addGroupWrapper]

            self.groupsGrid.addWidget(sep, row_idx, 4)
            
            # Add the wrapper to the grid
            self.groupsGrid.addWidget(self.addGroupWrapper, row_idx, 5)
            
            # Update our tracking index
            self.addGroupRow = row_idx

    def add_group(self, name: str = "", active_high: str = "", active_low: str = ""):
            # Determine insertion row
            row = self.addGroupRow

            # 1. Temporarily remove "Add Group" widgets
            for w in self.add_group_widgets:
                self.groupsGrid.removeWidget(w)
                w.setParent(None)

            # 2. Create the new row widgets
            nameEdit = QLineEdit()
            nameEdit.setPlaceholderText("Group")
            if name: nameEdit.setText(name)

            activeHighEdit = QLineEdit()
            activeHighEdit.setPlaceholderText("e.g. 0,1,5-7")
            if active_high: activeHighEdit.setText(active_high)

            activeLowEdit = QLineEdit()
            activeLowEdit.setPlaceholderText("e.g. 2,3")
            if active_low: activeLowEdit.setText(active_low)

            # --- ACTIONS COLUMN (Wrapped in QWidget+Layout) ---
            activateBtn = QPushButton("Activate")
            deactivateBtn = QPushButton("Deactivate")
            
            actionsWidget = QWidget()
            actionsLayout = QHBoxLayout(actionsWidget)
            actionsLayout.setContentsMargins(0, 0, 0, 0)
            actionsLayout.setSpacing(4)
            actionsLayout.addWidget(activateBtn)
            actionsLayout.addWidget(deactivateBtn)

            # Separator
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setFrameShadow(QFrame.Sunken)

            # --- REMOVE COLUMN (Now Wrapped to Match!) ---
            removeBtn = QPushButton("Remove")
            
            # Match the "Add group" width if desired
            removeBtn.setFixedWidth(self.addGroupBtn.sizeHint().width())

            # WRAPPER START
            removeWidget = QWidget()
            removeLayout = QHBoxLayout(removeWidget)
            removeLayout.setContentsMargins(0, 0, 0, 0) # crucial
            removeLayout.setAlignment(Qt.AlignCenter)   # ensures button stays centered
            removeLayout.addWidget(removeBtn)
            # WRAPPER END

            # 3. Add widgets to Grid
            self.groupsGrid.addWidget(nameEdit, row, 0)
            self.groupsGrid.addWidget(activeHighEdit, row, 1)
            self.groupsGrid.addWidget(activeLowEdit, row, 2)
            self.groupsGrid.addWidget(actionsWidget, row, 3)
            self.groupsGrid.addWidget(sep, row, 4)
            self.groupsGrid.addWidget(removeWidget, row, 5) # Add the WIDGET, not the button

            # 4. Save Config
            # IMPORTANT: Store 'removeWidget' in the list so it gets deleted properly later
            cfg = {
                "widgets": [nameEdit, activeHighEdit, activeLowEdit, actionsWidget, sep, removeWidget],
                "name": nameEdit,
                "on": activeHighEdit,
                "off": activeLowEdit,
            }
            self.groupConfigs.append(cfg)

            # Wire signals
            activateBtn.clicked.connect(lambda _, c=cfg: self.on_group_action(c, True))
            deactivateBtn.clicked.connect(lambda _, c=cfg: self.on_group_action(c, False))
            removeBtn.clicked.connect(lambda _, c=cfg: self.remove_group(c))
            
            nameEdit.editingFinished.connect(self.save_groups_to_settings)
            activeHighEdit.editingFinished.connect(self.save_groups_to_settings)
            activeLowEdit.editingFinished.connect(self.save_groups_to_settings)

            # 5. Re-add the "Add Group" button
            self._place_add_group_button(row + 1)
            self.save_groups_to_settings()


    def remove_group(self, cfg: dict):
        """
        Remove the specified group row from UI and config list.
        """
        if cfg in self.groupConfigs:
            self.groupConfigs.remove(cfg)

        # Delete all widgets associated with this row
        for w in cfg["widgets"]:
            self.groupsGrid.removeWidget(w)
            w.deleteLater()

        self.save_groups_to_settings()

    def load_groups_from_settings(self):
        """
        Load channel groups from QSettings. If none exist, create one example group.
        """
        key = self._device_key("channel_groups")
        if not key:
            return
        data = self.settings.value(key, "", type=str)
        # if not data:
        #     # No saved groups: create a single example group (only once)
        #     self.add_group(name="Example", active_high="0,1,2", active_low="3,4,5")
        #     return

        try:
            groups = json.loads(data)
        except Exception:
            # If settings are corrupted, fall back to a single example group
            # self.add_group(name="Example", active_high="0,1,2", active_low="3,4,5")
            return

        if not groups:
            # Saved but empty: respect that (user removed all groups)
            return

        for g in groups:
            self.add_group(
                name=g.get("name", ""),
                active_high=g.get("on", ""),
                active_low=g.get("off", ""),
            )

    def save_groups_to_settings(self):
        """
        Save the current list of groups to QSettings as JSON.
        """
        groups = []
        for cfg in self.groupConfigs:
            groups.append(
                {
                    "name": cfg["name"].text(),
                    "on": cfg["on"].text(),
                    "off": cfg["off"].text(),
                }
            )
        key = self._device_key("channel_groups")
        if not key:
            return
        self.settings.setValue(key, json.dumps(groups))


    def parse_channel_list(self, text: str) -> set:
        """
        Parse a string like "0,1,4-7" into a set of valid channel indices.
        Ignores invalid entries and clamps to available channels.
        """
        result = set()
        text = text.strip()
        if not text:
            return result

        parts = text.replace(" ", "").split(",")
        n_channels = len(self.channelWidgets)

        for part in parts:
            if not part:
                continue
            if "-" in part:
                # Range like 3-7
                try:
                    start_str, end_str = part.split("-", 1)
                    start = int(start_str)
                    end = int(end_str)
                except ValueError:
                    continue
                if start > end:
                    start, end = end, start
                for i in range(start, end + 1):
                    if 0 <= i < n_channels:
                        result.add(i)
            else:
                try:
                    idx = int(part)
                except ValueError:
                    continue
                if 0 <= idx < n_channels:
                    result.add(idx)

        return result

    
    def on_group_action(self, cfg: dict, activate: bool):
        # (Logic remains mostly the same, just accessing cfg directly)
        active_high = self.parse_channel_list(cfg["on"].text())
        active_low = self.parse_channel_list(cfg["off"].text())

        # Apply pattern only to channels in this group
        for i, (_, chan_btn) in enumerate(self.channelWidgets):
            if i in active_high:
                old = chan_btn.blockSignals(True)
                chan_btn.setChecked(activate)
                chan_btn.blockSignals(old)
            elif i in active_low:
                old = chan_btn.blockSignals(True)
                chan_btn.setChecked(not activate)
                chan_btn.blockSignals(old)

        self.send_static_state()

        name = cfg["name"].text() or "Group"
        self.statusBar().showMessage(
            f"Group '{name}' {'activated' if activate else 'deactivated'}",
            2000,
        )



    def send_static_state(self):
        state = [btn.isChecked() for _, btn in self.channelWidgets]
        try:
            self.pg.write_static_state(state)
            self.statusBar().showMessage("Static state sent", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error sending static state: {e}", 3000)

    # UI -> Device
    def make_toggle_handler(self, channel: int):
        def handler(checked: bool):
            self.send_static_state()

        return handler

    def on_accept_hw_changed(self, text: str):
        try:
            self.pg.write_device_options(accept_hardware_trigger=text)
            self.statusBar().showMessage("Updated accept_hardware_trigger", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_wait_changed(self, state: int):
        try:
            self.pg.write_powerline_trigger_options(trigger_on_powerline=bool(state))
            self.statusBar().showMessage("Updated trigger_on_powerline", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_delay_changed(self):
        # Use helper to get exact ticks
        value_clock_cycles = self.delaySpin.get_ticks()
        try:
            self.pg.write_powerline_trigger_options(powerline_trigger_delay=value_clock_cycles)
            self.statusBar().showMessage(f"Updated powerline delay: {value_clock_cycles} ticks", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_trigout_len_changed(self):
        value_clock_cycles = self.trigOutLenSpin.get_ticks()
        try:
            self.pg.write_device_options(trigger_out_length=value_clock_cycles)
            self.statusBar().showMessage(f"Updated trig out length: {value_clock_cycles} ticks", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_trigout_delay_changed(self):
        value_clock_cycles = self.trigOutDelaySpin.get_ticks()
        try:
            self.pg.write_device_options(trigger_out_delay=value_clock_cycles)
            self.statusBar().showMessage(f"Updated trig out delay: {value_clock_cycles} ticks", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_notify_finished_changed(self, state: int):
        try:
            self.pg.write_device_options(notify_when_run_finished=bool(state))
            self.statusBar().showMessage("Updated notify_when_run_finished", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    def on_notify_main_trig_out_changed(self, state: int):
        try:
            self.pg.write_device_options(notify_on_main_trig_out=bool(state))
            self.statusBar().showMessage("Updated notify_on_main_trig_out", 1000)
        except Exception as e:
            self.statusBar().showMessage(f"Error: {e}", 3000)

    # Connection actions
    def check_devices(self):
        try:
            prev_sn = self._current_serial()

            devs = self.pg.get_connected_devices().get("validated_devices", [])
            connected_by_sn: Dict[str, dict] = {}
            for d in devs:
                sn = d.get("serial_number")
                if sn is None:
                    continue
                sn = str(sn)
                connected_by_sn[sn] = dict(d)
                self._remember_serial(sn)

            known = self._get_known_serials()

            # Build ordered list: connected first (in reported order), then known-but-not-connected.
            connected_sns = [str(d.get("serial_number")) for d in devs if d.get("serial_number") is not None]
            extra_sns = [sn for sn in known if sn not in connected_by_sn]
            all_sns = connected_sns + extra_sns

            old_block = self.deviceComboBox.blockSignals(True)
            self.deviceComboBox.clear()

            for sn in all_sns:
                if sn in connected_by_sn:
                    d = connected_by_sn[sn]
                    label = f"SN {sn} | FW {d.get('firmware_version')} | {d.get('comport')}"
                    d2 = dict(d)
                    d2["serial_number"] = sn
                    d2["connected"] = True
                    self.deviceComboBox.addItem(label, d2)
                else:
                    d2 = {"serial_number": sn, "firmware_version": "", "comport": "", "connected": False}
                    label = f"SN {sn} | (not connected)"
                    self.deviceComboBox.addItem(label, d2)

            # Restore selection where possible
            if all_sns:
                if prev_sn and prev_sn in all_sns:
                    self.deviceComboBox.setCurrentIndex(all_sns.index(prev_sn))
                else:
                    self.deviceComboBox.setCurrentIndex(0)

            self.deviceComboBox.blockSignals(old_block)

            # Apply per-device UI state for current selection
            self.on_device_selection_changed(self.deviceComboBox.currentIndex())

            self.statusBar().showMessage("Devices updated." if all_sns else "No devices found.", 3000)
        except Exception as e:
            self.statusBar().showMessage(f"Error checking devices: {e}", 5000)


    def connect_device(self):
        idx = self.deviceComboBox.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "Connect", "No device selected.")
            return
        dev = self.deviceComboBox.itemData(idx)
        if not isinstance(dev, dict) or not dev.get("serial_number"):
            QMessageBox.warning(self, "Connect", "Invalid device selection.")
            return
        if dev.get("connected") is False:
            QMessageBox.warning(self, "Connect", "That device is not currently connected. Plug it in and click Refresh.")
            return
        try:
            # ok = self.pg.connect(serial_number=dev.get("serial_number"))

            try:
                sn_int = int(dev.get("serial_number"))
            except (TypeError, ValueError):
                QMessageBox.warning(self, "Connect", "Invalid serial number for selected device.")
                return

            # Set UI to a connecting state *before* calling pg.connect(), because pg.connect() emits
            # the 'connected' signal synchronously and on_connected() will set the final state.
            self.statusBar().showMessage("Connecting…", 2000)
            self.connStatusLabel.setText("Connecting…")
            self.portLabel.setText(str(dev.get("comport")))
            self.snLabel.setText(str(dev.get("serial_number")))

            ok = self.pg.connect(serial_number=sn_int, port=dev.get('comport'))
            if ok:
                # Device-info fields will be populated by the immediate echo emitted from pg.connect()
                # and periodic polling will start in on_connected().
                pass
            else:
                self.statusBar().showMessage("Connect failed: device not found.", 4000)
        except Exception as e:
            self.statusBar().showMessage(f"Error connecting: {e}", 5000)

    def disconnect_device(self):
        try:
            self.request_timer.stop()
            self.pg.disconnect()
            self.statusBar().showMessage("Disconnected", 2000)
            self.connStatusLabel.setText("Disconnected")
            self.portLabel.setText("—")
        except Exception as e:
            self.statusBar().showMessage(f"Error disconnecting: {e}", 5000)

    def poll_status(self):
        if not self.pg.is_open():
            return
        try:
            self.pg.write_action(request_state=True, request_powerline_state=True, request_state_extras=True)
        except Exception as e:
            self.statusBar().showMessage(f"Error requesting state: {e}", 3000)

    # Worker slots
    def on_connected(self, port: str):
        serial = getattr(self.pg, 'serial_number_save', None)
        serial_str = str(serial) if serial is not None else "—"
        self.statusBar().showMessage(f"Connected (SN {serial_str}) on {port}", 3000)
        self.connStatusLabel.setText(f"Connected: {serial_str}")
        self.portLabel.setText(port)
        # Button state
        self.connectAction.setEnabled(False)
        self.disconnectAction.setEnabled(True)
        self.refreshAction.setEnabled(False)
        # Start periodic status polling
        if not self.request_timer.isActive():
            self.request_timer.start()
            self.poll_status()

    def on_disconnected(self):
        self.statusBar().showMessage("Disconnected", 3000)
        self.connStatusLabel.setText("Disconnected")
        self.portLabel.setText("—")
        # Button state
        self.connectAction.setEnabled(True)
        self.disconnectAction.setEnabled(False)
        self.refreshAction.setEnabled(True)
                # Stop periodic status polling
        if self.request_timer.isActive():
            self.request_timer.stop()
# Auto refresh so the list reflects the unplug immediately
        QTimer.singleShot(0, self.check_devices)

    def on_error(self, message: str):
        self.statusBar().showMessage(f"ERROR: {message}", 5000)

    def on_bytes_dropped(self, msg_id: int, ts: float):
        self.statusBar().showMessage(f"Dropped byte id {msg_id} at {ts:.3f}", 2000)

    def on_echo(self, msg: dict):
        # decode_echo always provides these keys
        self.snLabel.setText(str(msg["serial_number"]))
        self.devTypeLabel.setText(str(msg["device_type"]))
        self.fwLabel.setText(str(msg["firmware_version"]))
        self.hwLabel.setText(str(msg["hardware_version"]))

    def on_internal_error(self, msg: dict):
        # An internal error occoured in the pulse generator.
        text = f"Internal error: {msg}"
        self.statusBar().showMessage(text, 10000)
        self.notifLog.append(text)

    def on_easyprint(self, msg: dict):
        # decode_easyprint always returns 'easy_printed_value'
        self.statusBar().showMessage(str(msg["easy_printed_value"]), 3000)

    def on_notification(self, msg: dict):
        # decode_notification returns: address, address_notify, trigger_notify,
        # finished_notify, run_time. For now just log the dict.
        self.notifLog.append(str(msg))

    def on_powerlinestate(self, msg: dict):
        # decode_powerlinestate returns:
        # 'trig_on_powerline', 'powerline_locked', 'powerline_period', 'powerline_trigger_delay'
        period_cycles = msg["powerline_period"]
        delay_cycles = msg["powerline_trigger_delay"]
        trig_on_powerline = msg["trig_on_powerline"]

        # Update powerline frequency label (period is in 10 ns clock cycles)
        if period_cycles:
            freq_hz = 1.0 / (period_cycles * 10e-9)
            self.freqLabel.setText(f"{freq_hz:.3f}")
        else:
            self.freqLabel.setText("—")

        # Update "wait for powerline" checkbox from trig_on_powerline
        old = self.waitCheckbox.blockSignals(True)
        self.waitCheckbox.setChecked(bool(trig_on_powerline))
        self.waitCheckbox.blockSignals(old)

        # Update delay spinbox (don’t overwrite while user is typing)
        delay_cycles = msg["powerline_trigger_delay"]
        self._update_tickspin_from_device(self.delaySpin, delay_cycles)

    @staticmethod
    def _format_run_time_cycles(run_time_cycles: Any) -> str:
        """Format FPGA run-time counter (10 ns ticks) as d hh:mm:ss.nnn nnn nnn."""
        # Use integer arithmetic to preserve nanosecond formatting (even though tick is 10 ns).
        try:
            cycles = int(run_time_cycles)
        except (TypeError, ValueError):
            return "—"
        if cycles < 0:
            return "—"

        total_ns = cycles * 10  # 10 ns per FPGA clock tick

        NS_PER_DAY = 86_400 * 1_000_000_000
        NS_PER_HOUR = 3_600 * 1_000_000_000
        NS_PER_MIN = 60 * 1_000_000_000

        days, rem = divmod(total_ns, NS_PER_DAY)
        hours, rem = divmod(rem, NS_PER_HOUR)
        minutes, rem = divmod(rem, NS_PER_MIN)
        seconds, ns = divmod(rem, 1_000_000_000)

        # Group the fractional part in triplets using a narrow no-break space (looks like a half space).
        sep = "\u202f"
        frac = f"{ns:09d}"
        frac_grouped = f"{frac[0:3]}{sep}{frac[3:6]}{sep}{frac[6:9]}"

        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}.{frac_grouped}"

    def on_devicestate_extras(self, msg: dict):
        # decode_devicestate_extras returns 'run_time'
        run_time = msg["run_time"]
        self.runTimeLabel.setText(self._format_run_time_cycles(run_time))

    def on_devicestate(self, ds: dict):
        # decode_devicestate returns a fixed set of keys; no need for existence checks
        self._set_indicator(self.runningIndicator, bool(ds["running"]))
        self._set_indicator(self.softwareRunEnable, bool(ds["software_run_enable"]))
        self._set_indicator(self.hardwareRunEnable, bool(ds["hardware_run_enable"]))

        self.currentAddrLabel.setText(str(ds["current_address"]))
        self.finalAddrLabel.setText(str(ds["final_address"]))

        # Accept hardware trigger combo
        val = str(ds["accept_hardware_trigger"])
        idx = self.acceptHwCombo.findText(val)
        if idx >= 0:
            old = self.acceptHwCombo.blockSignals(True)
            self.acceptHwCombo.setCurrentIndex(idx)
            self.acceptHwCombo.blockSignals(old)

        # Reference clock source
        self.refClockLabel.setText(f"{ds['clock_source']}")

        # Notification checkboxes (note naming from decode_devicestate)
        old = self.notifyFinishedCheckbox.blockSignals(True)
        self.notifyFinishedCheckbox.setChecked(bool(ds["notify_on_run_finished"]))
        self.notifyFinishedCheckbox.blockSignals(old)

        old = self.notifyMainTrigOutCheckbox.blockSignals(True)
        self.notifyMainTrigOutCheckbox.setChecked(bool(ds["notify_on_main_trig_out"]))
        self.notifyMainTrigOutCheckbox.blockSignals(old)

        # trigger_out_length
        trig_len_cycles = ds["trigger_out_length"]
        self._update_tickspin_from_device(self.trigOutLenSpin, trig_len_cycles)

        # trigger_out_delay
        trig_delay_cycles = ds["trigger_out_delay"]
        self._update_tickspin_from_device(self.trigOutDelaySpin, trig_delay_cycles)

        # Output state -> manual buttons
        self._apply_state_to_buttons(self._state_to_bools(ds["state"]))

    def closeEvent(self, ev):
        try:
            self.request_timer.stop()
            if self.pg and self.pg.is_open():
                self.pg.disconnect()
        finally:
            super().closeEvent(ev)

def main():
    app = QApplication(sys.argv)

    # Load icon relative to this file
    base_dir = os.path.abspath(os.path.dirname(__file__))
    icon_path = os.path.join(base_dir, "icons", "ndpulsegen.png")

    if os.path.exists(icon_path):
        icon = QIcon(icon_path)
        app.setWindowIcon(icon)
    else:
        print("Icon not found:", icon_path)

    w = MainWindow()
    if os.path.exists(icon_path):
        w.setWindowIcon(QIcon(icon_path))

    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()