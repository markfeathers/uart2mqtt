UART2MQTT
=========

Bidirectional bridge between USB-enumerated UART devices and MQTT topics.

Overview
--------

`UART2MQTT` continuously discovers USB-serial adapters under `/dev/serial/by-path`, streams their output to MQTT, and forwards MQTT messages back to the corresponding serial port. Each serial port has two associated MQTT topics for input and output, allowing remote interaction with multiple UART devices over MQTT.

The project is written in Python and is intended for headless Linux systems with USB-serial devices connected.

Features
--------

- Dynamic discovery of USB-serial adapters using `/dev/serial/by-path`.
- USB device VID:PID allow-list filtering.
- MQTT-based bi-directional data bridging.
- Auto-reconnection to MQTT broker.
- Robust multithreaded design:
  - Serial port monitor thread.
  - Dedicated thread per serial port for non-blocking IO.
- MQTT topics automatically created for each serial port.

MQTT Topics
------------

Each port gets two MQTT topics:

- **Device Output (UART → MQTT)**::
  
    /<mqtt_topic_base>/<port>/device_serial_output

- **Device Input (MQTT → UART)**::
  
    /<mqtt_topic_base>/<port>/device_serial_input

Example:

- `/testbench/usbv2-1.3.1/device_serial_output`
- `/testbench/usbv2-1.3.1/device_serial_input`

Configuration
-------------

All configuration is provided via a TOML file located at::

    /etc/uart2mqtt.toml

Example config file::

    [mqtt]
    host = "mqtt-broker-host"
    port = 1883
    topic_base = "testbench"

    [[usb_match]]
    vid = "10c4"
    pid = "ea60"

    [[usb_match]]
    vid = "*"
    pid = "*"  # Allow all devices (wildcard)

Config Options:

- **mqtt.host**: Hostname or IP address of MQTT broker (default: `localhost`)
- **mqtt.port**: TCP port of MQTT broker (default: `1883`)
- **mqtt.topic_base**: Root topic segment (default: `testbench`)
- **usb_match**: Optional list of allowed USB VID:PID pairs. If omitted, all devices are allowed.

USB VID/PID Filtering
---------------------

The allow-list is specified via the `usb_match` section. Each entry may contain:

- `vid`: Vendor ID (hexadecimal string, case-insensitive, "*" for wildcard)
- `pid`: Product ID (hexadecimal string, case-insensitive, "*" for wildcard)

If no `usb_match` section is provided, all USB serial devices will be processed.

Dependencies
------------

- Python 3.11+ (uses `tomllib`)
- paho-mqtt
- pyserial

Install dependencies via::

    pip install paho-mqtt pyserial

Usage
-----

After configuration, simply run::

    python uart2mqtt.py

The program will:

- Connect to the MQTT broker.
- Continuously monitor for USB-serial devices.
- Bridge serial data to/from MQTT topics.

Graceful shutdown on `SIGINT` (Ctrl+C).

Logging
-------

Logs are written to standard output using Python's built-in logging.

- INFO: High-level state changes (connections, port add/remove, etc)
- DEBUG: Data transfer details (disabled by default)

Design Notes
------------

- Uses `select` to avoid busy-polling serial ports.
- Ignores duplicate devices (e.g. multiple symlinks pointing to the same underlying USB serial device).
- MQTT subscriptions are automatically updated as new serial devices appear.

License
-------

MIT License (or specify your license here)

Author
------

Mark Featherston
