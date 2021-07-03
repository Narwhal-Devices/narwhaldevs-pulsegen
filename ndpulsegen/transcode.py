import numpy as np
import struct

#########################################################
# decode
def decode_internal_error(message):
    ''' Messagein identifier:  1 byte: 100
    Message format:                     BITS USED   FPGA INDEX.
    tags:               1 byte  [0]     2 bits      [0+:2]      unsigned int.
        invalid_identifier_received     1 bit       [0]
        timeout_waiting_for_full_msg    1 bit       [1]  
        received_message_not_forwarded  1 bit       [2]  
    error information:  1 byte  [1]     8 bits      [8+:8]     unsigned int.

    The 'error_info' represents the "device_index" for the received message, which basically says where the meassage should have headed in the FPGA.
    '''
    tags, =         struct.unpack('<Q', message[0:1] + bytes(7))
    error_info, =   struct.unpack('<Q', message[1:2] + bytes(7))
    invalid_identifier_received_tag =       (tags >> 0) & 0b1        
    timeout_waiting_for_msg_tag =           (tags >> 1) & 0b1     
    received_message_not_forwarded_tag =    (tags >> 2) & 0b1 
    invalid_identifier_received =       decode_lookup['invalid_identifier'][invalid_identifier_received_tag]
    timeout_waiting_for_msg =           decode_lookup['msg_receive_timeout'][timeout_waiting_for_msg_tag]
    received_message_not_forwarded =    decode_lookup['msg_not_forwarded'][received_message_not_forwarded_tag]
    return {'invalid_identifier_received':invalid_identifier_received, 'timeout_waiting_to_receive_message':timeout_waiting_for_msg, 'received_message_not_forwarded':received_message_not_forwarded, 'error_info':error_info}

def decode_easyprint(message):
    ''' Messagein identifier:  1 byte: 102
    Message format:                     BITS USED   FPGA INDEX.
    printed message:    8 bytes [0:3]   64 bits     [0+:64]     '''
    binary_representation = []
    for letter in message[::-1]:
        binary_representation.append('{:08b} '.format(letter))
    return ''.join(binary_representation)

def decode_devicestate(message):
    ''' Messagein identifier:  1 byte: 103
    Message format:                     BITS USED   FPGA INDEX.
    output state:       3 bytes [0:3]   24 bits     [0+:24]     unsigned int. LSB=output 0
    final ram address:  2 bytes [3:5]   16 bits     [24+:16]    unsigned int.
    trigger time:       7 bytes [5:12]  56 bits     [40+:56]    unsigned int.
    trigger length:     1 byte  [12]    8 bits      [96+:8]     unsigned int.
    current ram address:2 bytes [13:15] 16 bits     [104+:16]   unsigned int.
    tags:               2 byte  [15:17] 9 bits      [120+:8]    unsigned int.
        run mode                        1 bit       [120]   
        trigger mode                    2 bit       [121+:2] 
        notify on main trig             1 bit       [123]   
        clock source                    1 bit       [124]   
        running                         1 bit       [125]
        software run_enable             1 bit       [126]            
        hardware run_enable             1 bit       [127]
        notify on finished              1 bit       [128]
    
    '''
    state =                 np.unpackbits(np.array([message[0], message[1], message[2]], dtype=np.uint8), bitorder='little')
    final_ram_address, =    struct.unpack('<Q', message[3:5] + bytes(6))
    trigger_out_delay, =        struct.unpack('<Q', message[5:12] + bytes(1))
    trigger_length, =       struct.unpack('<Q', message[12:13] + bytes(7))
    current_ram_address, =  struct.unpack('<Q', message[13:15] + bytes(6))
    tags, =                 struct.unpack('<Q', message[15:17] + bytes(6))

    run_mode_tag =                  (tags >> 0) & 0b1            
    trigger_source_tag =            (tags >> 1) & 0b11              
    notify_on_main_trig_out_tag =   (tags >> 3) & 0b1    
    clock_source_tag =              (tags >> 4) & 0b1  
    running_tag =                   (tags >> 5) & 0b1  
    software_run_enable_tag =       (tags >> 6) & 0b1  
    hardware_run_enable_tag =       (tags >> 7) & 0b1
    notify_on_run_finished_tag =    (tags >> 8) & 0b1    
    run_mode =                  decode_lookup['run_mode'][run_mode_tag]
    trigger_source =            decode_lookup['trigger_source'][trigger_source_tag]
    notify_on_main_trig_out =   decode_lookup['notify_on_main_trig_out'][notify_on_main_trig_out_tag]
    clock_source =              decode_lookup['clock_source'][clock_source_tag]
    running =                   decode_lookup['running'][running_tag]
    software_run_enable =       decode_lookup['software_run_enable'][software_run_enable_tag]
    hardware_run_enable =       decode_lookup['hardware_run_enable'][hardware_run_enable_tag]
    notify_on_run_finished =    decode_lookup['notify_on_run_finished'][notify_on_run_finished_tag]
    return {'state:':state, 'final_ram_address':final_ram_address, 'trigger_out_delay':trigger_out_delay, 'run_mode':run_mode, 'trigger_source':trigger_source, 'notify_on_main_trig_out':notify_on_main_trig_out, 'notify_on_run_finished':notify_on_run_finished, 'trigger_length':trigger_length, 'clock_source':clock_source, 'running':running, 'software_run_enable':software_run_enable, 'hardware_run_enable':hardware_run_enable, 'current_address':current_ram_address}

