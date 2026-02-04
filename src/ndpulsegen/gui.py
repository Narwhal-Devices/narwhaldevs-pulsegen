# gui.py (with explicit "Manual outputs" group for per-channel control)
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import sys
import struct
import time
import threading
from typing import Optional, List, Dict, Any
import json

from PyQt5.QtCore import (
    Qt, QObject, QThread, pyqtSignal, QTimer, QSettings, QSize
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QComboBox, QLabel, QAction, QToolBar, QGroupBox, QCheckBox,
    QTextEdit, QDoubleSpinBox, QLineEdit, QMessageBox, QScrollArea, QFrame,
    QSizePolicy,
)


import serial
import serial.tools.list_ports

from . import transcode


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
        self.ser = serial.Serial()
        self.ser.timeout = 0.1
        self.ser.write_timeout = 1
        self.ser.baudrate = 12000000

        self._write_lock = threading.Lock()
        self._thread: Optional[QThread] = None
        self._worker: Optional[SerialWorker] = None

        self._valid_vid = 1027
        self._valid_pid = 24592

        self.serial_number_save: Optional[int] = None
        self.device_type: Optional[int] = None
        self.firmware_version: Optional[str] = None
        self.hardware_version: Optional[str] = None

    def is_open(self) -> bool:
        return bool(self.ser and self.ser.is_open)

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
        self._worker.errorOccurred.connect(self.errorOccurred)
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
        if serial_number is not None or port is None:
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

        self.ser.port = target_port
        self.ser.open()
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._start_reader()

        if device_meta:
            self.serial_number_save = device_meta.get("serial_number")
            self.device_type = device_meta.get("device_type")
            self.firmware_version = device_meta.get("firmware_version")
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
        s = serial.Serial()
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

    def write_command(self, encoded_command: bytes):
        if not self.is_open():
            raise serial.serialutil.PortNotOpenError("Serial port is not open")
        with self._write_lock:
            self.ser.write(encoded_command)

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

        # Device chooser
        self.deviceComboBox = QComboBox()

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
            saved = self.settings.value(f"channels/{i}", "")
            if saved is None:
                saved = ""
            label_edit.setText(saved)
            label_edit.editingFinished.connect(
                lambda i=i, e=label_edit: self.settings.setValue(f"channels/{i}", e.text())
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

        # Load saved groups
        self.load_groups_from_settings()



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
        centralLayout.addWidget(self.deviceComboBox)
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

        # If exactly one device is present at startup, auto-connect to it
        if self.deviceComboBox.count() == 1:
            self.connect_device()


    # Helpers
    @staticmethod
    def _set_indicator(widget: QLabel, on: bool):
        widget.setStyleSheet(
            "background-color: green; border-radius: 8px;"
            if on
            else "background-color: red; border-radius: 8px;"
        )

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
        data = self.settings.value("channel_groups", "", type=str)
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
        self.settings.setValue("channel_groups", json.dumps(groups))


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
            devs = self.pg.get_connected_devices().get("validated_devices", [])
            self.deviceComboBox.clear()
            for d in devs:
                label = f"SN {d.get('serial_number')} | FW {d.get('firmware_version')} | {d.get('comport')}"
                self.deviceComboBox.addItem(label, d)
            self.statusBar().showMessage("Devices updated." if devs else "No devices found.", 3000)
        except Exception as e:
            self.statusBar().showMessage(f"Error checking devices: {e}", 5000)

    def connect_device(self):
        idx = self.deviceComboBox.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "Connect", "No device selected.")
            return
        dev = self.deviceComboBox.itemData(idx)
        try:
            ok = self.pg.connect(serial_number=dev.get("serial_number"))
            if ok:
                self.statusBar().showMessage("Connected", 2000)
                self.connStatusLabel.setText(f"Connected: {dev.get('comport')}")
                self.portLabel.setText(str(dev.get("comport")))
                self.snLabel.setText(str(dev.get("serial_number")))
                self.devTypeLabel.setText(str(dev.get("device_type")))
                self.fwLabel.setText(str(dev.get("firmware_version")))
                self.hwLabel.setText(str(dev.get("hardware_version")))
                self.request_timer.start()
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
        self.statusBar().showMessage(f"Connected on {port}", 3000)
        self.connStatusLabel.setText(f"Connected: {port}")
        self.portLabel.setText(port)

    def on_disconnected(self):
        self.statusBar().showMessage("Disconnected", 3000)
        self.connStatusLabel.setText("Disconnected")
        self.portLabel.setText("—")

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

        # Update delay spinbox
        delay_cycles = msg["powerline_trigger_delay"]
        old = self.delaySpin.blockSignals(True)
        self.delaySpin.set_value_from_ticks(delay_cycles)
        self.delaySpin.blockSignals(old)

    def on_devicestate_extras(self, msg: dict):
        # decode_devicestate_extras returns 'run_time'
        run_time = msg["run_time"]
        self.runTimeLabel.setText(f"{run_time}")

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
        old = self.trigOutLenSpin.blockSignals(True)
        self.trigOutLenSpin.set_value_from_ticks(trig_len_cycles)
        self.trigOutLenSpin.blockSignals(old)

        # trigger_out_delay
        trig_delay_cycles = ds["trigger_out_delay"]
        old = self.trigOutDelaySpin.blockSignals(True)
        self.trigOutDelaySpin.set_value_from_ticks(trig_delay_cycles)
        self.trigOutDelaySpin.blockSignals(old)

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
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
