import os
import subprocess
import threading
import select
import time
import sys
import logging
from dataclasses import dataclass
from pathlib import Path
import tomllib as toml
from typing import Any, Dict, List, Tuple

import serial
from serial import Serial
import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, MQTTMessage

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = Path("/etc/uart2mqtt.toml")

SERIAL_BASE_PATH = "/dev/serial/by-path"
BAUD_RATE = 115200

@dataclass(frozen=True, slots=True)
class MqttCfg:
    host: str
    port: int
    topic_base: str

@dataclass(frozen=True, slots=True)
class Cfg:
    mqtt      : MqttCfg
    usb_match : List[Tuple[str, str]] | None

def load_config(path: Path) -> Cfg:
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open("rb") as fp:
        raw = toml.load(fp)

    # ---------- MQTT ----------
    mqtt_raw = raw.get("mqtt", {})
    mqtt_cfg = MqttCfg(
        host       = mqtt_raw.get("host", "localhost"),
        port       = int(mqtt_raw.get("port", 1883)),
        topic_base = str(mqtt_raw.get("topic_base", "testbench")),
    )

    # ---------- USB allow‑list ----------
    usb_match_raw = raw.get("usb_match")
    allow: list[tuple[str, str]] | None = None

    if usb_match_raw:
        allow = []
        for entry in usb_match_raw:
            vid = str(entry.get("vid", "*")).lower()
            pid = str(entry.get("pid", "*")).lower()
            allow.append((vid, pid))

    return Cfg(mqtt=mqtt_cfg, usb_match=allow)

def vid_pid_from_symlink(symlink: Path) -> tuple[str, str] | None:
    try:
        real_tty   = Path(os.path.realpath(symlink))
        tty_name   = real_tty.name
        sys_tty    = Path("/sys/class/tty") / tty_name / "device"
        dev_path   = sys_tty.resolve()

        # Handle 2 channel devices
        for _ in range(3):
            vid_file = dev_path / "idVendor"
            pid_file = dev_path / "idProduct"
            if vid_file.exists() and pid_file.exists():
                vid = vid_file.read_text().strip().lower()
                pid = pid_file.read_text().strip().lower()
                return vid, pid
            dev_path = dev_path.parent

        logger.warning(f"No VID:PID found for {symlink} after walking sysfs")
        return None
    except Exception as e:
        logger.warning(f"Failed to resolve VID:PID for {symlink}: {e}")
        return None

def usb_allowed(vid: str, pid: str, allow: List[Tuple[str, str]] | None) -> bool:
    """Return True if (vid,pid) is permitted by the list."""
    if allow is None:
        return True  # “match everything”
    for v, p in allow:
        if (v in ("*", vid)) and (p in ("*", pid)):
            return True
    return False