def decode_powerlinestate(message):
    ''' Messagein identifier:  1 byte: 105
    Message format:                             BITS USED   FPGA INDEX.
    tags:                       1 byte  [0]     2 bits      [0+:2]    unsigned int.
        trig_on_powerline                       1 bit       [0]   
        powerline_locked                        1 bit       [1] 
    powerline_period:           3 bytes [1:4]   22 bits     [8+:22]   unsigned int.
    powerline_trigger_delay:    3 bytes [4:7]   22 bits     [32+:22]  unsigned int.
    '''
    tags, =                     struct.unpack('<Q', message[0:1] + bytes(7))
    powerline_period, =         struct.unpack('<Q', message[1:4] + bytes(5))
    powerline_trigger_delay, =  struct.unpack('<Q', message[4:7] + bytes(5))
    trig_on_powerline_tag = (tags >> 0) & 0b1
    powerline_locked_tag =  (tags >> 1) & 0b1
    trig_on_powerline = decode_lookup['trig_on_powerline'][trig_on_powerline_tag]
    powerline_locked =  decode_lookup['powerline_locked'][powerline_locked_tag]
    return {'trig_on_powerline':trig_on_powerline, 'powerline_locked':powerline_locked, 'powerline_period':powerline_period, 'powerline_trigger_delay':powerline_trigger_delay}

def decode_notification(message):
    ''' Messagein identifier:  1 byte: 104
    Message format:                             BITS USED   FPGA INDEX.
    current instruction address:2 bytes [0:2]   16 bits     [0+:16]   unsigned int.
    tags:                       1 byte  [2]     3 bits      [16+:3]   
        instriction notify tag                  1 bit       [16] 
        trigger notify tag                      1 bit       [17] 
        end of run notify tag                   1 bit       [18] 
    '''
    address_of_notification, =  struct.unpack('<Q', message[0:2] + bytes(6))
    tags, =                     struct.unpack('<Q', message[2:3] + bytes(7))
    address_notify_tag =    (tags >> 0) & 0b1
    trig_notify_tag =       (tags >> 1) & 0b1
    finished_notify_tag =   (tags >> 2) & 0b1
    address_notify =    decode_lookup['address_notify'][address_notify_tag]
    trig_notify =       decode_lookup['trig_notify'][trig_notify_tag]
    finished_notify =        decode_lookup['finished_notify'][finished_notify_tag]
    return {'address':address_of_notification, 'address_notify':address_notify, 'trigger_notify':trig_notify, 'finished_notify':finished_notify}

