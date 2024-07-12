import re
import queue
import threading
import time
from typing import *
from dataclasses import dataclass, field

from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .const import (
    DEVICE_PLATFORM_TYPE,
    DOMAIN,
    ELEMENT_BYTE_RANGE,
    LOGGER,
    NEW_DEVICE_TYPE,
    PRESET_ECO,
    PRESET_NONE,
    PRESET_NATURAL,
    PRESET_RESERVATION,
    PRESET_SLEEP
)

# TEST
THERMOSTAT_STATE_MAP = { 
    0x01: True,
    0x02: False,
    0x05: True,
    0x06: True,
    0x07: False,
    0x11: True,
}

PRESET_CODE_MAP = {
    PRESET_NONE: (0x00, 0x01, 0x02, 0x11),
    PRESET_SLEEP: 0x05,
    PRESET_RESERVATION: 0x06,
    PRESET_ECO: 0x07,
    PRESET_NATURAL: 0x10,
}

CODE_TO_PRESET_MAP = {
    v: key
    for key, value in PRESET_CODE_MAP.items()
    for v in (value if isinstance(value, tuple) else (value,))
}

@dataclass
class DeviceProfile:
    """Create an initial device profile."""
    unique_id: str
    device_name: str
    device_type: str
    device_room: str
    device_state: dict
    platform_type: str
    register_callback: Callable
    remove_callback: Callable
    set_command: Callable
    update_callback: Optional[Callable] = field(default=None)


