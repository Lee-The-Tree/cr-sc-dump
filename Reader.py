# -*- coding: utf-8 -*-
import io
import lzma
import zstandard

class Reader(io.BytesIO):
    def __init__(self, stream):
        super().__init__(stream)
        self._bytes_left = len(stream)
        self._bytes_read = 0

    def __len__(self):
        return max(0, self._bytes_left)

    def read(self, size=-1):
        if size == -1 or size > self._bytes_left:
            size = self._bytes_left
        
        data = super().read(size)
        actual_size = len(data)
        self._bytes_left -= actual_size
        self._bytes_read += actual_size
        return data

    def read_byte(self):
        res = self.read(1)
        return res[0] if res else 0

    def read_uint16(self, byteorder="little"):
        return int.from_bytes(self.read(2), byteorder)

    def read_int16(self, byteorder="little"):
        return int.from_bytes(self.read(2), byteorder, signed=True)

    def read_uint32(self, byteorder="little"):
        return int.from_bytes(self.read(4), byteorder)

    def read_int32(self, byteorder="little"):
        return int.from_bytes(self.read(4), byteorder, signed=True)

    def read_string(self, length=None):
        if length is None:
            length = self.read_byte()
        if length == 0xFF or length == 0:
            return ""
        data = self.read(length)
        try:
            return data.decode("utf-8")
        except:
            return ""

def decompress(data):
    if not data: return b""
    if data[0:4] == b"SCLZ":
        import lzham
        dict_size = data[4]
        uncompressed_size = int.from_bytes(data[5:9], byteorder="little")
        return lzham.decompress(data[9:], uncompressed_size, {"dict_size_log2": dict_size})
    elif data[0:4] == zstandard.FRAME_HEADER:
        return zstandard.decompress(data)
    else:
        # Try LZMA
        try:
            # Supercell LZMA format: 5 bytes properties, 4 bytes uncompressed size (little endian), then data
            # Standard LZMA header: 5 bytes properties, 8 bytes uncompressed size
            fixed_data = data[0:9] + (b"\x00" * 4) + data[9:]
            return lzma.LZMADecompressor().decompress(fixed_data)
        except:
            return data # Return as is if decompression fails

# Compatibility functions for old sc_decode.py
def ReadByte(stream):
    if hasattr(stream, 'read_byte'):
        return stream.read_byte()
    return int.from_bytes(stream.read(1), 'little')

def ReadUint16(stream):
    if hasattr(stream, 'read_uint16'):
        return stream.read_uint16()
    return int.from_bytes(stream.read(2), 'little')

def ReadInt16(stream):
    if hasattr(stream, 'read_int16'):
        return stream.read_int16()
    return int.from_bytes(stream.read(2), 'little', signed=True)

def ReadUint32(stream):
    if hasattr(stream, 'read_uint32'):
        return stream.read_uint32()
    return int.from_bytes(stream.read(4), 'little')

def ReadInt32(stream):
    if hasattr(stream, 'read_int32'):
        return stream.read_int32()
    return int.from_bytes(stream.read(4), 'little', signed=True)

def ReadString(stream, length):
    if hasattr(stream, 'read_string'):
        return stream.read_string(length)
    return stream.read(length).decode('utf-8')