def decode_serialecho(message):
    ''' Messagein identifier:  1 byte: 101
    Message format:                     BITS USED   FPGA INDEX.
    echoed byte:        1 bytes [0:1]   8 bits      [0+:8]     
    device version:     7 bytes [1:8]   56 bits     [8+:56]    '''
    echoed_byte = message[0:1]
    device_version = message[1:8].decode()
    return {'echoed_byte':echoed_byte, 'device_version':device_version}

#########################################################
# encode
def encode_echo(byte_to_echo):
    ''' Messageout identifier:  1 byte: 150
    Message format:                             BITS USED   FPGA INDEX.
    byte_to_echo:               1 byte  [0:18]  8 bits     [0+:8]  
    '''    
    message_identifier = struct.pack('B', msgout_identifier['echo'])
    return message_identifier + byte_to_echo

def encode_powerline_trigger_options(trigger_on_powerline=None, powerline_trigger_delay=None):
    ''' Messageout identifier:  1 byte: 156
    Message format:                             BITS USED   FPGA INDEX.
    powerline_trigger_delay:    3 bytes [0:3]   22 bits     [0+:22]     unsigned int.
    tags:                       1 byte  [3]     3 bits      [24+:3]     unsigned int.
        update_powln_trig_dly_tag               1 bit       [24]
        wait_for_powerline                      2 bit       [25+:2]     [25]: powerline_wait_setting, [26]:update flag
    '''
    if powerline_trigger_delay is None:
        update_powerline_trigger_delay_tag = 0
        powerline_trigger_delay = 0
    else:
        update_powerline_trigger_delay_tag = 1

    trigger_on_powerline_tag =  encode_lookup['trigger_on_powerline'][trigger_on_powerline] << 1
    tags = update_powerline_trigger_delay_tag | trigger_on_powerline_tag
    message_identifier =        struct.pack('B', msgout_identifier['powerline_trigger_options'])
    powerline_trigger_delay =   struct.pack('<Q', powerline_trigger_delay)[:3]
    tags =                      struct.pack('<Q', tags)[:1]
    return message_identifier + powerline_trigger_delay + tags


