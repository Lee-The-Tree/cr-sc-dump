#!/usr/bin/env python3

import argparse
import io
import logging
import lzma
import os
import re

from PIL import Image
import texture2ddecoder
import zstandard


class Reader(io.BytesIO):
    def __init__(self, stream):
        super().__init__(stream)
        self._bytes_left = len(stream)
        self._bytes_read = 0

    def __len__(self):
        return max(0, self._bytes_left)

    def align_to(self, alignment):
        remainder = self._bytes_read % alignment
        if remainder != 0:
            self.read(alignment - remainder)

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

    def read_int32(self, byteorder="little"):
        return int.from_bytes(self.read(4), byteorder, signed=True)

    def read_uint32(self, byteorder="little"):
        return int.from_bytes(self.read(4), byteorder)

    def read_uint32_big(self):
        return int.from_bytes(self.read(4), byteorder="big")

    def read_string(self):
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


def create_image(width, height, pixels, sub_type):
    try:
        if sub_type in [0, 1]:  # RGBA8888
            return Image.frombytes("RGBA", (width, height), pixels, "raw")
        elif sub_type == 2:  # RGBA4444
            img = Image.new("RGBA", (width, height))
            ps = img.load()
            for h in range(height):
                for w in range(width):
                    i = (w + h * width) * 2
                    if i + 2 > len(pixels): continue
                    p = int.from_bytes(pixels[i : i + 2], "little")
                    ps[w, h] = (((p >> 12) & 0xF) << 4, ((p >> 8) & 0xF) << 4, ((p >> 4) & 0xF) << 4, (p & 0xF) << 4)
            return img
        elif sub_type == 3:  # RBGA5551
            return Image.frombytes("RGBA", (width, height), pixels, "raw", ("RGBA;4B", 0, 0))
        elif sub_type == 4:  # RGB565
            img = Image.new("RGB", (width, height))
            ps = img.load()
            for h in range(height):
                for w in range(width):
                    i = (w + h * width) * 2
                    if i + 2 > len(pixels): continue
                    p = int.from_bytes(pixels[i : i + 2], "little")
                    ps[w, h] = (((p >> 11) & 0x1F) << 3, ((p >> 5) & 0x3F) << 2, (p & 0x1F) << 3)
            return img
        elif sub_type == 6:  # LA88
            return Image.frombytes("LA", (width, height), pixels)
        elif sub_type == 10:  # L8
            return Image.frombytes("L", (width, height), pixels)
    except Exception as e:
        logging.debug(f"create_image error: {e}")
    return None


def pixel_size(sub_type):
    if sub_type in [0, 1]: return 4
    if sub_type in [2, 3, 4, 6]: return 2
    if sub_type in [10]: return 1
    return 4


ASTC_MAP = {
    0x93B0: (4, 4), 0x93B1: (5, 4), 0x93B2: (5, 5), 0x93B3: (6, 5), 0x93B4: (6, 6),
    0x93B5: (8, 5), 0x93B6: (8, 6), 0x93B7: (8, 8), 0x93B8: (10, 5), 0x93B9: (10, 6),
    0x93BA: (10, 8), 0x93BB: (10, 10), 0x93BC: (12, 10), 0x93BD: (12, 12),
    157: (4, 4), 158: (5, 4), 159: (5, 5), 160: (6, 5), 161: (6, 6),
    162: (8, 5), 163: (8, 6), 164: (8, 8), 165: (10, 5), 166: (10, 6),
    167: (10, 8), 168: (10, 10), 169: (12, 10), 170: (12, 12),
}


