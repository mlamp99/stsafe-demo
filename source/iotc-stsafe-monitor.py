import json
import os
import serial
import time
import glob
import re
import subprocess
import traceback
import logging
import signal
import sys
import threading  # Added for threading
from threading import Lock
from avnet.iotconnect.sdk.lite import Client, DeviceConfig, Callbacks, DeviceConfigError
from avnet.iotconnect.sdk.lite import __version__ as SDK_VERSION
from avnet.iotconnect.sdk.sdklib.mqtt import C2dCommand, C2dAck

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG to capture all levels of logs
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("device_logs.log"),  # Log to a file
        logging.StreamHandler()  # Also log to the console
    ]
)

# Signal handler for graceful shutdown
def signal_handler(sig, frame):
    logging.info("Gracefully shutting down...")
    for dev, ser in serial_objects.items():
        if ser.is_open:
            ser.close()
            logging.info(f"Serial connection for {dev} closed.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global variables
serial_objects = {}
device_data = {}
device_state = {}
c = None
serial_lock = Lock()

DEVICES = {
    "device1": {
        "port": "/dev/stlinkv3_0",
        "json_file": "/var/www/usbdata/usb_data_device1.json",
        "usb_port_number": 1
    },
    "device2": {
        "port": "/dev/stlinkv3_2",
        "json_file": "/var/www/usbdata/usb_data_device2.json",
        "usb_port_number": 2
    }
}

ansi_escape = re.compile(r'(?:\x1B[@-Z\\-_]|\x1B\[0?K|\x1B\[[0-9;]*[A-Za-z])')

CATEGORIES = {
    "Device Start and Initialization": [
        "IOTC RUNNING",
        "STARTING APP VERSION"
    ],
    "Network and Discovery Attempts": [
        "IOTC: Performing discovery",
        "CA Certificate: CN=Go"
    ],
    "TLS Handshake and Requests": [
        "Sending HTTPS GET request to",
        "TLS handshake successful"
    ],
    "Device Identity Retrieval": [
        "Received HTTP response from awspocdi.iotconnect"
    ],
    "Successful Connection": [
        "Subscribed to topic iot"
    ],
    "Connection Failure (Key Mismatch)": [ 
        "Public-Private keypair does",
        "Failed to configure mbedtls transport.",
        "Terminating MqttAgentTask.",
        "Network connection failed.",
        "MQTT connection refused",
        "MQTT connection timeout"
    ]
}

# Define EXPECTED_KEYS
EXPECTED_KEYS = ["thing_name", "mqtt_endpoint", "platform", "cpid", "env"]

def clean_log_message(message):
    """
    Cleans up log messages by removing log levels, timestamps, file references, and prefixes.
    Ensures the input is a valid string before cleaning.
    """
    if not message or not isinstance(message, str):
        return ""  # Return an empty string for invalid input

    # Remove leading log levels like <INF>, <SYS>, etc.
    message = re.sub(r"^<.*?>\s*", "", message)

    # Remove timestamps (e.g., 8692)
    message = re.sub(r"^\d+\s*\[\w+\]\s*", "", message)

    # Remove file references (e.g., (iotconnect.c:211))
    message = re.sub(r"\s*\(.*?\)\s*$", "", message)

    # Remove specific prefixes like [iotconnect] or device labels like "device1:"
    message = re.sub(r"^[\w\d]+:\s*", "", message)

    return message.strip()

def categorize_line(line):
    # Check for device credentials (public key/certificate)
    if "BEGIN PUBLIC KEY" in line or "END PUBLIC KEY" in line:
        logging.debug(f"Line categorized as Device Credentials: {line}")
        return "Device Credentials"
    if "BEGIN CERTIFICATE" in line or "END CERTIFICATE" in line:
        logging.debug(f"Line categorized as Device Credentials: {line}")
        return "Device Credentials"
    
    # Check if line starts with an expected key followed by '='
    for key in EXPECTED_KEYS:
        if line.startswith(f'{key}=') or line.startswith(f'{key}="'):
            logging.debug(f"Line categorized as Device Credentials: {line}")
            return "Device Credentials"

    # Existing categories
    for category, keywords in CATEGORIES.items():
        if any(kw in line for kw in keywords):
            logging.debug(f"Line categorized under {category}: {line}")
            return category

    logging.debug(f"Line categorized as Uncategorized: {line}")
    return "Uncategorized"  # Assign to "Uncategorized" if no match

def empty_categories():
    return {
        "Device Start and Initialization": [],
        "Network and Discovery Attempts": [],
        "TLS Handshake and Requests": [],
        "Device Identity Retrieval": [],
        "Successful Connection": [],
        "Connection Failure (Key Mismatch)": [],
        "Device Credentials": {},
#        "Device Credentials": {
#            "thing_name": "",
#            "mqtt_endpoint": "",
#            "platform": "",
#            "cpid": "",
#            "env": "",
#            "Device Public Key": "",
#            "Device Certificate": ""
#        },
        "Uncategorized": []
    }

def reinitialize_device(dev, partial_cache, missed_logs_counter):
    """
    Safely reinitializes the serial connection for a device.
    Flushes old buffers, clears partial cache, and resets the device for clean reads.
    """
    if dev in serial_objects:
        old_ser = serial_objects.pop(dev)
        if is_valid_serial(old_ser):
            try:
                logging.info(f"Closing old connection for {dev}...")
                old_ser.reset_input_buffer()
                old_ser.reset_output_buffer()
                old_ser.close()
                logging.info(f"Old connection for {dev} closed successfully.")
            except Exception as e:
                logging.error(f"Error closing serial port for {dev}: {e}")

    # Attempt reinitialization up to 5 times
    for attempt in range(1, 6):
        try:
            logging.info(f"Initializing new connection for {dev} (Attempt {attempt}/5)...")
            new_ser = initialize_serial(DEVICES[dev]["port"])
            if new_ser:
                new_ser.reset_input_buffer()
                new_ser.reset_output_buffer()
                time.sleep(2)  # Allow device to stabilize
                serial_objects[dev] = new_ser
                partial_cache[dev] = ""  # Clear partial cache for this device
                missed_logs_counter[dev] = 0  # Reset missed logs counter
                logging.info(f"{dev} reinitialized successfully.")
                return
        except Exception as e:
            logging.error(f"Error initializing serial port for {dev}: {e}")
    logging.error(f"Failed to reinitialize {dev} after 5 attempts. Marking as unavailable.")

def initialize_serial(port):
    try:
        ser = serial.Serial(
            port=port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,  # Longer timeout for stability
            inter_byte_timeout=0.5
        )
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        logging.info(f"Serial port {port} initialized successfully.")
        return ser
    except serial.SerialException as e:
        logging.error(f"SerialException on {port}: {e}")
        return None

def read_serial_data(ser, partial_line_cache):
    """
    Reads logs from the serial port robustly.
    Handles incomplete lines using a cache and cleans up the output.
    """
    lines = []
    try:
        with serial_lock:
            buffer = bytearray()
            while ser.in_waiting > 0:
                chunk = ser.read(1024)
                buffer.extend(chunk)
                time.sleep(0.01)

            if buffer:
                data_str = buffer.decode('utf-8', errors='ignore')

                # Append cached partial line
                if partial_line_cache:
                    data_str = partial_line_cache + data_str
                    partial_line_cache = ""

                raw_lines = data_str.splitlines()

                # Handle incomplete last line
                if not data_str.endswith("\n"):
                    partial_line_cache = raw_lines.pop() if raw_lines else ""

                for line in raw_lines:
                    line = ansi_escape.sub('', line.strip())
                    if line:
                        logging.debug(f"Raw line captured: {line}")
                        lines.append(clean_log_message(line))

            logging.debug(f"Read {len(lines)} lines. Cache: {len(partial_line_cache)} chars")
            return lines, partial_line_cache
    except Exception as e:
        logging.error(f"Error reading serial data: {e}")
        traceback.print_exc()
        return lines, partial_line_cache

def run_device_command(device, command):
    """
    Sends a command to a device via its existing serial connection without reading responses.
    """
    try:
        if device not in serial_objects:
            logging.error(f"Serial connection for {device} not found.")
            return False

        ser = serial_objects[device]
        logging.info(f"Sending command '{command}' to {device} on port {ser.port}")

        with serial_lock:
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # Send the command
            ser.write(f"\r\n\r\n\r\n".encode())
            time.sleep(0.1)
            ser.write(f"{command}\r\n".encode())
            ser.flush()
            time.sleep(0.5)  # Allow time for the device to process the command

        return True

    except serial.SerialException as e:
        logging.error(f"SerialException on {ser.port}: {e}")
    except Exception as e:
        logging.error(f"Error sending command '{command}' to {device}: {e}")
        traceback.print_exc()
    return False

def run_device_credentials(device, device_state):
    time.sleep(2)
    logging.info(f"Running credentials fetch for {device}")
    
    # Run 'conf get' command
    device_state[device]["current_command"] = "conf_get"
    if not run_device_command(device, "conf get"):
        logging.error(f"Failed to run 'conf get' on {device}")
    time.sleep(3)  # Increased sleep to allow 'conf get' to be processed

    # Run 'pki export key' command
    device_state[device]["current_command"] = "pki_key"
    if not run_device_command(device, "pki export key"):
        logging.error(f"Failed to run 'pki export key' on {device}")
    time.sleep(3)  # Increased sleep

    # Run 'pki export cert' command
    device_state[device]["current_command"] = "pki_cert"
    if not run_device_command(device, "pki export cert"):
        logging.error(f"Failed to run 'pki export cert' on {device}")
    time.sleep(3)  # Increased sleep

def get_message_type(line):
    if "<ERR>" in line:
        return "ERR"
    elif "<SYS>" in line:
        return "SYS"
    else:
        return "INF"

def control_usb_port(port, action):
    if action not in ["ON", "OFF"]:
        raise ValueError("Invalid action. Must be 'ON' or 'OFF'.")
    try:
        command = ["cusbi", "/S:ttyUSB0", f"{1 if action == 'ON' else 0}:{port}"]
        subprocess.run(command, capture_output=True, text=True, check=True)
        logging.info(f"Port {port} {action} executed.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to execute command for port {port}: {e.stderr.strip()}")

def clear_logs_for_device(device):
    try:
        with open(DEVICES[device]["json_file"], "w") as f:
            json.dump(empty_categories(), f, indent=4)
        logging.info(f"Logs cleared for {device}.")
    except Exception as e:
        logging.error(f"Error clearing logs for {device}: {e}")

def clear_logs():
    empty_log = empty_categories()

    for device in DEVICES:
        try:
            with open(DEVICES[device]["json_file"], "w") as f:
                json.dump(empty_log, f, indent=4)
            logging.info(f"Logs cleared for {device}.")
        except Exception as e:
            logging.error(f"Error clearing logs for {device}: {e}")

def determine_state_from_logs(log_file):
    try:
        with open(log_file, "r") as f:
            logs = json.load(f)

        has_success = False
        has_error = False
        for category, entries in logs.items():
            if isinstance(entries, list):
                for entry in entries:
                    msg = entry.get("message", "")
                    if "Subscribed to topic iot" in msg:
                        has_success = True
                    if "Public-Private keypair does " in msg:
                        has_error = True
            elif isinstance(entries, dict):
                # Optionally handle dict entries if needed
                pass

        if has_success:
            return "SUCCESS"
        if has_error:
            return "FAILURE"
        return "DISCOVERY"

    except json.JSONDecodeError:
        return "OFF"
    except FileNotFoundError:
        return "OFF"
    except Exception as e:
        logging.error(f"Unexpected error while reading logs: {e}")
        return "OFF"

def get_device_state(dev):
    if dev not in serial_objects:
        return "OFF"
    return determine_state_from_logs(DEVICES[dev]["json_file"])

def is_valid_serial(ser):
    try:
        return ser and ser.is_open and ser.fileno() >= 0
    except Exception as e:
        logging.error(f"Error checking serial port: {e}")
        return False

def on_command(msg: C2dCommand):
    logging.info(f"Received command: {msg.command_name} {msg.command_args} {msg.ack_id}")
    global c, device_state

    try:
        if msg.command_name == "Device1Credentials":
            run_device_credentials("device1", device_state)
            c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device1 credentials fetched and logged.")

        elif msg.command_name == "Device2Credentials":
            run_device_credentials("device2", device_state)
            c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device2 credentials fetched and logged.")

        elif msg.command_name == "UsbDevice1Control":
            if len(msg.command_args) == 1:
                action = msg.command_args[0].upper()
                if action == "START":
                    control_usb_port(DEVICES["device1"]["usb_port_number"], "ON")
                    c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device1 port ON.")
                elif action == "STOP":
                    control_usb_port(DEVICES["device1"]["usb_port_number"], "OFF")
                    # Clear logs for device1
                    clear_logs_for_device("device1")
                    c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device1 port OFF and logs cleared.")
                else:
                    c.send_command_ack(msg, C2dAck.CMD_FAILED, "Invalid arg for UsbDevice1Control. Use START or STOP.")
            else:
                c.send_command_ack(msg, C2dAck.CMD_FAILED, "Expected 1 arg: START or STOP")

        elif msg.command_name == "UsbDevice2Control":
            if len(msg.command_args) == 1:
                action = msg.command_args[0].upper()
                if action == "START":
                    control_usb_port(DEVICES["device2"]["usb_port_number"], "ON")
                    c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device2 port ON.")
                elif action == "STOP":
                    control_usb_port(DEVICES["device2"]["usb_port_number"], "OFF")
                    # Clear logs for device2
                    clear_logs_for_device("device2")
                    c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Device2 port OFF and logs cleared.")
                else:
                    c.send_command_ack(msg, C2dAck.CMD_FAILED, "Invalid arg for UsbDevice2Control. Use START or STOP.")
            else:
                c.send_command_ack(msg, C2dAck.CMD_FAILED, "Expected 1 arg: START or STOP")
        
        # *** Start of New SequenceControl Command Handling ***
        elif msg.command_name == "SequenceControl":
            if len(msg.command_args) == 1:
                action = msg.command_args[0].lower()
                if action == "start":
                    logging.info("Initiating Start Sequence")
                    # Start the sequence in a new thread
                    threading.Thread(target=handle_sequence_start, args=(msg,), daemon=True).start()
                elif action == "stop":
                    logging.info("Initiating Stop Sequence")
                    # Stop the sequence in a new thread
                    threading.Thread(target=handle_sequence_stop, args=(msg,), daemon=True).start()
                else:
                    c.send_command_ack(msg, C2dAck.CMD_FAILED, "Invalid arg for SequenceControl. Use START or STOP.")
            else:
                c.send_command_ack(msg, C2dAck.CMD_FAILED, "Expected 1 arg: START or STOP")
        # *** End of New SequenceControl Command Handling ***
        
        else:
            logging.warning(f"Unknown command: {msg.command_name}")
            c.send_command_ack(msg, C2dAck.CMD_FAILED, "Command not implemented.")

    except Exception as e:
        logging.error(f"Error processing command {msg.command_name}: {e}")
        traceback.print_exc()
        c.send_command_ack(msg, C2dAck.CMD_FAILED, f"Exception while processing command: {str(e)}")

# *** Start of New Callback Functions ***
def handle_sequence_start(msg):
    try:
        logging.info("Starting sequence: Turning on Device1")
        # Turn on Device1
        control_usb_port(DEVICES["device1"]["usb_port_number"], "ON")
        logging.info("Device1 port turned ON. Waiting for Device1 to initialize and reach state...")

        # Wait for Device1 to reach SUCCESS or FAILURE
        timeout = 120  # seconds
        poll_interval = 5  # seconds
        start_time = time.time()
        while True:
            state = get_device_state("device1")
            logging.debug(f"Device1 state: {state}")
            if state in ["SUCCESS", "FAILURE"]:
                logging.info(f"Device1 reached state: {state}")
                break
            if time.time() - start_time > timeout:
                logging.error("Timeout waiting for Device1 state.")
                c.send_command_ack(msg, C2dAck.CMD_FAILED, "Timeout waiting for Device1 state.")
                return
            time.sleep(poll_interval)
        
        # Run Device1Credentials
        logging.info("Running Device1Credentials")
        run_device_credentials("device1", device_state)
        logging.info("Device1Credentials executed.")

        # Turn on Device2
        logging.info("Starting sequence: Turning on Device2")
        control_usb_port(DEVICES["device2"]["usb_port_number"], "ON")
        logging.info("Device2 port turned ON. Waiting for Device2 to initialize and reach state...")

        # Wait for Device2 to reach SUCCESS or FAILURE
        start_time = time.time()
        while True:
            state = get_device_state("device2")
            logging.debug(f"Device2 state: {state}")
            if state in ["SUCCESS", "FAILURE"]:
                logging.info(f"Device2 reached state: {state}")
                break
            if time.time() - start_time > timeout:
                logging.error("Timeout waiting for Device2 state.")
                c.send_command_ack(msg, C2dAck.CMD_FAILED, "Timeout waiting for Device2 state.")
                return
            time.sleep(poll_interval)
        
        # Run Device2Credentials
        logging.info("Running Device2Credentials")
        run_device_credentials("device2", device_state)
        logging.info("Device2Credentials executed.")

        # Send success acknowledgment
        c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Sequence completed successfully.")

    except Exception as e:
        logging.error(f"Error in sequence start: {e}")
        traceback.print_exc()
        c.send_command_ack(msg, C2dAck.CMD_FAILED, f"Exception during sequence start: {str(e)}")

def handle_sequence_stop(msg):
    try:
        logging.info("Stopping sequence: Turning off Device1 and Device2")
        # Turn off Device1
        control_usb_port(DEVICES["device1"]["usb_port_number"], "OFF")
        logging.info("Device1 port turned OFF.")
        
        # Turn off Device2
        control_usb_port(DEVICES["device2"]["usb_port_number"], "OFF")
        logging.info("Device2 port turned OFF.")
        
        # Clear logs for both devices
        clear_logs_for_device("device1")
        clear_logs_for_device("device2")
        
        # Send success acknowledgment
        c.send_command_ack(msg, C2dAck.CMD_SUCCESS_WITH_ACK, "Sequence stopped: Both devices turned off and logs cleared.")

    except Exception as e:
        logging.error(f"Error in sequence stop: {e}")
        traceback.print_exc()
        c.send_command_ack(msg, C2dAck.CMD_FAILED, f"Exception during sequence stop: {str(e)}")
# *** End of New Callback Functions ***

def initialize_device_data_and_state():
    global device_data, device_state
    for dev, info in DEVICES.items():
        device_data[dev] = empty_categories()
        logging.info(f"Initializing JSON file for {dev} at {info['json_file']}...")
        try:
            with open(info["json_file"], "w") as f:
                json.dump(device_data[dev], f, indent=4)
            logging.info(f"JSON file for {dev} initialized.")
        except Exception as e:
            logging.error(f"Error initializing JSON file for {dev}: {e}")

    for dev in DEVICES.keys():
        device_state[dev] = {
            "current_command": None,
            "key_lines": [],
            "cert_lines": []
        }
    logging.info("Device data and state initialized.")

def initialize_iotconnect_client():
    global c
    try:
        logging.info("Initializing IoTConnect client...")
        device_config = DeviceConfig.from_iotc_device_config_json_file(
            device_config_json_path="iotcDeviceConfig.json",
            device_cert_path="device-cert.pem",
            device_pkey_path="device-pkey.pem"
        )
        c = Client(
            config=device_config,
            callbacks=Callbacks(
                command_cb=on_command
            )
        )
        logging.info("Connecting to IoTConnect...")
        c.connect()
        if not c.is_connected():
            logging.error("Unable to connect to IoTConnect.")
            return False
        logging.info("Connected to IoTConnect.")
        return True
    except DeviceConfigError as dce:
        logging.error(f"Device configuration error: {dce}")
        traceback.print_exc()
        return False
    except Exception as e:
        logging.error(f"Unexpected error during IoTConnect initialization: {e}")
        traceback.print_exc()
        return False

def main_loop():
    partial_cache = {dev: "" for dev in DEVICES.keys()}
    missed_logs_counter = {dev: 0 for dev in DEVICES.keys()}

    while True:
        try:
            # Check available ports
            available_ports = glob.glob("/dev/stlinkv3_*")
            logging.debug(f"Available ports: {available_ports}")
            for dev, info in DEVICES.items():
                if dev not in serial_objects and info["port"] in available_ports:
                    # Device port is available, try to initialize if USB power is ON
                    reinitialize_device(dev, partial_cache, missed_logs_counter)
                    missed_logs_counter[dev] = 0

            # Read logs from available devices
            for dev, ser in list(serial_objects.items()):
                if not is_valid_serial(ser):
                    logging.warning(f"Serial port for {dev} is no longer valid. Removing...")
                    serial_objects.pop(dev, None)
                    continue
                elif ser and ser.is_open:
                    logging.debug(f"Reading data from {dev}...")
                    try:
                        missed_logs_counter[dev] = missed_logs_counter.get(dev, 0)

                        # Attempt to read data
                        lines, partial_cache[dev] = read_serial_data(ser, partial_cache[dev])

                        if not lines:
                            missed_logs_counter[dev] += 1
                            logging.warning(f"No logs received from {dev}. Missed count: {missed_logs_counter[dev]}")
                        else:
                            missed_logs_counter[dev] = 0
                            for line in lines:
                                try:
                                    if not line:
                                        continue

                                    cleaned_line = clean_log_message(line)
                                    if not cleaned_line:
                                        continue

                                    # Debug: Print current_command
                                    current_cmd = device_state[dev]["current_command"]
                                    logging.debug(f"{dev}: Current Command: {current_cmd}")

                                    logging.debug(f"{dev}: {cleaned_line}")
                                    category = categorize_line(cleaned_line) or "Uncategorized"
                                    entry = {"type": get_message_type(cleaned_line), "message": cleaned_line}

                                    if category == "Device Credentials":
                                        # Handle key-value pairs
                                        for key in EXPECTED_KEYS:
                                            if cleaned_line.startswith(f'{key}=') or cleaned_line.startswith(f'{key}="'):
                                                key_part, val_part = cleaned_line.split('=', 1)
                                                key = key_part.strip().lower()
                                                val = val_part.strip('"').strip()
                                                device_data[dev][category][key] = val
                                                logging.debug(f"{dev}: Stored {key} = {val} in Device Credentials")
                                                break  # Exit after handling the key-value pair

                                        # Handle PUBLIC KEY lines
                                        if "BEGIN PUBLIC KEY" in cleaned_line:
                                            device_state[dev]["key_lines"] = [cleaned_line]
                                            logging.debug(f"{dev}: Started capturing Device Public Key")
                                        elif "END PUBLIC KEY" in cleaned_line and device_state[dev]["key_lines"]:
                                            device_state[dev]["key_lines"].append(cleaned_line)
                                            full_key = "\n".join(device_state[dev]["key_lines"])
                                            device_data[dev][category]["Device Public Key"] = full_key
                                            logging.debug(f"{dev}: Stored Device Public Key")
                                            device_state[dev]["key_lines"] = []
                                        elif device_state[dev]["key_lines"]:
                                            # Intermediate PUBLIC KEY lines
                                            device_state[dev]["key_lines"].append(cleaned_line)
                                            logging.debug(f"{dev}: Appended to Device Public Key")

                                        # Handle CERTIFICATE lines
                                        if "BEGIN CERTIFICATE" in cleaned_line:
                                            device_state[dev]["cert_lines"] = [cleaned_line]
                                            logging.debug(f"{dev}: Started capturing Device Certificate")
                                        elif "END CERTIFICATE" in cleaned_line and device_state[dev]["cert_lines"]:
                                            device_state[dev]["cert_lines"].append(cleaned_line)
                                            full_cert = "\n".join(device_state[dev]["cert_lines"])
                                            device_data[dev][category]["Device Certificate"] = full_cert
                                            logging.debug(f"{dev}: Stored Device Certificate")
                                            device_state[dev]["cert_lines"] = []
                                        elif device_state[dev]["cert_lines"]:
                                            # Intermediate CERTIFICATE lines
                                            device_state[dev]["cert_lines"].append(cleaned_line)
                                            logging.debug(f"{dev}: Appended to Device Certificate")

                                    elif category == "Uncategorized":
                                        # Check if currently capturing key or cert
                                        if device_state[dev]["key_lines"]:
                                            device_state[dev]["key_lines"].append(cleaned_line)
                                            logging.debug(f"{dev}: Appended to Device Public Key (Uncategorized)")
                                        elif device_state[dev]["cert_lines"]:
                                            device_state[dev]["cert_lines"].append(cleaned_line)
                                            logging.debug(f"{dev}: Appended to Device Certificate (Uncategorized)")
                                        else:
                                            # Regular Uncategorized processing
                                            if category not in device_data[dev]:
                                                device_data[dev][category] = []
                                            if not any(existing_entry == entry for existing_entry in device_data[dev][category]):
                                                device_data[dev][category].append(entry)
                                                logging.debug(f"{dev}: Appended to Uncategorized")

                                    else:
                                        # Handle regular categories as lists
                                        if category not in device_data[dev]:
                                            device_data[dev][category] = []
                                        if not any(existing_entry == entry for existing_entry in device_data[dev][category]):
                                            device_data[dev][category].append(entry)
                                            logging.debug(f"{dev}: Appended to {category}")

                                    # Update JSON log file
                                    json_file = DEVICES[dev]["json_file"]
                                    with open(json_file, "w") as f:
                                        json.dump(device_data[dev], f, indent=4)
                                    logging.info(f"Logs updated for {dev}: {json_file}")

                                except Exception as e:
                                    logging.error(f"Error processing line for {dev}: {line}. Error: {e}")
                                    traceback.print_exc()

                    except Exception as e:
                        logging.error(f"Error reading data from {dev}: {e}")
                        traceback.print_exc()

            # Detect and handle devices with excessive missed logs
            for dev, missed_count in missed_logs_counter.items():
                if missed_count > 10:
                    logging.warning(f"Device {dev} missed logs for {missed_count} cycles. Reinitializing...")
                    reinitialize_device(dev, partial_cache, missed_logs_counter)
                    missed_logs_counter[dev] = 0

            # Send telemetry data
            try:
                device1_state = get_device_state("device1")
                device2_state = get_device_state("device2")
                if c and c.is_connected():
                    telemetry = {
                        "sdk_version": SDK_VERSION,
                        "device1_state": device1_state,
                        "device2_state": device2_state,
                    }
                    c.send_telemetry(telemetry)
                    logging.info(f"Telemetry sent: {telemetry}")
            except Exception as e:
                logging.error(f"Error sending telemetry: {e}")
                traceback.print_exc()

        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            traceback.print_exc()

        time.sleep(1)

def main():
    logging.info("Starting main program...")

    # Initialize device data and state
    initialize_device_data_and_state()

    # *** Start of New Initialization Steps ***
    # Ensure both devices are in a known state (OFF) and logs are cleared
    logging.info("Ensuring both devices are turned OFF and logs are cleared on startup.")
    try:
        # Turn off Device1
        control_usb_port(DEVICES["device1"]["usb_port_number"], "OFF")
        logging.info("Device1 port turned OFF on startup.")
        
        # Turn off Device2
        control_usb_port(DEVICES["device2"]["usb_port_number"], "OFF")
        logging.info("Device2 port turned OFF on startup.")
        
        # Clear logs for both devices
        clear_logs_for_device("device1")
        clear_logs_for_device("device2")
        
        logging.info("Logs cleared for both devices on startup.")
    except Exception as e:
        logging.error(f"Error during initial shutdown of devices: {e}")
    # *** End of New Initialization Steps ***

    # Initialize IoTConnect client
    if not initialize_iotconnect_client():
        logging.error("IoTConnect client initialization failed. Exiting program.")
        return

    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("Program interrupted by user.")
    finally:
        for ser in serial_objects.values():
            if ser.is_open:
                ser.close()
                logging.info(f"Serial connection for {ser.port} closed.")
        logging.info("Program exited cleanly.")

if __name__ == "__main__":
    main()