def encode_device_options(final_ram_address=None, run_mode=None, trigger_source=None, trigger_out_delay=None, notify_on_main_trig_out=None, trigger_length=None, software_run_enable=None, notify_when_run_finished=None):
    ''' 
        Generates the command to change most the global settings, encoded in a format that is readable by the Pulse Gen FPGA design.
        All arguments are optional. All settings with arguments that are not 'None' are updated. All settings with argument 'None' are
        not updated. 

    Parameters
    ----------
    final_ram_address : int, optional
        `final_ram_address` ∈ [0, 8191].
        The address of the final instruction that is executed in a run. After this instruction has completed, the run will stop or restart,
        depending on the `run_mode` setting.
    run_mode : str, optional
        `run_mode` ∈ ('single', 'continuous')
        After completing of the instruction specified by `final_ram_address`, if `run_mode`='single' the device will immediately stop 
        counting down to the next instruction, and all channels will retain the output state of the final instruction. 
        If `run_mode`=='continuous', after the last cycle of the final instruction, the device will immediately execute the instruction
        at address 0, and the entire run will begin again.
    trigger_source : str, optional
        `trigger_source` ∈ ('software', 'hardware', 'either', 'single_hardware')
        Controls the accecpted source of input triggers that start or restart a run. If `trigger_source`='software', then all hardware input 
        trigger signals are ignored, and the input trigger can only be activated using a software trigger, which is generated with the 
        `encode_action' function with argument `trigger_now`=True. If `trigger_source`='hardware' the opposite is the case; all software 
        triggers are ignored, and only harware input trigger signals start or restart a run. If `trigger_source`='either' both software and 
        hardware input triggers are accepted. If `trigger_source`='single_hardware', then the device accecpts a single hardware trigger, 
        after which it automatically reverts to `trigger_source`='software'.
    trigger_out_delay : int, optional
        `trigger_out_delay` ∈ [0, 72057594037927935].
        Controls the delay between the first cycle of a run, and the start of the hardware output trigger pulse. This only controls the delay
        for the output trigger that is emittted at the start of a run, any output triggers that are emitted because an instruction contains 
        `hardware_trig_out`=True is emitted on the first cycle of execution of that instruction.
    notify_on_main_trig_out : bool, optional
        If True, when the main hardware output trigger pulse is sent, a notification will be sent to the host computer. The notification dictionary 
        will contain 'trigger_notify':True, and the 'address' field will be equal to the address of the instruction that is currently being
        executed. This setting only controls if a notification is sent on the output trigger that is emittted at the start of a run, not those 
        produced by instructions with argument 'notify_computer':True. If `run_mode`='continuous', a notification is sent each time the run 
        repeats.
    trigger_length: int, optional
        `trigger_length` ∈ [0, 255].
        Controls the duration of hardware output trigger pulse, for both the output trigger that is emittted at the start of a run, and for those
        emitterd beacuse an instruction contains `hardware_trig_out`=True. If `trigger_length`=0, not putput pulse is emitted.
    software_run_enable: bool, optional
        If False, and a run is in progress the timer in the run is immediately paused, and all channels maintain their current output the output.
        The timer immediately resumes when `software_run_enable`=True. If False, and a run is not in progress, the run will be prevented from 
        starting until `software_run_enable`=True. Triggers are ignored while `software_run_enable`=False.
    notify_when_run_finished: bool, optional
        If True, a notification will be sent to the host computer when the run has ended.  The notification dictionary will contain 
        'finished_notify':True, and the 'address' field will be equal to the address of the final instruction. If `run_mode`='continuous', only
        a single notification is sent after the completion of the last run (which is induced by sending a command generated with the `encode_action' 
        function with argument `disable_after_current_run`=True).

    Returns
    -------
    bytes
        The raw bytes of the command, ready to be uploaded to the Pulse Gen.

    Raises
    ------
    TypeError
        Arguments are checked for type to avoid undetermined behaviour of Pulse Gen.
    ValueError
        Arguments are checked to ensure they lie in a valid range to avoid undetermined behaviour of Pulse Gen.

    See Also
    --------
    encode_instruction : The function that encodes the timing instructions of the Pulse Gen.
    encode_action : The function that encodes commands that induce one off effects on the Pulse Gen.
    encode_device_options : The function that encodes the global settings of the Pulse Gen.
    encode_powerline_trigger_options : The function that encodes the global settings of the Pulse Gen which relate to powerline synchronisation.
    
    Notes
    -----
    Below is the bitwise layout of the encoded instructions. The FPGA INDEX corresponds to 
    the instruction bit index as written in Lucid HDL (hardware design language).
    Messageout identifier:  1 byte: 154
    Message format:                             BITS USED   FPGA INDEX.
    final_RAM_address:          2 bytes [0:2]   16 bits     [0+:16]     unsigned int.
    trigger_out_delay:          7 bytes [2:9]   56 bits     [16+:56]    unsigned int.
    trigger_length:             1 byte  [9]     8 bits      [72+:8]     unsigned int.
    
    tags:                       2 byte  [10:12] 14 bits     [80+:14]    unsigned int.
        run_mode                                2 bit       [80+:2]     [80]: run mode, [81]:update flag
        trigger_source                          3 bits      [82+:3]     [82+:2]: trig mode, [84]:update flag
        trigger_notification_enable             2 bit       [85+:2]     [85]: trig notif, [86]:update flag
        update_flag:final_RAM_address           1 bit       [87]
        update_flag:trigger_out_delay           1 bit       [88]
        update_flag:trigger_length              1 bit       [89]
        software_run_enable                     2 bit       [90+:2]     [90]: software_run_enable, [91]:update flag
        notify_when_run_finished                2 bit       [92+:2]     [92]: notify_when_run_finished, [93]:update flag
    '''
    run_mode_tag =                  encode_lookup['run_mode'][run_mode] << 0
    trigger_source_tag =            encode_lookup['trigger_source'][trigger_source] << 2
    notify_on_main_trig_out_tag =   encode_lookup['notify_on_trig'][notify_on_main_trig_out] << 5
    software_run_enable_tag =       encode_lookup['software_run_enable'][software_run_enable] << 10
    notify_when_run_finished_tag =  encode_lookup['notify_when_finished'][notify_when_run_finished] << 12

    if final_ram_address is None:   
        final_ram_address = 0
        update_final_ram_address_tag = 0
    else:
        update_final_ram_address_tag = 1 << 7
    if trigger_out_delay is None:        
        trigger_out_delay = 0
        update_trigger_out_delay_tag = 0
    else:
        update_trigger_out_delay_tag = 1 << 8
    if trigger_length is None:      
        trigger_length = 0
        update_trigger_length_tag = 0
    else:
        update_trigger_length_tag = 1 << 9

    tags = run_mode_tag | trigger_source_tag | notify_on_main_trig_out_tag | update_final_ram_address_tag | update_trigger_out_delay_tag | update_trigger_length_tag | software_run_enable_tag | notify_when_run_finished_tag
    message_identifier =    struct.pack('B', msgout_identifier['device_options'])
    final_ram_address =     struct.pack('<Q', final_ram_address)[:2]
    trigger_out_delay =     struct.pack('<Q', trigger_out_delay)[:7]
    trigger_length =        struct.pack('<Q', trigger_length)[:1]
    tags =                  struct.pack('<Q', tags)[:2]
    return message_identifier + final_ram_address + trigger_out_delay + trigger_length + tags