class BestinController:
    """Bestin Controller Class."""

    def __init__(
        self, 
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        entities,
        host, 
        connection,
        async_add_device,
    ) -> None:
        self.hass = hass
        self.entry = config_entry
        self.entities = entities
        self.host = host
        self.connection = connection
        self.async_add_device = async_add_device
        self.gateway_type: str = config_entry.data["gateway_mode"][0]
        self.room_to_command: dict[bytes] = config_entry.data["gateway_mode"][1]
        
        self.device: dict[str, dict] = {}
        self.packet_queue: queue.Queue = queue.Queue()

        self.stop_event = threading.Event()
        self.process_thread = threading.Thread(
            target=self.process_data, args=(self.stop_event,)
        )
    
    @property
    def available(self) -> bool:
        """Check if the connection is alive"""
        return self.connection.is_connected()
    
    @property
    def receive_data(self) -> bytes:
        """Receive data from connection."""
        if self.available:
            return self.connection.receive()

    def send_data(self, packet: bytearray):
        """Send packet data to the connection."""
        if self.available:
            self.connection.send(packet)
    
    def start(self):
        """process_thread Starts threading."""
        self.process_thread.start()

    def calculate_checksum(self, packet: bytearray) -> int:
        """Compute checksum from packet data."""
        checksum = 3
        for i in range(len(packet) - 1):
            checksum ^= packet[i]
            checksum = (checksum + 1) & 0xFF
        return checksum
    
    def verify_checksum(self, packet: bytes) -> bool:
        """Checksum verification of packet data."""
        if len(packet) < 6:
            return False
        
        checksum = 3
        for byte in packet[:-1]:
            checksum ^= byte
            checksum = (checksum + 1) & 0xFF
        return checksum == packet[-1]

    def convert_unique_id(self, unique_id: str) -> tuple[str, Optional[str]]:
        """Convert device_id, sub_id from unique_id."""
        parts = unique_id.split("-")[0].split("_")
        if len(parts) > 3:
            sub_id = "_".join(parts[3:])
            device_id = "_".join(parts[1:3])
        else:
            sub_id = None
            device_id = "_".join(parts[1:3])
        return device_id, sub_id

    def get_devices_from_domain(self, domain: str) -> list[dict]:
        """Get devices registered to an entity based on domain."""
        entity_list = self.entities.get(domain, set())
        return [self._initialize_device(
            *self.convert_unique_id(uid), None) for uid in entity_list
        ]
    
    @property
    def lights(self) -> list[dict]:
        """Loads a device in the light domain from an entity."""
        return self.get_devices_from_domain(Platform.LIGHT)

    @property
    def switchs(self) -> list[dict]:
        """Loads a device in the switch domain from an entity."""
        return self.get_devices_from_domain(Platform.SWITCH)
    
    @property
    def sensors(self) -> list[dict]:
        """Loads a device in the sensor domain from an entity."""
        return self.get_devices_from_domain(Platform.SENSOR)

    @property
    def climates(self) -> list[dict]:
        """Loads a device in the climate domain from an entity."""
        return self.get_devices_from_domain(Platform.CLIMATE)
    
    @property
    def fans(self) -> list[dict]:
        """Loads a device in the fan domain from an entity."""
        return self.get_devices_from_domain(Platform.FAN)
    
    def register_callback(self, unique_id: str, update_callback: Callable):
        """Register a callback function for updates."""
        self.device[unique_id].update_callback = update_callback
        LOGGER.debug(
            f"Callback registered for device with unique_id: {unique_id}"
        )

    def remove_callback(self, unique_id: str):
        """Remove a registered callback function."""
        self.device[unique_id].update_callback = None
        LOGGER.debug(
            f"Callback removed for device with unique_id: {unique_id}"
        )

    def make_light_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool
    ) -> bytearray:
        """Create a light control packet."""
        aio_gateway = self.gateway_type == "AIO"
        onoff_value = 0x01 if value else 0x00
        onoff_value2 = 0x04 if value else 0x00
        position_flag = 0x80 if value else 0x00
        
        if aio_gateway:
            room_id_conv = 0x50 + room_id
            packet = self.make_common_packet(room_id_conv, 0x0A, 0x12)
            packet[5] = onoff_value
            packet[6] = 10 if pos_id == 4 else 1 << pos_id
        else:
            packet = self.make_common_packet(0x31, 0x0D, 0x01)
            packet[5] = room_id & 0x0F
            packet[6] = (0x01 << pos_id) | position_flag
            packet[11] = onoff_value2

        packet[-1] = self.calculate_checksum(packet)
        return packet
    
    def make_outlet_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool
    ) -> bytearray:
        """Create an outlet control packet."""
        aio_gateway = self.gateway_type == "AIO"
        onoff_value = 0x01 if value else 0x02
        onoff_value2 = (0x09 << pos_id) if value else 0x00
        position_flag = 0x80 if value else 0x00

        if aio_gateway:
            room_id_conv = 0x50 + room_id 
            packet = self.make_common_packet(room_id_conv, 0x0C, 0x12)
        else:
            packet = self.make_common_packet(0x31, 0x0D, 0x01)
            packet[5] = room_id & 0x0F

        if aio_gateway:
            packet[8] = 0x01
            packet[9] = (pos_id + 1) & 0x0F
            packet[10] = onoff_value >> (onoff_value + 3) if sub_type else onoff_value
        else:
            if sub_type == "cutoff":
                packet[8] = 0x83 if value else 0x03
            else:
                packet[7] = (0x01 << pos_id) | position_flag
                packet[11] = onoff_value2

        packet[-1] = self.calculate_checksum(packet)
        return packet
    
    def make_thermostat_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool | float
    ) -> bytearray:
        """Create an thermostat control packet."""
        packet = self.make_common_packet(0x28, 14, 0x12)
        packet[5] = room_id & 0x0F
        
        if sub_type == "preset":
            pass
        elif sub_type == "set_temperature":
            value_int = int(value)
            value_float = value - value_int
            packet[7] = value_int & 0xFF
            if value_float != 0:
                packet[7] |= 0x40
        else:
            packet[6] = 0x01 if value else 0x02

        packet[-1] = self.calculate_checksum(packet)
        return packet
    
    def make_gas_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool
    ) -> bytearray:
        """Create an gas control packet."""
        packet = bytearray(
            [0x02, 0x31, 0x02, self.timestamp & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        )
        packet[-1] = self.calculate_checksum(packet)
        return packet
    
    def make_doorlock_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool
    ) -> bytearray:
        """Create an doorlock control packet."""
        packet = bytearray(
            [0x02, 0x41, 0x02, self.timestamp & 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00]
        )
        packet[-1] = self.calculate_checksum(packet)
        return packet

    def make_fan_packet(
        self, room_id: int, pos_id: int, sub_type: str, value: bool | int
    ) -> bytearray:
        """Create an fan control packet."""
        packet = bytearray(
            [0x02, 0x61, 0x00, self.timestamp & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        )
        if sub_type == "speed":
            packet[2] = 0x03
            packet[6] = value
        elif sub_type == "preset":
            preset = PRESET_CODE_MAP[value]

            packet[2] = 0x07
            packet[5] = 0x00 if isinstance(preset, tuple) else preset
        else:
            packet[2] = 0x01
            packet[5] = 0x01 if value else 0x00
            packet[6] = 0x01

        packet[-1] = self.calculate_checksum(packet)
        return packet
    
    @callback
    def set_command(self, unique_id: str, value: Any, **kwargs: Optional[dict]):
        """Queue a command for the device identified by unique_id."""
        parts = unique_id.split("-")[0].split("_")    
        device_type = parts[1]
        room_id = int(parts[2])
        pos_id = 0
        sub_type = None
        
        if kwargs:
            for st, v in kwargs.items():
                sub_type, value = st, v
        if len(parts) == 4 and not parts[3].isdigit():
            sub_type = parts[3]
        elif len(parts) == 4 and parts[3].isdigit():
            pos_id = int(parts[3])    
        if len(parts) > 4:
            pos_id = int(parts[4])
            sub_type = parts[3]

        queue_task = {
            "attempt": 1,
            "device_type": device_type,
            "room_id": room_id,
            "pos_id": pos_id,
            "sub_type": sub_type,
            "value": value,
            "command": getattr(self, f"make_{device_type}_packet")(
                room_id, pos_id, sub_type, value
            ),
            "resp": None, # ACK
        }
        LOGGER.debug(f"Create queue task: {queue_task}")
        self.packet_queue.put(queue_task)

    def _initialize_device(
        self,
        device_id: str,
        sub_id: Optional[str],
        state: Union[bool, dict, int]
    ) -> dict[str, dict]:
        """Initial devices are created using unique_id."""
        device_type, device_room = device_id.split("_")
        unique_id = f"bestin_{device_id}_{sub_id}" if sub_id else f"bestin_{device_id}"
        device_name = unique_id
        unique_id = f"{unique_id}-{self.host}"
        
        if device_type != "energy" and (sub_id and not sub_id.isdigit()):
            letter_sub_id = ''.join(re.findall(r'[a-zA-Z]', sub_id))
            device_type = f"{device_type}:{letter_sub_id}"

        platform_type = DEVICE_PLATFORM_TYPE.get(device_type)
        if not platform_type:
            raise ValueError(f"Unsupported device type: {device_type}, {unique_id}")

        if unique_id not in self.device:
            device = DeviceProfile(
                unique_id=unique_id,
                device_name=device_name,  
                device_type=device_type,
                device_room=device_room,
                device_state=state,
                platform_type=platform_type,
                register_callback=self.register_callback,
                remove_callback=self.remove_callback,
                set_command=self.set_command
            )
            self.device[unique_id] = device

        if unique_id not in self.device:
            raise ValueError(f"Device with unique_id {unique_id} could not be initialized.")
        return self.device[unique_id]
    
    def setup_device(
        self,
        device_id: str,
        state: Union[bool, dict, int],
        is_sub: bool = False
    ) -> None:
        """Set up device with specified state."""
        if not device_id:
            return

        device_type = device_id.split("_")[0]
        if device_type not in NEW_DEVICE_TYPE:
            return
        
        final_states = state.items() if is_sub else [(None, state)]
        for sub_id, sub_state in final_states:
            unique_id = f"bestin_{device_id}_{sub_id}" if sub_id else f"bestin_{device_id}"
            unique_id = f"{unique_id}-{self.host}"

            if device_type != "energy" and (sub_id and not sub_id.isdigit()):
                letter_sub_id = ''.join(re.findall(r'[a-zA-Z]', sub_id))
                new_device = NEW_DEVICE_TYPE[f"{device_type}:{letter_sub_id}"]
            else:
                new_device = NEW_DEVICE_TYPE[device_type]

            device = self._initialize_device(device_id, sub_id, sub_state)
            self.async_add_device(new_device, device)

            current_device = self.device[unique_id]
            if current_device and current_device.device_state != sub_state:
                current_device.device_state = sub_state
                if update_callback := current_device.update_callback:
                    update_callback()

    def make_common_packet(
        self,
        header: int, # In case of AIO gateway, room_id is assigned
        length: int,
        packet_type: int,
    ) -> bytearray:
        """Create a base structure for common packets."""
        packet = bytearray([
            0x02, 
            header & 0xFF, 
            length & 0xFF, 
            packet_type & 0xFF, 
            self.timestamp & 0xFF
        ])
        packet.extend(bytearray([0] * (length - 5)))
        return packet

    def parse_thermostat(self, packet: bytearray) -> tuple[int, dict[str, Any]]:
        """Thermostat parse from packet data."""
        room_id = packet[5] & 0x0F
        is_heating = bool(packet[6] & 0x01)
        #is_heating = THERMOSTAT_STATE_MAP[packet[6]]
        target_temperature = (packet[7] & 0x3F) + (packet[7] & 0x40 > 0) * 0.5
        current_temperature = int.from_bytes(packet[8:10], byteorder='big') / 10.0
        hvac_mode = HVACMode.HEAT if is_heating else HVACMode.OFF
        #preset_mode = CODE_TO_PRESET_MAP[packet[6]]

        thermostat_state = {
            "mode": hvac_mode,
            "preset": PRESET_NONE, #preset_mode, 
            "target_temperature": target_temperature,
            "current_temperature": current_temperature
        }
        return room_id, thermostat_state
    
    def parse_gas(self, packet: bytearray) -> tuple[int, bool]:
        """Gas parse from packet data."""
        room_id = 0
        gas_state = bool(packet[5])
        return room_id, gas_state
    
    def parse_doorlock(self, packet: bytearray) -> tuple[int, bool]:
        """Doorlock parse from packet data."""
        room_id = 0
        doorlock_state = bool(packet[5] & 0xAE)
        return room_id, doorlock_state
    
    def parse_fan(self, packet: bytearray) -> tuple[int, dict[str, Any]]:
        """Fan parse from packet data."""
        room_id = 0
        natural = packet[5] >> 4 & 1
        preset_mode	= CODE_TO_PRESET_MAP[natural]

        fan_state = {
            "state": bool(packet[5] & 0x01),
            "speed": packet[6],
            "timer": packet[7],
            "preset": preset_mode,
        }
        return room_id, fan_state
    
    def parse_state_General(self, packet: bytearray) -> tuple[int, dict[str, dict]]:
        """Energy state General-gateway parse from packet data."""
        state_general = {"light": {}, "outlet": {}}
        room_id = packet[5] & 0x0F
        if room_id == 1:
            iterations = 4, 3
        else:
            iterations = 2, 2

        for i in range(iterations[0]):
            light_state = bool(packet[6] & (0x01 << i))
            state_general["light"][str(i)] = light_state

        for i in range(iterations[1]): 
            idx = 14 + 2 * i
            idx2 = idx + 2

            if len(packet) > idx2:
                value = int.from_bytes(packet[idx:idx2], byteorder='big')
                consumption = value / 10.
            else:
                consumption = 0.

            outlet_state = bool(packet[7] & (0x01 << i))
            outlet_cutoff = bool(packet[7] >> 4 & 1)

            state_general["outlet"][str(i)] = outlet_state
            state_general["outlet"]["cutoff"] = outlet_cutoff
            state_general["outlet"][f"consumption_{str(i)}"] = consumption

        return room_id, state_general
    
    def parse_state_AIO(self, packet: bytearray) -> tuple[int, dict[str, dict]]:
        """Energy state AIO(all-in-one)-gateway parse from packet data."""
        state_aio = {"light": {}, "outlet": {}}
        room_id = packet[1] & 0x0F

        for i in range(packet[5]):
            light_state = bool(packet[6] & (1 << i))
            state_aio["light"][str(i)] = light_state

        for i in range(2):
            idx = 9 + 5 * i  # state
            idx2 = 10 + 5 * i  # consumption

            outlet_state = packet[idx] in [0x21, 0x11]
            outlet_cutoff = packet[idx] in [0x11, 0x13, 0x12]
            outlet_consumption = (packet[idx2] << 8 | packet[idx2 + 1]) / 10

            state_aio["outlet"][str(i)] = outlet_state
            state_aio["outlet"][f"cutoff_{str(i)}"] = outlet_cutoff
            state_aio["outlet"][f"consumption_{str(i)}"] = outlet_consumption

        return room_id, state_aio
    
    def parse_energy(self, packet: bytearray) -> dict[str, dict]:
        """Energy parse from packet data."""
        index = 13
        energy_state = {}
        element_offset = 1 if self.gateway_type == "AIO" else 0

        if element_offset == 1:
            elements = ["electric", "water", "gas"] 
        else:
            elements = ["electric", "water", "hotwater", "gas", "heat"]

        for element in elements:
            total_value = float(packet[ELEMENT_BYTE_RANGE[element][element_offset]].hex())
            realtime_value = int(packet[index:index + 2].hex())

            energy_state[element] = {"total": total_value, "realtime": realtime_value}
            index += 8

        return energy_state

    def evaluate_command_packet(self, packet: bytes, queue: list[dict[str, Any]]) -> None:
        """Processes data related to the response after the command."""
        general_gateway = self.gateway_type == "General"
        command = queue["command"]
        header_byte = command[1]

        offset = 2 if general_gateway and len(command) == 10 else 3
        command_4bit = 0x9 if not general_gateway or header_byte == 0x28 else 0x8

        overview = (command_4bit << 4) | (command[offset] & 0x0F) # Line 0-3
        packet_value = packet[offset]

        if header_byte == packet[1] and (
            overview == packet_value or packet_value == 0x81
        ):
            queue["resp"] = packet
            #self.parse_packet_data(packet)

    def send_packet_queue(self, queue: dict[str, Any]) -> None:
        """Sends queued command packet data."""
        LOGGER.info(
            "Send the %s command of the %s device. command Packet: %s, attempts: %s",
            queue["value"],
            queue["device_type"],
            queue["command"].hex(),
            queue["attempt"],
        )
        queue["attempt"] += 1
        time.sleep(0.185)
        self.send_data(queue["command"])

    def parse_packet_data(self, packet: bytes) -> None:
        """Parse the packet data to get the status of the device."""
        header = packet[1]
        packet_len = len(packet)
        room_id = device_state = device_id = None

        if packet_len == 10:
            command = packet[2]
            self.timestamp = packet[3]
        else:
            command = packet[3]
            self.timestamp = packet[4]

        if packet_len != 10 and command in [0x81, 0x82, 0x91, 0x92, 0xB2]:
            if header == 0x28:
                room_id, device_state = self.parse_thermostat(packet)
                device_id = f"thermostat_{room_id}"
                self.setup_device(device_id, device_state)
            elif header == 0x31 or packet_len in [20, 22]:
                room_id, device_state = getattr(self, f"parse_state_{self.gateway_type}")(packet)
                for device, state in device_state.items():
                    device_id = f"{device}_{room_id}"
                    self.setup_device(device_id, state, True)
            elif header == 0xD1:
                device_state = self.parse_energy(packet)
                for room_id, state in device_state.items():
                    device_id = f"energy_{room_id}"
                    self.setup_device(device_id, state, True)

        elif packet_len == 10 and command != 0x00:
            parser_mapping = {
                0x31: (self.parse_gas, "gas"),
                0x41: (self.parse_doorlock, "doorlock"),
                0x61: (self.parse_fan, "fan"),
            }
            if header in parser_mapping:
                parse_func, device_type = parser_mapping[header]
                room_id, device_state = parse_func(packet)
                device_id = f"{device_type}_{room_id}"
                self.setup_device(device_id, device_state)

    def process_packet_queue(self, max_attempt=20) -> None:
        """Processes queued command packet data."""
        queue = self.packet_queue.queue[0]
        self.send_packet_queue(queue)

        if resp := queue["resp"]:
            LOGGER.info(
                "Command successful for %s device (value: %s), ACK: %s, Attempts: %s",
                queue["device_type"].capitalize(),
                queue["value"],
                resp.hex(),
                queue["attempt"],
            )
            self.packet_queue.get()

        elif queue["attempt"] > max_attempt:
            LOGGER.info(
                "Command for %s device exceeded %s attempts, cancelling operation",
                queue["device_type"], max_attempt
            )
            self.packet_queue.get()

    def process_data(self, stop_event) -> None:
        """Processes received packet data and packet queue data.""" 
        while not stop_event.is_set():
            received_data = self.receive_data if self.available else None

            if received_data and self.verify_checksum(received_data):
                self.parse_packet_data(received_data)
                if not self.packet_queue.empty():
                    self.evaluate_command_packet(received_data, self.packet_queue.queue[0])

            if not self.packet_queue.empty():
                self.process_packet_queue()