def _wait_for_udev():
    try:
        subprocess.run(["udevadm", "settle", "-t", "2"], check=True)
    except subprocess.CalledProcessError:
        logger.warning("udevadm settle timed out")

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
        Root topic segment under ``/`` used when constructing per-port
        topics.
    mqtt_client:
        Instance of :class:`paho.mqtt.client.Client`.
    serial_ports:
        Map ``{port_name: {"connection": Serial, "thread": Thread, ...}}`` for
        all active devices.
    stop_event:
        Global flag signalling every thread to shut down.
    """
    def __init__(self, cfg: Cfg) -> None:
        self.cfg = cfg
        self.mqtt_client = mqtt.Client()
        self.serial_ports: Dict[str, Dict[str, Any]] = {}
        self.ignored_ports: set[str] = set()
        self.opened_real_devices: set[str] = set()
        self.stop_event = threading.Event()

    def mqtt_connect(self) -> None:
        def on_connect(client: Client, _userdata: Any, _flags: Dict[str, Any], rc: int) -> None:
            if rc == 0:
                logger.info("Connected to MQTT broker")
                client.subscribe(f"/{self.cfg.mqtt.topic_base}/+/device_serial_input")
            else:
                logger.error("MQTT connection failed with code %d", rc)

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
                self.mqtt_client.connect(self.cfg.mqtt.host, self.cfg.mqtt.port)
                self.mqtt_client.loop_start()
                return
            except Exception as e:
                logger.error(f"Failed to connect to MQTT broker: {e}, retrying...")
                time.sleep(3)

    def monitor_serial_ports(self) -> None:
        while not self.stop_event.is_set():
            _wait_for_udev()
            try:
                available_ports = set(sorted(os.listdir(SERIAL_BASE_PATH), key=lambda p: (not p.startswith("usbv2"), p)))
                current_ports = set(self.serial_ports.keys())

                # Handle new ports
                for port in available_ports - current_ports - self.ignored_ports:
                    full_path = Path(SERIAL_BASE_PATH) / port
                    real_dev_path = os.path.realpath(full_path)

                    if real_dev_path in self.opened_real_devices:
                        logger.debug(f"Skipping duplicate symlink {port} → {real_dev_path}")
                        self.ignored_ports.add(port)
                        continue

                    vid_pid = vid_pid_from_symlink(full_path)
                    if vid_pid:
                        vid, pid = vid_pid
                        if usb_allowed(vid, pid, self.cfg.usb_match):
                            logger.info(f"Opening Serial Port <{vid}:{pid}>({real_dev_path}): {port}")
                            self.opened_real_devices.add(real_dev_path)
                            self.start_serial_thread(port)
                        else:
                            logger.info(f"Ignoring Serial Port <{vid}:{pid}>: {port}")
                            self.ignored_ports.add(port)
                    else:
                        logger.warning(f"Could not resolve VID:PID for {port}, ignoring")
                        self.ignored_ports.add(port)

                # Remove vanished ports
                for port in current_ports - available_ports:
                    full_path = Path(SERIAL_BASE_PATH) / port
                    real_dev_path = os.path.realpath(full_path)
                    logger.info(f"Serial port removed: {port}")
                    self.stop_serial_thread(port)
                    self.opened_real_devices.remove(real_dev_path)

                for port in self.ignored_ports.copy():
                    if port not in available_ports:
                        self.ignored_ports.remove(port)

                time.sleep(1)
            except FileNotFoundError as e:
                if e.filename == SERIAL_BASE_PATH:
                    logger.warning(f"{SERIAL_BASE_PATH} not found, retrying in 1 second")
                    time.sleep(1)
                else:
                    logger.error(f"Unexpected FileNotFoundError: {e}")
                    time.sleep(1)
            except Exception as e:
                logger.error(f"Error monitoring serial ports: {e}")

    def start_serial_thread(self, port: str) -> None:
        try:
            full_path = os.path.join(SERIAL_BASE_PATH, port)
            serial_conn = serial.Serial(full_path, BAUD_RATE, timeout=0)
            topic_base = f"/{self.cfg.mqtt.topic_base}/{port}"
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
        except Exception as e:
            logger.error("Failed to open %s: %s", port, e)

    def stop_serial_thread(self, port: str) -> None:
        try:
            thread = self.serial_ports[port]["thread"]
            thread.join(timeout=2.0)  # Wait for thread to exit
            self.serial_ports[port]["connection"].close()
            self.serial_ports.pop(port, None)
            logger.info("Stopped monitoring serial port: %s", port)
        except Exception as e:
            logger.error("Error stopping %s: %s", port, e)


    def handle_serial(self, port: str,
                    serial_conn: Serial,
                    serial_output_topic: str) -> None:
        try:
            while not self.stop_event.is_set():
                rlist, _, _ = select.select([serial_conn], [], [], 0.1)
                if rlist:
                    data = serial_conn.read(serial_conn.in_waiting or 1)
                    if data:
                        self.mqtt_client.publish(serial_output_topic, data)

        except Exception as e:
            logger.error("Error in thread for %s: %s", port, e)

        finally:
            try:
                serial_conn.close()
            except Exception:
                pass

            self.serial_ports.pop(port, None)
            real_dev = os.path.realpath(serial_conn.port)
            self.opened_real_devices.discard(real_dev)

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
    """
    Start the bridge using the TOML file that sits alongside uart2mqtt.py.

    * If the file is missing or malformed we exit with code 1.
    * If everything loads, control never returns until the program is stopped.
    """
    try:
        cfg = load_config(CONFIG_FILE)
    except Exception as e:           # FileNotFoundError, toml.TOMLDecodeError…
        logger.error("Failed to load config %s: %s", CONFIG_FILE, e)
        return 1

    UART2MQTT(cfg).run()
    return 0