def encode_action(trigger_now=False, request_state=False, reset_output_coordinator=False, disable_after_current_run=False, request_powerline_state=False):
    ''' Messageout identifier:  1 byte: 152
    Message format:                             BITS USED   FPGA INDEX.
    tags:                       1 byte  [0]     5 bits      [0+:5]    
        trigger_now                             1 bit       [0] 
        request_state                           1 bit       [1]
        request_powerline_state                 1 bit       [2]
        disable_after_current_run               1 bit       [3]
        reset_output_coordinator                1 bit       [4] 
    '''
    trigger_now_tag =                   encode_lookup['trigger_now'][trigger_now] << 0
    request_state_tag =                 encode_lookup['request_state'][request_state] << 1
    request_powerline_state_tag =       encode_lookup['request_powerline_state'][request_powerline_state] << 2
    disable_after_current_run =         encode_lookup['disable_after_current_run'][disable_after_current_run] << 3
    reset_output_coordinator_tag =      encode_lookup['reset_output_coordinator'][reset_output_coordinator] << 4
    tags = trigger_now_tag | request_state_tag | reset_output_coordinator_tag | disable_after_current_run | request_powerline_state_tag
    message_identifier =    struct.pack('B', msgout_identifier['action_request'])
    tags =                  struct.pack('<Q', tags)[:1]
    return message_identifier + tags

def encode_general_debug(message):
    ''' Messageout identifier:  1 byte: 153
    Message format:                             BITS USED   FPGA INDEX.
    general_putpose_input:      8 bytes [0:8]   64 bits     [0+:64]     unsigned int.
    '''
    message_identifier =    struct.pack('B', msgout_identifier['general_input'])
    message =               struct.pack('<Q', message)[:8]
    return message_identifier + message