def process_ktx(base_name, data, path):
    reader = Reader(data)
    identifier = reader.read(12)
    if b"KTX 11" in identifier:
        reader.read(16)
        file_type = reader.read_uint32()
        reader.read(4)
        width = reader.read_uint32()
        height = reader.read_uint32()
        reader.read(16)
        reader.read(reader.read_uint32()) # Skip key-value data
        reader.read(4) # image size
        image_data = reader.read()
    elif b"KTX 20" in identifier:
        file_type = reader.read_uint32()
        reader.read(4) # typeSize
        width = reader.read_uint32()
        height = reader.read_uint32()
        reader.read(12) # depth, layer, face
        level_count = reader.read_uint32()
        reader.read(4) # supercompression
        reader.read(16) # dfd, kvd offsets/lengths
        reader.read(16) # sgd offset/length
        for _ in range(max(1, level_count)):
            reader.read(24) # level index
        image_data = reader.read()
    else:
        return

    pixels = None
    if file_type in ASTC_MAP:
        bw, bh = ASTC_MAP[file_type]
        pixels = texture2ddecoder.decode_astc(image_data, width, height, bw, bh)
    elif file_type == 0x8D64: # ETC1
        pixels = texture2ddecoder.decode_etc1(image_data, width, height)
    
    if pixels:
        img = Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
        os.makedirs(path, exist_ok=True)
        img.save(os.path.join(path, f"{base_name}.png"))


def process_sctx(base_name, data, path):
    try:
        reader = Reader(data)
        reader.read(48)
        file_type = reader.read_uint32()
        width = reader.read_uint16()
        height = reader.read_uint16()
        some_type = reader.read_uint32()
        reader.read(20)
        key_value_data_size = reader.read_uint32()
        reader.read(key_value_data_size)
        reader.read(52)
        if width == 0 or height == 0: return
        if some_type == 12:
            pixels = reader.read()
            bw = bh = 4
        elif some_type == 5:
            pixels = decompress(reader.read())
            bw = bh = 8
        else:
            reader.read(some_type - key_value_data_size - 76)
            pixels = decompress(reader.read())
            bw = bh = 8
        pixels = texture2ddecoder.decode_astc(pixels, width, height, bw, bh)
        img = Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
        os.makedirs(path, exist_ok=True)
        img.save(os.path.join(path, f"{base_name}.png"))
    except: pass


def process_sc(base_dir, base_name, data, path, old):
    try:
        reader = Reader(data)
        if reader.read(2) != b"SC": return
        major = reader.read_uint32_big()
        minor = reader.read_uint32_big()
        hash_len = reader.read_uint32_big()
        reader.read(hash_len)
        decompressed = decompress(reader.read())
        if not decompressed: return
        
        # Check if it's a wrapped KTX
        if decompressed[:7] == b"\xab\x4b\x54\x58\x20\x31\x31" or decompressed[:7] == b"\xab\x4b\x54\x58\x20\x32\x30":
            process_ktx(base_name, decompressed, path)
            return
            
        reader = Reader(decompressed)
    except: return

    if old:
        try:
            reader.read(17)
            count = reader.read_uint16()
            reader.read(count * 2)
            for i in range(count): reader.read_string()
        except: pass

    count = 0
    while len(reader) > 5:
        file_type = reader.read_byte()
        file_size = reader.read_uint32()
        if file_size == 0: continue
        tag_data = reader.read(file_size)
        tag_reader = Reader(tag_data)

        if file_type == 45:
            idx = tag_data.find(b"\xab\x4b\x54\x58\x20")
            if idx != -1:
                process_ktx(f"{base_name}_{count}", tag_data[idx:], path)
                count += 1
            continue
        if file_type == 47:
            file_name = tag_reader.read_string()
            if file_name:
                process_file_type_47(os.path.join(base_dir, file_name), path)
            continue
        if file_type not in [1, 24, 27, 28]: continue

        sub_type = tag_reader.read_byte()
        width = tag_reader.read_uint16()
        height = tag_reader.read_uint16()
        if width == 0 or height == 0: continue
        pixel_sz = pixel_size(sub_type)
        img = None
        if file_type in [27, 28]:
            block_sz = 32
            pixels = bytearray(width * height * pixel_sz)
            for _h in range(0, height, block_sz):
                for _w in range(0, width, block_sz):
                    for h in range(_h, _h + block_sz):
                        if h < height:
                            i = (_w + h * width) * pixel_sz
                            sz = min(block_sz, width - _w) * pixel_sz
                            chunk = tag_reader.read(sz)
                            if len(chunk) == sz: pixels[i : i + sz] = chunk
                            if block_sz > (width - _w): tag_reader.read((block_sz - (width - _w)) * pixel_sz)
                        else: tag_reader.read(block_sz * pixel_sz)
            img = create_image(width, height, bytes(pixels), sub_type)
        else:
            pixels = tag_reader.read()
            if len(pixels) >= width * height * pixel_sz:
                img = create_image(width, height, pixels[:width * height * pixel_sz], sub_type)
        if img:
            os.makedirs(path, exist_ok=True)
            img.save(os.path.join(path, f"{base_name}_{count}.png"))
            count += 1


