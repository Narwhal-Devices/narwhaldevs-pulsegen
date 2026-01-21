import numpy as np
import sys
import time
import serial
import serial.tools.list_ports
import struct
import threading
import queue
from . import transcode

class PulseGenerator():
    def __init__(self):
        #setup serial port
        self.ser = serial.Serial()
        self.ser.timeout = 0.1        #block read for 100ms
        self.ser.writeTimeout = 1     #timeout for write
        self.ser.baudrate = 12000000
        # self.ser.exclusive = True

        # For every message type that can recieved by the monitor thread, make a queue that the main thread will interact with
        self.msgin_queues = {decodeinfo['message_type']:queue.Queue() for decodeinfo in transcode.msgin_decodeinfo.values()}
        self.msgin_queues['bytes_dropped'] = queue.Queue()

        # If the main thread needs to close the read thread, it will set this event.
        self.close_readthread_event = threading.Event()

        self.device_type = 1 # The designator of the pulse generator

        # encoding instructions is done all the time by the user. Make it also a method so people's code can be more self contained. 
        self.encode_instruction = transcode.encode_instruction

    def connect(self, serial_number=None):
        # Get a list of all available Narwhal Devices devices. Devices won't appear if they are connected to another program
        validated_devices = self.get_connected_devices()['validated_devices']
        # If a serial number is specified, search for a device with that number. Otherwise, search for the first pulse generator found.
        device_found = False
        for device in validated_devices:
            if (serial_number == device['serial_number']) or (serial_number == None and device['device_type'] == self.device_type):
                device_found = True
                break
        if device_found:
            self.serial_number_save = device['serial_number'] # This is incase the the program needs to automatically reconnect. Porbably superfluous at the moment.
            self.ser.port = device['comport']
            self.ser.open()                 
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.serial_read_thread = threading.Thread(target=self.monitor_serial, daemon=True)
            self.serial_read_thread.start()
        else:
            if serial_number == None:
                ex = 'No Narwhal Devices Pulse Generator found. It might be unconnected, or another program might be connected to it.'
                raise Exception(ex)
            else:
                ex = f'No Narwhal Devices Pulse Generator found with serial number: {serial_number}.  It might be unconnected, or another program might be connected to it.'
                raise Exception(ex)

    def get_connected_devices(self):
        # This attmpts to connect to all serial devices with valid parameters, and if it is a valid Narwhal Device, it adds them to a list and disconnects
        valid_ports = []
        comports = list(serial.tools.list_ports.comports())
        for comport in comports:
            if 'vid' in vars(comport) and 'pid' in vars(comport):
                if vars(comport)['vid'] == 1027 and vars(comport)['pid'] == 24592:
                    if sys.platform == 'linux' and comport.location.endswith('.0'):
                        # ignore JTAG interface incorrectly exposed as a serial port
                        continue
                    valid_ports.append(comport)
        # For every valid port, ask for an echo (which also sends serial number etc.) and store the info
        validated_devices = []
        unvalidated_devices = []
        for comport in valid_ports:
            self.ser.port = comport.device
            try:
                self.ser.open()
                # print(f'open comport {comport.device}')
            except Exception as ex: # Poor practice? Catch only the exception that happens when you can open a port...?
                unvalidated_devices.append(comport.device)
                # print(ex)
                continue
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.serial_read_thread = threading.Thread(target=self.monitor_serial, daemon=True)
            self.serial_read_thread.start()
            # Ask the device to echo a byte, as the reply also contains device information sucha s version and serial number
            check_byte = 209
            check_byte = check_byte.to_bytes(1, 'little')
            self.write_command(transcode.encode_echo(check_byte))
            try:
                device_info = self.msgin_queues['echo'].get(block=True, timeout=1)
                if device_info['echoed_byte'] == check_byte:  #This is just to double check that the message is valid (the first check is the valid identifier and suffient lenght)
                    del(device_info['echoed_byte'])
                    device_info['comport'] = comport.device
                    validated_devices.append(device_info)
                else:
                    unvalidated_devices.append(comport.device)
            except queue.Empty as ex:
                unvalidated_devices.append(comport.device)
                pass
            self.disconnect()
        return {'validated_devices':validated_devices, 'unvalidated_devices':unvalidated_devices}

    def monitor_serial(self):
        while not self.close_readthread_event.is_set():
            # Try reading one byte. The first byte is always the message identifier
            try:
                byte_message_identifier = self.ser.read(1)
            except serial.serialutil.SerialException as ex:
                self.close_readthread_event.set()
                break
            # Normally the read will timeout and return empty, but if it returns someting try to read the reminder of the message
            if byte_message_identifier:
                timestamp = time.time()
                message_identifier, = struct.unpack('B', byte_message_identifier)
                # Only read more bytes if the identifier is valid
                if message_identifier not in transcode.msgin_decodeinfo.keys():
                    self.msgin_queues['bytes_dropped'].put({'message_identifier':message_identifier, 'message':None, 'timestamp':timestamp})
                else:
                    decodeinfo = transcode.msgin_decodeinfo[message_identifier]
                    message_length = decodeinfo['message_length'] - 1
                    try:
                        byte_message = self.ser.read(message_length)
                    except serial.serialutil.SerialException as ex:
                        self.close_readthread_event.set()
                        break   
                    # A random byte still a chance of being valid, so the read could timeout without reading a whole message worth of bytes
                    if len(byte_message) != message_length:
                        self.msgin_queues['bytes_dropped'].put({'message_identifier':message_identifier, 'message':None, 'timestamp':timestamp})
                    else:
                        # At this point, just decode the message and put it in the queue corresponding to its type.
                        decode_function = decodeinfo['decode_function']
                        message = decode_function(byte_message)
                        message['timestamp'] = timestamp
                        queue_name = decodeinfo['message_type']
                        self.msgin_queues[queue_name].put(message)

    def disconnect(self):
        self.close_readthread_event.set()
        self.serial_read_thread.join()
        self.close_readthread_event.clear()
        for q in self.msgin_queues.values():
            q.queue.clear()
        self.ser.close()

    def write_command(self, encoded_command):
        # not really sure if this is the correct place to put this. 
        # basically, what i need is that if the read_thread shits itself, the main thread will automatically safe close the connection, and then try to reconnect.
        if self.close_readthread_event.is_set():
            self.disconnect()
            self.connect(serial_number=self.serial_number_save)
        
        #I used to catch any Exceptions. Should I just let them happen?
        self.ser.write(encoded_command)
        # try:
        #     self.ser.write(encoded_command)
        # except Exception as ex:
        #     print(f'write command failed')
        #     print(ex)
        #     self.disconnect()

    ######################### Write command functions
    def write_echo(self, byte_to_echo):
        '''For more documentation, see ndpulsegen.transcode.encode_echo '''
        command = transcode.encode_echo(byte_to_echo)
        self.write_command(command)

    def write_device_options(self, final_address=None, run_mode=None, accept_hardware_trigger=None, trigger_out_length=None, trigger_out_delay=None, notify_on_main_trig_out=None, notify_when_run_finished=None, software_run_enable=None):
        '''For more documentation, see ndpulsegen.transcode.encode_device_options '''
        command = transcode.encode_device_options(final_address, run_mode, accept_hardware_trigger, trigger_out_length, trigger_out_delay, notify_on_main_trig_out, notify_when_run_finished, software_run_enable)
        self.write_command(command)

    def write_powerline_trigger_options(self, trigger_on_powerline=None, powerline_trigger_delay=None):
        '''For more documentation, see ndpulsegen.transcode.encode_powerline_trigger_options '''
        command = transcode.encode_powerline_trigger_options(trigger_on_powerline, powerline_trigger_delay)
        self.write_command(command)

    def write_action(self, trigger_now=False, disable_after_current_run=False, reset_run=False, request_state=False, request_powerline_state=False, request_state_extras=False):
        '''For more documentation, see ndpulsegen.transcode.encode_action '''
        command = transcode.encode_action(trigger_now, disable_after_current_run, reset_run, request_state, request_powerline_state, request_state_extras)
        self.write_command(command)

    def write_general_debug(self, message):
        '''For more documentation, see ndpulsegen.transcode.encode_general_debug '''
        command = transcode.encode_general_debug(message)
        self.write_command(command)

    def write_static_state(self, state):
        '''For more documentation, see ndpulsegen.transcode.encode_static_state '''
        command = transcode.encode_static_state(state)
        self.write_command(command)

    def write_instructions(self, instructions):
        '''For more documentation, see ndpulsegen.transcode.write_device_options 
        "instructions" are the encoded timing instructions that will be loaded into the pulse generator memeory.
        These instructions must be generated using the transcode.encode_instruction function. 
        This function accecpts encoded instructions in the following formats (where each individual instruction is always
        in bytes/bytearray): A single encoded instruction, multiple encoded instructions joined together in a single bytes/bytearray, 
        or a list, tuple, or array of single or multiple encoded instructions.'''
        if isinstance(instructions, (list, tuple, np.ndarray)):
            self.write_command(b''.join(instructions)) 
        else:
            self.write_command(instructions) 

    ######################### Some functions that will help in reading, waiting, doing stuff. I am not sure how future programs will interact with this
    def read_all_messages(self, timeout=0):
        if timeout != 0:
            sleeptime = min(0.1, timeout)
            messages = []
            t0 = time.time()
            while True:
                messages.extend(self.read_all_current_messages())
                if time.time() - t0 > timeout:
                    break
                time.sleep(sleeptime)
            return messages
        else:
            return self.read_all_current_messages()

    def read_all_current_messages(self):
        messages = []
        for q in self.msgin_queues.values():
            while not q.empty():
                messages.append(q.get())
        return messages

    def get_state(self, timeout=None):
        state_queue = self.msgin_queues['devicestate']
        #Empty the queue
        state_queue.queue.clear()
        #request the state
        self.write_action(request_state=True)
        # wait for the state to be sent
        try:
            return state_queue.get(timeout=1)
        except queue.Empty as ex:
            return None

    def get_powerline_state(self, timeout=None):
        state_queue = self.msgin_queues['powerlinestate']
        #Empty the queue
        state_queue.queue.clear()
        #request the state
        self.write_action(request_powerline_state=True)
        # wait for the state to be sent
        try:
            return state_queue.get(timeout=1)
        except queue.Empty as ex:
            return None

    def get_state_extras(self, timeout=None):
        state_queue = self.msgin_queues['devicestate_extras']
        #Empty the queue
        state_queue.queue.clear()
        #request the state
        self.write_action(request_state_extras=True)
        # wait for the state to be sent
        try:
            return state_queue.get(timeout=1)
        except queue.Empty as ex:
            return None

    def return_on_notification(self, finished=None, triggered=None, address=None, timeout=None):
        # if no criteria are specified, return on any notification received
        return_on_any = True if finished is triggered is address is None else False
        timeout_remaining = timeout
        t0 = time.time()
        notification_queue = self.msgin_queues['notification']
        while True:
            try:
                # wait for a notification. 
                notification = notification_queue.get(timeout=timeout_remaining)
                # check if notification satisfies any of the criteria set 
                if (notification['address_notify'] and notification['address'] == address) or (notification['trigger_notify'] == triggered) or (notification['finished_notify'] == finished) or return_on_any:
                    return notification
            except queue.Empty as ex:
                # Reached timeout limit.
                return None
            if timeout is not None:
                # If a notification was recieved that didn't match any of the specified criteria, calculate the remaining time until the requested timeout
                timeout_remaining = max(timeout - (time.time() - t0), 0.0)