def encode_static_state(state):
    ''' Messageout identifier:  1 byte: 155
    Message format:                             BITS USED   FPGA INDEX.
    main_outputs_state:         3 bytes [0:3]   24 bits     [0+:24]     unsigned int.
    '''
    state = state_multiformat_to_int(state)
    message_identifier =    struct.pack('B', msgout_identifier['set_static_state'])
    state =                 struct.pack('<Q', state)[:3] 
    return message_identifier + state

def encode_instruction(address, duration, state, goto_address=0, goto_counter=0, stop_and_wait=False, hardware_trig_out=False, notify_computer=False, powerline_sync=False):
    """
    Generates a timing instruction encoded in a format that is readable by the Pulse Gen FPGA design.

    Parameters
    ----------
    address : int
        `address` ∈ [0, 8191].
        The address of this instruction in the Pulse Gen memory. Instructions are executed in sequential order
        unless a nonzero `goto_counter` is specified.
    duration : int
        `duration` ∈ [1, 281474976710655].
        The number of clock cycles (of 10 nanoseconds) that `state` is output before the next instruction is activated.
    state : int or list or tuple or numpy.ndarray
        The low/high output state for each of the 24 channels of the Pulse Gen for this instruction.
        If `state` is an int, the state of each channel is determined by the value of the binary digit at the corresponding 
        digit position. The least significant bit (rightmost bit) corresponds to channel 0. A digit value of 0 represents a low 
        state, and 1 is a high state.
        If `state` is a list, tuple, or array, then the index of the element corresponds to the channel of the Pulse Gen. Ie index
        0 corresponds to channel 0. The boolean value of each element determinses whether that channel is low or high, where False 
        corresponds to a low state, and True corresponds to a high state.
    goto_address : int, optional
        `goto_address` ∈ [0, 8191].
        Specifies the address of the next instruction that the Pulse Gen will execute after the current instruction has finished.
        This is used for creating 'loops' in the Pulse Gen's instruction order. `goto_address` is ignored if `goto_counter` = 0.
    goto_counter : int, optional
        `goto_counter` ∈ [0, 4294967295].
        Specifies the number of times the Pulse Dev will jump to the `goto_address` after the current instruction has finished.
        Each time the end of the current instruction is reached, a volitile copy of `goto_counter` is checked. If the value is nonzero, 
        the device jumps to `goto_address` and decrements the volitile copy by one. If the volitile copy of `goto_counter` is zero, then
        the device continues to the next sequential instruction after the current instruction, and replaces the volitile copy of `goto_counter`
        with a saved original copy. This "resets" the current instruction to the state in which it was origianlly uploaded.
    stop_and_wait : bool, optional
        If True, a run will pause after the last cycle of the current instruction. A subsequent trigger will restart the run, and the next
        instruction will immediately be executed.
    hardware_trig_out : bool, optional
        If True, when this instruction is first executed, a trigger pulse will also be output from the output trigger connector. This pulse
        is output on the first cycle of the current instruction, irrespective of the `trigger_out_delay` set in the global settings. The lenght of
        this trigger pulse is determined by the `trigger_length` set in the global settings.
    notify_computer : bool, optional
        If True, when this instruction is first executed, a notification will be sent to the host computer. The notification dictionary will
        contain 'address_notify':True, and the 'address' field will be equal to the address of this instruction. 
    powerline_sync : bool, optional
        If True, when a run is paused and this is the next instruction that would be executed, the run automatically restarts on the next
        recieved `powerline_trigger`. An instruction where `powerline_sync` is True usually follows an instrustion where `stop_and_wait`
        is True. This allows a particular section of a run to be be very accurately synchronised with the powerline phase, which may be 
        helpful for example if that part of the run corresponds to a measurement that is very sensitive to magnetic field. 
        The `powerline_trigger` respects the global setting `powerline_trigger_delay`. The global setting `trigger_on_powerline` is ignored. 

    Returns
    -------
    bytes
        The raw bytes of the instruction, ready to be uploaded to the Pulse Gen.

    Raises
    ------
    TypeError
        Arguments are checked for type to avoid undetermined behaviour of Pulse Gen.
    ValueError
        Arguments are checked to ensure they lie in a valid range to avoid undetermined behaviour of Pulse Gen.

    See Also
    --------
    encode_device_options : The function that encodes the global settings of the Pulse Gen.
    encode_powerline_trigger_options : The function that encodes the global settings of the Pulse Gen which relate to powerline synchronisation.
    
    Notes
    -----
    Below is the bitwise layout of the encoded instructions. The FPGA INDEX corresponds to 
    the instruction bit index as written in Lucid HDL (hardware design language).
    Messageout identifier:  1 byte: 151
    Message format:                             BITS USED   FPGA INDEX.
    instruction_address:        2 bytes [0:2]   16 bits     [0+:16]     unsigned int.
    main_outputs_state:         3 bytes [2:5]   24 bits     [16+:24]    unsigned int.
    instruction_duration:       6 bytes [5:11]  48 bits     [40+:48]    unsigned int.
    goto_address:               2 bytes [11:13] 16 bits     [88+:16]    unsigned int.
    goto_counter:               4 bytes [13:17] 32 bits     [104+:32]   unsigned int.
    tags:                       1 byte  [17]    4 bits      [136+:4]    unsigned int.
        stop_and_wait                           1 bit       [136]   
        hardware_trigger_out                    1 bits      [137] 
        notify_instruction_activated            1 bit       [138]
        powerline_sync                          1 bit       [139] 
    """
    # Type and value checking
    if not isinstance(address, int):
        err_msg = f'\'address\' must be an int, not a {type(address).__name__}'
        raise TypeError(err_msg)
    if address < 0 or address > 8191:
        err_msg = f'\'address\' out of range. Must be in must be range [0, 8191]'
        raise ValueError(err_msg)
    if not isinstance(duration, int):
        err_msg = f'\'duration\' must be an int, not a {type(duration).__name__}'
        raise TypeError(err_msg)
    if duration < 1 or duration > 281474976710655:
        err_msg = f'\'duration\' out of range. Must be in range [1, 281474976710655]'
        raise ValueError(err_msg)
    if not isinstance(goto_address, int):
        err_msg = f'\'goto_address\' must be an int, not a {type(goto_address).__name__}'
        raise TypeError(err_msg)
    if goto_address < 0 or goto_address > 8191:
        err_msg = f'\'goto_address\' out of range. Must be in must be range [0, 8191]'
        raise ValueError(err_msg)
    if not isinstance(goto_counter, int):
        err_msg = f'\'goto_counter\' must be an int, not a {type(goto_counter).__name__}'
        raise TypeError(err_msg)
    if goto_counter < 0 or goto_counter > 4294967295:
        err_msg = f'\'goto_counter\' out of range. Must be in range [0, 4294967295]'
        raise ValueError(err_msg)
    # Tag arguments are not explicitly validated. Errors are caught in the dictionary lookup
    state = state_multiformat_to_int(state)
    stop_and_wait_tag =     encode_lookup['stop_and_wait'][stop_and_wait] << 0
    hard_trig_out_tag =     encode_lookup['trig_out_on_instruction'][hardware_trig_out] << 1
    notify_computer_tag =   encode_lookup['notify_on_instruction'][notify_computer] << 2
    powerline_sync_tag =    encode_lookup['powerline_sync'][powerline_sync] << 3
    tags = stop_and_wait_tag | hard_trig_out_tag | notify_computer_tag | powerline_sync_tag
    message_identifier =    struct.pack('B', msgout_identifier['load_ram'])
    address =               struct.pack('<Q', address)[:2]
    state =                 struct.pack('<Q', state)[:3]
    duration =              struct.pack('<Q', duration)[:6]
    goto_address =          struct.pack('<Q', goto_address)[:2]
    goto_counter =          struct.pack('<Q', goto_counter)[:4]
    tags =                  struct.pack('<Q', tags)[:1]
    return message_identifier + address + state + duration + goto_address + goto_counter + tags