def process_file_type_47(file_path, path):
    if not os.path.isfile(file_path): return
    with open(file_path, "rb") as f: data = f.read()
    if not data: return
    process_sctx(os.path.splitext(os.path.basename(file_path))[0], data, path)


def check_header(data):
    if not data: return None
    if data[0] == 0x5D: return "csv"
    if data[:2] == b"\x53\x43": return "sc"
    if data[:4] == b"\x53\x69\x67\x3a": return "sig:"
    if data[:5] == b"\xab\x4b\x54\x58\x20": return "ktx"
    if data[8:12] == b"SCTX": return "sctx"
    return None


def get_group_and_type(filename):
    name, ext = os.path.splitext(filename)
    if name.endswith("_dl"): ftype = "dl_sc"
    elif name.endswith("_tex"): ftype = "tex_sc"
    else: ftype = ext.lstrip(".").lower()
    clean_name = re.sub(r"(_dl|_tex|BG|FG|highres|lowres)$", "", name)
    if clean_name.startswith("chr_"): clean_name = clean_name[4:]
    group = "ui" if clean_name.startswith("ui") else clean_name.split("_")[0]
    return group.lower(), ftype


def process_file(file, path, group_flag, old_flag):
    if os.path.isdir(file): return
    base_dir = os.path.dirname(file)
    filename = os.path.basename(file)
    base_name, _ = os.path.splitext(filename)
    target_path = path
    if group_flag:
        group, ftype = get_group_and_type(filename)
        target_path = os.path.join(path, ftype, group)
    try:
        with open(file, "rb") as f: data = f.read()
        file_type = check_header(data)
        if not file_type: return
        logging.info(f"Processing: {filename} -> {target_path}")
        if file_type == "csv":
            try:
                decompressed = decompress(data)
                os.makedirs(target_path, exist_ok=True)
                with open(os.path.join(target_path, filename), "wb") as f: f.write(decompressed)
            except: pass
        elif file_type == "sig:":
            try:
                decompressed = decompress(data[68:])
                os.makedirs(target_path, exist_ok=True)
                with open(os.path.join(target_path, filename), "wb") as f: f.write(decompressed)
            except: pass
        elif file_type == "sc": process_sc(base_dir, base_name, data, target_path, old_flag)
        elif file_type == "ktx": process_ktx(base_name, data, target_path)
        elif file_type == "sctx": process_sctx(base_name, data, target_path)
    except Exception as e:
        logging.error(f"Failed to process {filename}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--old", action="store_true")
    parser.add_argument("-o", type=str)
    parser.add_argument("--group", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    path = os.path.normpath(args.o) if args.o else os.getcwd()
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if args.verbose else logging.INFO)
    for input_path in args.inputs:
        if os.path.isdir(input_path):
            for root, _, files in os.walk(input_path):
                for file in files: process_file(os.path.join(root, file), path, args.group, args.old)
        else: process_file(input_path, path, args.group, args.old)
