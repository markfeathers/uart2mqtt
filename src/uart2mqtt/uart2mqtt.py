import os
import paho.mqtt.client as mqtt
import serial
import threading
import time
import sys

SERIAL_BASE_PATH = "/dev/serial/by-path"
BAUD_RATE = 115200


class UART2MQTT:
    def __init__(self, mqtt_host, mqtt_port, mqtt_topic_base):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_topic_base = f'testbench/{mqtt_topic_base}'
        self.mqtt_client = mqtt.Client()
        self.serial_ports = {}
        self.stop_event = threading.Event()

    def mqtt_connect(self):
        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                print("Connected to MQTT broker")
            else:
                print(f"MQTT connection failed with code {rc}")

        self.mqtt_client.on_connect = on_connect

        while not self.stop_event.is_set():
            try:
                self.mqtt_client.connect(self.mqtt_host, self.mqtt_port)
                self.mqtt_client.loop_start()
                return
            except Exception as e:
                print(f"Failed to connect to MQTT broker: {e}, retrying...")
                time.sleep(5)

    def monitor_serial_ports(self):
        while not self.stop_event.is_set():
            try:
                available_ports = set(os.listdir(SERIAL_BASE_PATH))
                current_ports = set(self.serial_ports.keys())

                # Add new ports
                for port in available_ports - current_ports:
                    self.start_serial_thread(port)

                # Remove vanished ports
                for port in current_ports - available_ports:
                    self.stop_serial_thread(port)

                time.sleep(1)
            except Exception as e:
                print(f"Error monitoring serial ports: {e}")

    def start_serial_thread(self, port):
        try:
            full_path = os.path.join(SERIAL_BASE_PATH, port)
            serial_conn = serial.Serial(full_path, BAUD_RATE, timeout=1)
            thread = threading.Thread(target=self.handle_serial,
                                      args=(port, serial_conn),
                                      daemon=True)
            self.serial_ports[port] = {
                "connection": serial_conn,
                "thread": thread
            }
            thread.start()
            print(f"Started monitoring {port}")
        except Exception as e:
            print(f"Failed to open {port}: {e}")

    def stop_serial_thread(self, port):
        try:
            self.serial_ports[port]["connection"].close()
            self.serial_ports.pop(port, None)
            print(f"Stopped monitoring {port}")
        except Exception as e:
            print(f"Error stopping {port}: {e}")

    def handle_serial(self, port, serial_conn):
        topic = f"{self.mqtt_topic_base}/{port}/data"

        def on_message(client, userdata, message):
            try:
                serial_conn.write(message.payload)
            except Exception as e:
                print(f"Error writing to {port}: {e}")

        self.mqtt_client.subscribe(topic)
        self.mqtt_client.message_callback_add(topic, on_message)

        while not self.stop_event.is_set():
            try:
                if serial_conn.in_waiting > 0:
                    data = serial_conn.read(serial_conn.in_waiting)
                    self.mqtt_client.publish(topic, data)
            except Exception as e:
                print(f"Error reading from {port}: {e}")
                break

        self.mqtt_client.message_callback_remove(topic)

    def stop(self):
        self.stop_event.set()
        for port in list(self.serial_ports.keys()):
            self.stop_serial_thread(port)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def run(self):
        self.mqtt_connect()

        try:
            monitor_thread = threading.Thread(target=self.monitor_serial_ports,
                                              daemon=True)
            monitor_thread.start()

            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            print("Shutting down...")
        finally:
            self.stop()


def main():
    if len(sys.argv) < 4:
        print("Usage: python uart2mqtt <mqtt_host> <mqtt_port> "
              "<mqtt_topic_base>")
    else:
        mqtt_host = sys.argv[1]
        mqtt_port = int(sys.argv[2])
        mqtt_topic_base = sys.argv[3]
        uart2mqtt = UART2MQTT(mqtt_host, mqtt_port, mqtt_topic_base)
        uart2mqtt.run()