def state_multiformat_to_int(state):
    if isinstance(state, int):
        if state < 0 or state > 16777215:
            err_msg = f'\'state\' out of range. If state is int, it must be in range [{bin(0)}, {bin(16777215)}]'
            raise ValueError(err_msg)
    elif isinstance(state, (list, tuple, np.ndarray)):
        if len(state) > 24:
            err_msg = f'\'state\' too long. If state is list, tuple or numpy.ndarray, it must have length <= 24'
            raise ValueError(err_msg)
        state_int = 0
        for bit_idx, value in enumerate(state):
            state_int += bool(value) << bit_idx
        state = state_int
    else:
        err_msg = f'\'state\' must be an int, list, tuple or numpy.ndarray, not a {type(state).__name__}'
        raise TypeError(err_msg)
    return state

#########################################################
# constants
msgin_decodeinfo = {
    100:{'message_length':3,    'decode_function':decode_internal_error,    'message_type':'error'},
    101:{'message_length':9,    'decode_function':decode_serialecho,        'message_type':'echo'},
    102:{'message_length':9,    'decode_function':decode_easyprint,         'message_type':'print'},
    103:{'message_length':18,   'decode_function':decode_devicestate,       'message_type':'devicestate'},
    104:{'message_length':4,    'decode_function':decode_notification,      'message_type':'notification'},
    105:{'message_length':8,    'decode_function':decode_powerlinestate,    'message_type':'powerlinestate'}
    }

