import asyncio
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from .protocol import MpptProtocol, ResultContainer, Result


class MpptClient(MpptProtocol):
    DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
    NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
    WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

    buffer = bytearray()

    def __init__(self, mac_address: str):
        self.client = BleakClient(mac_address)
        self.response_queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        super().__init__()

    async def __aenter__(self):
        await self.client.connect()
        await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.stop_notify(self.NOTIFY_UUID)
        try:
            await self.client.disconnect()
        except EOFError:
            pass

    def notification_handler(self, characteristic: BleakGATTCharacteristic, data: bytearray):
        if characteristic.uuid != self.NOTIFY_UUID:
            return
        self.buffer += data
        try:
            if not self.is_complete(self.buffer):
                return
            results = self.parse(self.start_address, self.buffer)
            self.response_queue.put_nowait(results)
        except Exception as e:
            print(f"Response from device: 0x{self.buffer.hex()}")
            print(f"Error while parsing response: {e}")

    async def read(self, start_address: int, count: int, repeat: int = 10, timeout: float = 2) -> ResultContainer:
        async with self.lock:
            self.start_address = start_address
            command = self.get_read_command(0xFE, start_address, count)
            self.response_queue = asyncio.Queue()
            i = 0
            while i < repeat:
                i += 1
                self.buffer = bytearray()
                await self.client.write_gatt_char(self.WRITE_UUID, command)
                try:
                    return await asyncio.wait_for(self.response_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if self.buffer:
                        print(f"Got partial response: 0x{self.buffer.hex()}")
                    print(f"Repeating read command...")
            return ResultContainer([])

    async def read_status(self) -> ResultContainer:
        """Read real-time status registers (address 0x3030, 41 registers)."""
        return await self.read(0x3030, 41)

    async def read_parameters(self) -> ResultContainer:
        """Read battery and load parameter registers (address 0x9021, 12 registers)."""
        return await self.read(0x9021, 12)

    async def write(self, results: list[Result], repeat: int = 10, timeout: float = 2) -> ResultContainer:
        async with self.lock:
            start_address, command = self.get_write_command(self.device_id, results)
            self.start_address = start_address
            self.response_queue = asyncio.Queue()
            i = 0
            while i < repeat:
                i += 1
                self.buffer = bytearray()
                await self.client.write_gatt_char(self.WRITE_UUID, command)
                try:
                    await asyncio.wait_for(self.response_queue.get(), timeout=timeout)
                    return ResultContainer(results)
                except asyncio.TimeoutError:
                    if self.buffer:
                        print(f"Got partial response: 0x{self.buffer.hex()}")
                    print(f"Repeating write command...")
            return ResultContainer([])

    async def get_device_name(self) -> str:
        device_name = await self.client.read_gatt_char(self.DEVICE_NAME_UUID)
        return "".join(map(chr, device_name))

    async def list_services(self):
        service_text = "[Service]          "
        charact_text = "  [Characteristic] "
        descrip_text = "    [Descriptor]   "
        value_text   = "      Value = "
        for service in self.client.services:
            print(service_text, service)
            for char in service.characteristics:
                print(charact_text, char, ",".join(char.properties))
                for descriptor in char.descriptors:
                    try:
                        print(descrip_text, descriptor)
                        value = await self.client.read_gatt_descriptor(descriptor.handle)
                        print(value_text, "0x" + value.hex())
                    except Exception as e:
                        pass

    @staticmethod
    async def scan() -> list:
        """Scan for nearby BLE devices and return a list of discovered devices."""
        return await BleakScanner.discover()
