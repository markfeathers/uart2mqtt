import os
import threading
import select
import time
import sys
import logging
from typing import Any, Dict

import serial
from serial import Serial
import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, MQTTMessage

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SERIAL_BASE_PATH = "/dev/serial/by-path"
BAUD_RATE = 115200

class UART2MQTT:
    """Bidirectional bridge between USB-enumerated UART devices and MQTT topics.

    The class continuously discovers USB-serial adapters under
    ``/dev/serial/by-path``, streams their output to MQTT, and forwards MQTT
    messages back to the corresponding serial port.  Every port gets two MQTT
    topics:

    * ``testbench/<mqtt_topic_base>/<port>/device_serial_output`` – data **from**
      the device (UART → MQTT).
    * ``testbench/<mqtt_topic_base>/<port>/device_serial_input`` – data **to**
      the device (MQTT → UART).

    Threads:
        * One monitor thread watches the directory for new/removed devices.
        * One thread per active serial port handles non-blocking IO.

    Attributes
    ----------
    mqtt_host:
        Hostname or IP address of the MQTT broker.
    mqtt_port:
        TCP port of the MQTT broker.
    mqtt_topic_base:
        Root topic segment under ``testbench/`` used when constructing per-port
        topics.
    mqtt_client:
        Instance of :class:`paho.mqtt.client.Client`.
    serial_ports:
        Map ``{port_name: {"connection": Serial, "thread": Thread, ...}}`` for
        all active devices.
    stop_event:
        Global flag signalling every thread to shut down.
    """
    def __init__(self, mqtt_host: str, mqtt_port: int, mqtt_topic_base: str):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_topic_base = mqtt_topic_base
        self.mqtt_client = mqtt.Client()
        self.serial_ports: Dict[str, Dict[str, Any]] = {}
        self.stop_event = threading.Event()

    def mqtt_connect(self) -> None:
        def on_connect(client: Client, _userdata: Any, _flags: Dict[str, Any], rc: int) -> None:
            if rc == 0:
                logger.info("Connected to MQTT broker")
                # Subscribe to the wildcard topic for serial input
                client.subscribe(f"testbench/{self.mqtt_topic_base}/+/device_serial_input")
            else:
                logger.error(f"MQTT connection failed with code %d", rc)

        def on_message(_client: Client, _userdata: Any, message: MQTTMessage) -> None:
            port_name = message.topic.split("/")[-2]  # Extract port name from the topic
            if port_name in self.serial_ports:
                try:
                    # Write incoming data to the corresponding UART port
                    serial_conn = self.serial_ports[port_name]["connection"]
                    serial_conn.write(message.payload)
                    logger.debug("Data written to %s: %s", port_name, message.payload)
                except Exception as e:
                    logger.error("Error writing to UART %s: %s", port_name, e)
            else:
                logger.warning("Port %s not found for topic %s", port_name, message.topic)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message

        while not self.stop_event.is_set():
            try:
                self.mqtt_client.connect(self.mqtt_host, self.mqtt_port)
                self.mqtt_client.loop_start()
                return
            except Exception as e:
                logger.error(f"Failed to connect to MQTT broker: {e}, retrying...")
                time.sleep(3)

    def monitor_serial_ports(self) -> None:
        while not self.stop_event.is_set():
            try:
                available_ports = set(os.listdir(SERIAL_BASE_PATH))
                current_ports = set(self.serial_ports.keys())

                # Add new ports
                for port in available_ports - current_ports:
                    logger.info(f"New serial port detected: {port}")
                    self.start_serial_thread(port)

                # Remove vanished ports
                for port in current_ports - available_ports:
                    logger.info(f"Serial port removed: {port}")
                    self.stop_serial_thread(port)

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error monitoring serial ports: {e}")

    def start_serial_thread(self, port: str) -> None:
        try:
            full_path = os.path.join(SERIAL_BASE_PATH, port)
            serial_conn = serial.Serial(full_path, BAUD_RATE, timeout=0)
            topic_base = f"testbench/{self.mqtt_topic_base}/{port}"
            serial_output_topic = f"{topic_base}/device_serial_output"
            serial_input_topic = f"{topic_base}/device_serial_input"

            # Subscribe to the input topic dynamically for this port
            self.mqtt_client.subscribe(serial_input_topic)

            thread = threading.Thread(target=self.handle_serial,
                                       args=(port, serial_conn, serial_output_topic),
                                       daemon=True)
            self.serial_ports[port] = {
                "connection": serial_conn,
                "thread": thread,
                "serial_output_topic": serial_output_topic,
                "serial_input_topic": serial_input_topic
            }
            thread.start()
            logger.info(f"Started monitoring serial port: {port}")
        except Exception as e:
            logger.error(f"Failed to open {port}: {e}")

    def stop_serial_thread(self, port: str) -> None:
        try:
            self.serial_ports[port]["connection"].close()
            self.serial_ports.pop(port, None)
            logger.info(f"Stopped monitoring serial port: {port}")
        except Exception as e:
            logger.error(f"Error stopping {port}: {e}")

    def handle_serial(self, port: str, serial_conn: Serial, serial_output_topic: str) -> None:
        logger.info(f"Thread started for port: {port}")

        while not self.stop_event.is_set():
            try:
                # Use select to wait for data instead of polling
                rlist, _, _ = select.select([serial_conn], [], [], 0.1) # 100ms timeout

                if rlist:
                    data = serial_conn.read(serial_conn.in_waiting or 1)
                    if data:
                        logger.debug("Serial read from %s: %s", port, data)
                        self.mqtt_client.publish(serial_output_topic, data)
            except Exception as e:
                logger.error(f"Error in thread for {port}: {e}")
                break

        logger.info("Thread exiting for port: %s", port)

    def stop(self) -> None:
        self.stop_event.set()
        for port in list(self.serial_ports.keys()):
            self.stop_serial_thread(port)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def run(self) -> None:
        self.mqtt_connect()

        try:
            monitor_thread = threading.Thread(target=self.monitor_serial_ports,
                                              daemon=True)
            monitor_thread.start()

            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.stop()

def main() -> int:
    if len(sys.argv) < 4:
        print("Usage: python uart2mqtt <mqtt_host> <mqtt_port> <mqtt_topic_base>")
        return 1
    else:
        mqtt_host = sys.argv[1]
        mqtt_port = int(sys.argv[2])
        mqtt_topic_base = sys.argv[3]
        uart2mqtt = UART2MQTT(mqtt_host, mqtt_port, mqtt_topic_base)
        uart2mqtt.run()
        return 0