# This is a "reverse lookup" dictionaty for the msgin_decodeinfo. I don't think I use this much/at all. It can probably be deleted.
msgin_identifier = {value['message_type']:key for key, value in msgin_decodeinfo.items()}

decode_lookup = {
    'clock_source':{1:'internal', 0:'external'},
    'running':{1:True, 0:False},
    'software_run_enable':{1:True, 0:False},
    'hardware_run_enable':{1:True, 0:False},
    'noitfy_on_instruction':{1:True, 0:False},
    'notify_on_main_trig_out':{1:True, 0:False},
    'notify_on_run_finished':{1:True, 0:False},
    'run_mode':{0:'single', 1:'continuous'},
    'trigger_source':{0:'software', 1:'hardware', 2:'either', 3:'single_hardware'},
    'trig_on_powerline':{1:True, 0:False},
    'powerline_locked':{1:True, 0:False},
    'address_notify':{1:True, 0:False},
    'trig_notify':{1:True, 0:False},
    'finished_notify':{1:True, 0:False},
    'invalid_identifier':{1:True, 0:False},
    'msg_not_forwarded':{1:True, 0:False},
    'msg_receive_timeout':{1:True, 0:False}
    }

msgout_identifier = {
    'echo':150,
    'load_ram':151,
    'action_request':152,
    'general_input':153,
    'device_options':154,
    'set_static_state':155,
    'powerline_trigger_options':156
    }

encode_lookup = {
    'software_run_enable':{True:0b11, False:0b10, None:0b00}, 
    'trigger_now':{True:1, False:0},
    'request_state':{True:1, False:0},
    'request_powerline_state':{True:1, False:0},
    'reset_output_coordinator':{True:1, False:0},
    'disable_after_current_run':{True:1, False:0},
    'notify_when_finished':{True:0b11, False:0b10, None:0b00},
    'run_mode':{'single':0b10, 'continuous':0b11, None:0b00},
    'trigger_source':{'software':0b100, 'hardware':0b101, 'either':0b110, 'single_hardware':0b111, None:0b000},
    'notify_on_trig':{True:0b11, False:0b10, None:0b00},
    'trigger_on_powerline':{True:0b11, False:0b10, None:0b00},
    'stop_and_wait':{True:1, False:0}, 
    'notify_on_instruction':{True:1, False:0},  
    'trig_out_on_instruction':{True:1, False:0},
    'powerline_sync':{True:1, False:0}
    }

