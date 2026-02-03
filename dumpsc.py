#!/usr/bin/env python3

import argparse
import io
import logging
import lzma
import os
import re
import shutil
import zlib
import json
from typing import Optional, List, Tuple, Set, Dict
import multiprocessing
from functools import partial

from PIL import Image, ImageDraw, ImageChops
import texture2ddecoder
import zstandard

try:
    import lzham
except ImportError:
    try:
        import pylzham as lzham
    except ImportError:
        lzham = None


class Reader(io.BytesIO):
    def __init__(self, stream: bytes):
        super().__init__(stream)
        self._bytes_left = len(stream)
        self._bytes_read = 0

    def __len__(self):
        return max(0, self._bytes_left)

    def align_to(self, alignment: int):
        remainder = self._bytes_read % alignment
        if remainder != 0:
            self.read(alignment - remainder)

    def read(self, size: int = -1) -> bytes:
        if size == -1 or size > self._bytes_left:
            size = self._bytes_left
        
        data = super().read(size)
        actual_size = len(data)
        self._bytes_left -= actual_size
        self._bytes_read += actual_size
        return data

    def read_byte(self) -> int:
        res = self.read(1)
        return res[0] if res else 0

    def read_uint16(self, byteorder: str = "little") -> int:
        return int.from_bytes(self.read(2), byteorder) # type: ignore

    def read_int16(self, byteorder: str = "little") -> int:
        return int.from_bytes(self.read(2), byteorder, signed=True) # type: ignore

    def read_int32(self, byteorder: str = "little") -> int:
        return int.from_bytes(self.read(4), byteorder, signed=True) # type: ignore

    def read_uint32(self, byteorder: str = "little") -> int:
        return int.from_bytes(self.read(4), byteorder) # type: ignore

    def read_uint32_big(self) -> int:
        return int.from_bytes(self.read(4), "big")

    def read_string(self, length: Optional[int] = None) -> str:
        if length is None:
            length = self.read_byte()
        if length == 0xFF or length == 0:
            return ""
        data = self.read(length)
        try:
            return data.decode("utf-8")
        except Exception:
            return ""


def decompress(data: bytes) -> Optional[bytes]:
    if not data: return None
    
    def try_decompress(d):
        if len(d) < 5: return None
        
        # SCLZ
        if d[0:4] == b"SCLZ":
            if lzham:
                try:
                    dict_size = d[4]
                    uncompressed_size = int.from_bytes(d[5:9], byteorder="little")
                    return lzham.decompress(d[9:], uncompressed_size, {"dict_size_log2": dict_size})
                except Exception as e:
                    logging.debug(f"SCLZ decompression failed: {e}")
            else:
                logging.warning("SCLZ compression detected but lzham/pylzham not installed")
            return None

        # Zstandard
        if d[0:4] == zstandard.FRAME_HEADER:
            try:
                return zstandard.decompress(d)
            except Exception as e:
                logging.debug(f"Zstd decompression failed: {e}")
                return None

        # Zlib
        if d[0] == 0x78:
            try:
                return zlib.decompress(d)
            except Exception:
                pass

        # LZMA
        try:
            # Supercell LZMA: 5 bytes props, 4 bytes size (LE)
            fixed_data = d[0:9] + (b"\x00" * 4) + d[9:]
            return lzma.LZMADecompressor().decompress(fixed_data)
        except Exception:
            # Fallback for standard LZMA or unknown size
            if d[0] == 0x5D:
                try:
                    fixed_data = d[0:5] + (b"\xff" * 8) + d[9:]
                    return lzma.LZMADecompressor().decompress(fixed_data)
                except Exception:
                    pass
        return None

    res = try_decompress(data)
    if res: return res
    
    # Try skipping 4 bytes (size header)
    if len(data) > 4:
        res = try_decompress(data[4:])
        if res: return res
        
    return None

def find_payload_offset(data: bytes, base_name: str) -> int:
    # Signatures
    sigs = [
        (b"SCLZ", "SCLZ"),
        (b"\x28\xb5\x2f\xfd", "Zstd"),
    ]
    
    for sig, name in sigs:
        idx = data.find(sig, 0, 128) # Search in first 128 bytes
        if idx != -1:
            logging.debug(f"Found {name} signature at offset {idx} for {base_name}")
            return idx
            
    # Fallback to header parsing if no signature found
    if data[:2] == b"SC":
        # Try 16-byte header (HashLen at 12)
        if len(data) > 16:
            hl = int.from_bytes(data[12:16], "big")
            if hl > 0 and hl < 256 and 16 + hl < len(data):
                 return 16 + hl
        # Try 14-byte header (HashLen at 10)
        if len(data) > 14:
            hl = int.from_bytes(data[10:14], "big")
            if hl > 0 and hl < 256 and 14 + hl < len(data):
                 return 14 + hl
        # Try 12-byte header (HashLen at 8)
        if len(data) > 12:
            hl = int.from_bytes(data[8:12], "big")
            if hl > 0 and hl < 256 and 12 + hl < len(data):
                 return 12 + hl
        # Try 10-byte header (HashLen at 6)
        if len(data) > 10:
            hl = int.from_bytes(data[6:10], "big")
            if hl > 0 and hl < 256 and 10 + hl < len(data):
                 return 10 + hl
                 
    return -1

def create_image(width: int, height: int, pixels: bytes, sub_type: int) -> Optional[Image.Image]:
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
                    if ps:
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
                    if ps:
                        ps[w, h] = (((p >> 11) & 0x1F) << 3, ((p >> 5) & 0x3F) << 2, (p & 0x1F) << 3)
            return img
        elif sub_type == 6:  # LA88
            return Image.frombytes("LA", (width, height), pixels)
        elif sub_type == 10:  # L8
            return Image.frombytes("L", (width, height), pixels)
        elif sub_type == 15: # ETC1
             decoded = texture2ddecoder.decode_etc1(pixels, width, height)
             return Image.frombytes("RGBA", (width, height), decoded, "raw", "BGRA")
    except Exception as e:
        logging.debug(f"create_image error: {e}")
    return None


def pixel_size(sub_type: int) -> int:
    if sub_type in [0, 1]: return 4
    if sub_type in [2, 3, 4, 6]: return 2
    if sub_type in [10]: return 1
    if sub_type == 15: return 0 # Special case for ETC1 (4 bits/pixel)
    return 0


ASTC_MAP = {
    0x93B0: (4, 4), 0x93B1: (5, 4), 0x93B2: (5, 5), 0x93B3: (6, 5), 0x93B4: (6, 6),
    0x93B5: (8, 5), 0x93B6: (8, 6), 0x93B7: (8, 8), 0x93B8: (10, 5), 0x93B9: (10, 6),
    0x93BA: (10, 8), 0x93BB: (10, 10), 0x93BC: (12, 10), 0x93BD: (12, 12),
    157: (4, 4), 158: (5, 4), 159: (5, 5), 160: (6, 5), 161: (6, 6),
    162: (8, 5), 163: (8, 6), 164: (8, 8), 165: (10, 5), 166: (10, 6),
    167: (10, 8), 168: (10, 10), 169: (12, 10), 170: (12, 12),
}


def decode_texture(data: bytes) -> Optional[Image.Image]:
    if not data: return None
    
    # Handle SC compression wrapper
    if data[:2] == b"SC":
        try:
            offset = find_payload_offset(data, "texture_wrapper")
            if offset != -1:
                data = decompress(data[offset:])
            else:
                # Try skipping 26 bytes (common wrapper size?)
                data = decompress(data[26:])
        except Exception:
            pass

    if not data: return None
    reader = Reader(data)
    
    # Check for KTX
    if data[:7] == b"\xab\x4b\x54\x58\x20\x31\x31" or data[:7] == b"\xab\x4b\x54\x58\x20\x32\x30":
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
            return None

        pixels = None
        if file_type in ASTC_MAP:
            bw, bh = ASTC_MAP[file_type]
            pixels = texture2ddecoder.decode_astc(image_data, width, height, bw, bh)
        elif file_type == 0x8D64: # ETC1
            pixels = texture2ddecoder.decode_etc1(image_data, width, height)
        
        if pixels:
            return Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
        return None

    # Check for SCTX or ZXTX
    if len(data) > 12 and (data[8:12] == b"SCTX" or data[8:12] == b"ZXTX"):
        try:
            reader.read(48)
            file_type = reader.read_uint32()
            width = reader.read_uint16()
            height = reader.read_uint16()
            some_type = reader.read_uint32()
            reader.read(20)
            key_value_data_size = reader.read_uint32()
            reader.read(key_value_data_size)
            reader.read(52)
            
            if width == 0 or height == 0: return None
            
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
            return Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
        except Exception:
            return None

    return None


def save_texture(base_name: str, data: bytes, path: str, count: int) -> bool:
    img = decode_texture(data)
    if img:
        os.makedirs(path, exist_ok=True)
        save_path = os.path.join(path, f"{base_name}_{count}.png")
        img.save(save_path)
        logging.info(f"Saved texture: {save_path}")
        return True
    return False


def check_header(data: bytes) -> Optional[str]:
    if not data: return None
    if data[0] == 0x5D: return "csv"
    if data[:2] == b"\x53\x43": return "sc"
    if data[:4] == b"\x53\x69\x67\x3a": return "sig:"
    if data[:5] == b"\xab\x4b\x54\x58\x20": return "ktx"
    if data[8:12] == b"SCTX": return "sctx"
    if data[8:12] == b"ZXTX": return "zxtx"
    if data[:8] == b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a": return "png"
    return None

def find_texture_tag_start(reader: Reader, data_len: int) -> int:
    # Scan for a valid texture tag pattern:
    # Tag (1 byte) in [1, 16, 24, 27, 28, 29]
    # Size (4 bytes) < data_len
    # PixelFormat (1 byte) in [0, 1, 2, 3, 4, 6, 10]
    # Width (2 bytes) < 4096
    # Height (2 bytes) < 4096
    
    valid_tags = [1, 16, 24, 27, 28, 29]
    valid_formats = [0, 1, 2, 3, 4, 6, 10]
    
    start_pos = reader.tell()
    
    # Scan up to 5000 bytes
    for i in range(5000):
        if start_pos + i + 10 > data_len: break
        
        reader.seek(start_pos + i)
        tag = reader.read_byte()
        
        if tag in valid_tags:
            size = reader.read_uint32()
            if size < data_len - (start_pos + i):
                fmt = reader.read_byte()
                if fmt in valid_formats:
                    w = reader.read_uint16()
                    h = reader.read_uint16()
                    if 0 < w < 4096 and 0 < h < 4096:
                        # Found it!
                        return start_pos + i
                        
    return -1

def prepare_reader(base_name: str, data: bytes, old: bool) -> Optional[Reader]:
    reader = Reader(data)
    
    # Determine skip logic based on filename
    is_dl = base_name.endswith("_dl")
    is_tex = base_name.endswith("_tex") or base_name.endswith("_highres_tex") or base_name.endswith("_lowres_tex")
    
    tag_start = -1
    
    if is_tex:
        # Texture files usually don't have the sprite header, just tags
        tag_start = 0
    elif is_dl:
        try:
            reader.read(17)
            count = reader.read_uint16()
            reader.read(count * 2)
            for i in range(count): reader.read_string()
            tag_start = reader.tell()
        except Exception:
            pass
    else:
        # Standard SC file, skip sprite header
        try:
            reader.read(12) # Counts
            reader.read(5)  # Padding
            export_count = reader.read_uint16()
            for _ in range(export_count): reader.read_uint16() # Export IDs
            for _ in range(export_count): reader.read_string() # Export Names
            tag_start = reader.tell()
        except Exception:
            pass

    # Validate the found start
    if tag_start != -1:
        reader.seek(tag_start)
        if tag_start + 5 < len(data):
            tag = reader.read_byte()
            size = reader.read_uint32()
            # Basic validation: tag is reasonable, size fits
            if tag < 50 and size < len(data) - tag_start:
                reader.seek(tag_start)
                return reader

    # Fallback to scanning
    logging.info(f"Header skipping failed for {base_name}, scanning for texture tags...")
    reader.seek(0)
    tag_start = find_texture_tag_start(reader, len(data))
    
    if tag_start != -1:
        reader.seek(tag_start)
        logging.info(f"Found texture tags at offset {tag_start}")
        return reader
    
    logging.warning(f"Could not find texture tags in {base_name}")
    return None

def process_sc(base_dir: str, base_name: str, data: bytes, path: str, old: bool):
    try:
        decompressed = None
        if data[:2] == b"SC":
            offset = find_payload_offset(data, base_name)
            if offset != -1:
                decompressed = decompress(data[offset:])
            else:
                decompressed = decompress(data)
        else:
            decompressed = decompress(data)
            
        if not decompressed: 
            logging.warning(f"Decompression failed or returned empty for {base_name}")
            return
        
        # Check if decompressed data is a texture
        tex_type = check_header(decompressed)
        if tex_type in ["ktx", "sctx", "zxtx", "png"]:
             logging.info(f"Decompressed data identified as {tex_type}")
             save_texture(base_name, decompressed, path, 0)
             return
        
        reader = prepare_reader(base_name, decompressed, old)
        if not reader: return

    except Exception as e:
        logging.error(f"Failed to parse SC data for {base_name}: {e}")
        return

    # Check if external texture file exists
    tex_file_exists = False
    if not base_name.endswith("_tex"):
        tex_path = os.path.join(base_dir, f"{base_name}_tex.sc")
        if os.path.exists(tex_path):
            tex_file_exists = True
            logging.info(f"Found external texture file: {tex_path}")

    count = 0
    while len(reader) > 5:
        file_type = reader.read_byte()
        file_size = reader.read_uint32()
        
        if file_type == 0: # TAG_END
             break
             
        if file_size == 0: continue
        
        if file_size > len(reader):
            logging.warning(f"Tag {file_type} size {file_size} exceeds remaining data {len(reader)} in {base_name}. Stopping.")
            break

        tag_data = reader.read(file_size)
        tag_reader = Reader(tag_data)

        if file_type == 45: # KTX
            idx = tag_data.find(b"\xab\x4b\x54\x58\x20")
            if idx != -1:
                if save_texture(base_name, tag_data[idx:], path, count):
                    count += 1
            continue
        if file_type == 47: # SCTX
            file_name = tag_reader.read_string()
            if file_name:
                # Recursively process referenced file
                ref_path = os.path.join(base_dir, file_name)
                if os.path.isfile(ref_path):
                    with open(ref_path, "rb") as f: ref_data = f.read()
                    if ref_data:
                        save_texture(os.path.splitext(os.path.basename(file_name))[0], ref_data, path, 0)
            continue
        
        # Expanded list of texture tags based on XCoder
        # 0, 1, 2, 19, 24, 27, 28, 29 are common raw texture tags
        if file_type not in [0, 1, 2, 19, 24, 27, 28, 29]: 
            logging.debug(f"Skipping tag {file_type} (size {file_size})")
            continue

        if len(tag_data) < 5: continue

        sub_type = tag_reader.read_byte()
        width = tag_reader.read_uint16()
        height = tag_reader.read_uint16()
        
        logging.info(f"Texture found: Tag={file_type}, SubType={sub_type}, W={width}, H={height}")
        
        img = None
        
        # Check for embedded KTX/SCTX (after 5-byte header)
        if len(tag_data) > 17:
             if tag_data[5:12] == b"\xab\x4b\x54\x58\x20\x31\x31" or tag_data[5:12] == b"\xab\x4b\x54\x58\x20\x32\x30":
                 img = decode_texture(tag_data[5:])
             elif tag_data[13:17] == b"SCTX" or tag_data[13:17] == b"ZXTX":
                 img = decode_texture(tag_data[5:])

        # Check for direct KTX/SCTX (at offset 0)
        if img is None and len(tag_data) > 12:
             if tag_data[:7] == b"\xab\x4b\x54\x58\x20\x31\x31" or tag_data[:7] == b"\xab\x4b\x54\x58\x20\x32\x30":
                 img = decode_texture(tag_data)
             elif tag_data[8:12] == b"SCTX" or tag_data[8:12] == b"ZXTX":
                 img = decode_texture(tag_data)

        if img is None:
            if width == 0 or height == 0: continue
            pixel_sz = pixel_size(sub_type)
            
            if pixel_sz == 0 and sub_type != 15:
                logging.warning(f"Unsupported texture type {sub_type} in tag {file_type}")
                continue

            if file_type in [27, 28, 29]: # Block-based textures
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
                # Linear read
                pixels = tag_reader.read()
                
                if sub_type == 15:
                    expected_size = (width * height) // 2
                else:
                    expected_size = width * height * pixel_sz
                
                if len(pixels) < expected_size:
                    if len(pixels) == 0:
                        # Empty texture, likely placeholder
                        pass
                    else:
                        # Try decompressing
                        decompressed = decompress(pixels)
                        if decompressed and len(decompressed) >= expected_size:
                            pixels = decompressed
                        else:
                            logging.warning(f"Texture {count}: Data length {len(pixels)} < Expected {expected_size}. Decompression failed.")

                if len(pixels) >= expected_size:
                    img = create_image(width, height, pixels[:expected_size], sub_type)
                elif len(pixels) > 0:
                    logging.warning(f"Insufficient data for texture {count}: expected {expected_size}, got {len(pixels)}. Tag {file_type}, Type {sub_type}")
        
        if img:
            os.makedirs(path, exist_ok=True)
            save_path = os.path.join(path, f"{base_name}_{count}.png")
            img.save(save_path)
            logging.info(f"Saved texture: {save_path}")
            count += 1
        else:
            if not (len(tag_data) < 20): # Don't warn if it's likely an external texture placeholder
                logging.warning(f"Failed to decode texture {count} (Tag {file_type}, Type {sub_type})")
            
            # Increment count for known sheet tags to maintain alignment
            # Always increment if we found a valid texture tag structure, even if empty
            if file_type in [1, 24, 27, 28, 29]:
                count += 1


def get_clean_base_name(filename: str) -> str:
    name, _ = os.path.splitext(filename)
    # Remove common suffixes
    name = re.sub(r"(_tex|_dl|_highres|_lowres)$", "", name)
    # Repeat to handle combinations like _highres_tex
    name = re.sub(r"(_tex|_dl|_highres|_lowres)$", "", name)
    # Remove _0, _1 suffixes if they exist (from previous dumps)
    name = re.sub(r"_\d+$", "", name)
    return name

def get_group_and_type(filename: str) -> Tuple[str, str]:
    name, ext = os.path.splitext(filename)
    if name.endswith("_dl"): ftype = "dl_sc"
    elif name.endswith("_tex"): ftype = "tex_sc"
    else: ftype = ext.lstrip(".").lower()
    clean_name = re.sub(r"(_dl|_tex|BG|FG|highres|lowres)$", "", name)
    if clean_name.startswith("chr_"): clean_name = clean_name[4:]
    group = "ui" if clean_name.startswith("ui") else clean_name.split("_")[0]
    return group.lower(), ftype


processed_files: Set[str] = set()

def process_file(file_path: str, output_path: str, group_flag: bool, old_flag: bool):
    abs_path = os.path.abspath(file_path)
    if abs_path in processed_files: return
    processed_files.add(abs_path)

    if os.path.isdir(file_path): return
    base_dir = os.path.dirname(file_path)
    filename = os.path.basename(file_path)
    
    if filename.endswith("_dl.sc"):
        old_flag = True
        logging.info(f"Auto-detected old format for {filename}")

    base_name, _ = os.path.splitext(filename)
    
    # Use clean base name for output folder to consolidate files
    clean_base = get_clean_base_name(filename)
    target_path = os.path.join(output_path, clean_base)
    
    if group_flag:
        group, ftype = get_group_and_type(filename)
        target_path = os.path.join(output_path, ftype, group, clean_base)
    
    if not os.path.exists(target_path):
        os.makedirs(target_path, exist_ok=True)
    
    try:
        shutil.copy2(file_path, os.path.join(target_path, filename))
    except shutil.SameFileError:
        pass
    except Exception as e:
        logging.warning(f"Failed to copy SC file: {e}")

    # Auto-process external texture file if it exists and hasn't been processed
    if filename.endswith(".sc") and not filename.endswith("_tex.sc"):
        for suffix in ["_tex.sc", "_highres_tex.sc", "_lowres_tex.sc"]:
            tex_filename = base_name + suffix
            tex_path = os.path.join(base_dir, tex_filename)
            if os.path.exists(tex_path):
                logging.info(f"Auto-processing external texture file: {tex_filename}")
                process_file(tex_path, output_path, group_flag, old_flag)

    try:
        with open(file_path, "rb") as f: data = f.read()
        file_type_header = check_header(data)
        
        if not file_type_header:
            if filename.endswith(".sc"):
                file_type_header = "sc"
            else:
                return

        logging.info(f"Processing: {filename} -> {target_path} (Type: {file_type_header})")
        
        if file_type_header == "csv":
            try:
                decompressed = decompress(data)
                if decompressed:
                    if filename.endswith(".sc"):
                        # Treat as SC data
                        process_sc(base_dir, base_name, data, target_path, old_flag)
                    else:
                        os.makedirs(target_path, exist_ok=True)
                        with open(os.path.join(target_path, filename), "wb") as f: f.write(decompressed)
            except Exception: pass
        elif file_type_header == "sig:":
            try:
                decompressed = decompress(data[68:])
                if decompressed:
                    os.makedirs(target_path, exist_ok=True)
                    with open(os.path.join(target_path, filename), "wb") as f: f.write(decompressed)
            except Exception: pass
        elif file_type_header == "sc": process_sc(base_dir, base_name, data, target_path, old_flag)
        elif file_type_header == "ktx": save_texture(base_name, data, target_path, 0)
        elif file_type_header == "sctx": save_texture(base_name, data, target_path, 0)
        elif file_type_header == "zxtx": save_texture(base_name, data, target_path, 0)
        elif file_type_header == "png":
            os.makedirs(target_path, exist_ok=True)
            with open(os.path.join(target_path, f"{base_name}_0.png"), "wb") as f: f.write(data)
            logging.info(f"Saved texture: {os.path.join(target_path, f'{base_name}_0.png')}")

    except Exception as e:
        logging.error(f"Failed to process {filename}: {e}")

# Sprite Reconstruction Logic

def find_texture(base_name: str, index: int, texture_dir: str, extra_dir: Optional[str] = None) -> Optional[str]:
    # Cache key
    cache_key = (base_name, index, texture_dir, extra_dir)
    if hasattr(find_texture, "cache") and cache_key in find_texture.cache:
        return find_texture.cache[cache_key]
    
    candidates = [
        f"{base_name}_tex_{index}.png",
        f"{base_name}_{index}.png",
        f"{base_name}_{index}_0.png",
        f"{base_name}_tex_{index}_0.png",
        f"{base_name}_tex.png" if index == 0 else None,
        f"{base_name}.png" if index == 0 else None,
        f"{base_name}_tex_{index}.ktx",
        f"{base_name}_tex_{index}.sctx",
        f"{base_name}_tex_{index}.zxtx",
        f"{base_name}_highres_tex_{index}.png",
        f"{base_name}_lowres_tex_{index}.png"
    ]
    
    search_paths = [texture_dir]
    
    # Add clean base name path
    clean_base = get_clean_base_name(base_name)
    search_paths.append(os.path.join(texture_dir, clean_base))
    
    if extra_dir:
        search_paths.append(extra_dir)
        search_paths.append(os.path.join(extra_dir, f"{base_name}_tex"))
        search_paths.append(os.path.join(extra_dir, f"{base_name}_highres_tex"))
        search_paths.append(os.path.join(extra_dir, f"{base_name}_lowres_tex"))
    
    if base_name.endswith("_dl"):
        clean_base_dl = base_name[:-3]
        search_paths.append(os.path.join(texture_dir, clean_base_dl))
        search_paths.append(os.path.join(texture_dir, f"{clean_base_dl}_dl"))
        
        parent_dir = os.path.dirname(texture_dir)
        search_paths.append(os.path.join(parent_dir, f"{clean_base_dl}_tex"))
        search_paths.append(os.path.join(parent_dir, f"{clean_base_dl}_highres_tex"))
        search_paths.append(os.path.join(parent_dir, f"{clean_base_dl}_lowres_tex"))
        
        for c in [
            f"{clean_base_dl}_tex_{index}.png",
            f"{clean_base_dl}_{index}.png",
            f"{clean_base_dl}_tex.png" if index == 0 else None,
            f"{clean_base_dl}.png" if index == 0 else None,
            f"{clean_base_dl}_highres_tex_{index}.png",
            f"{clean_base_dl}_lowres_tex_{index}.png"
        ]:
            if c: candidates.append(c)
    
    search_paths.append(os.path.join(texture_dir, base_name))
    
    parent_dir = os.path.dirname(texture_dir)
    
    search_paths.append(os.path.join(texture_dir, f"{base_name}_tex"))
    search_paths.append(os.path.join(texture_dir, f"{base_name}_highres_tex"))
    search_paths.append(os.path.join(texture_dir, f"{base_name}_lowres_tex"))
    
    clean_base_regex = re.sub(r"(_BG|_FG|_dl)$", "", base_name)
    if clean_base_regex != base_name:
        search_paths.append(os.path.join(texture_dir, f"{clean_base_regex}_tex"))
        search_paths.append(os.path.join(texture_dir, f"{clean_base_regex}_highres_tex"))
        search_paths.append(os.path.join(texture_dir, f"{clean_base_regex}_lowres_tex"))
        search_paths.append(os.path.join(parent_dir, f"{clean_base_regex}_tex"))

    if os.path.basename(texture_dir) == base_name:
        search_paths.append(parent_dir)
        search_paths.append(os.path.join(parent_dir, f"{base_name}_tex"))
        
    for path in search_paths:
        if not os.path.exists(path): continue
        for c in candidates:
            if not c: continue
            full_path = os.path.join(path, c)
            if os.path.exists(full_path):
                logging.debug(f"Found texture: {full_path}")
                if not hasattr(find_texture, "cache"): find_texture.cache = {}
                find_texture.cache[cache_key] = full_path
                return full_path
    
    if not hasattr(find_texture, "cache"): find_texture.cache = {}
    find_texture.cache[cache_key] = None
    logging.debug(f"Texture not found for {base_name} (index {index}). Searched paths: {search_paths}")
    return None

def path_out(filein: str, output_dir: Optional[str] = None) -> str:
    base_name = os.path.splitext(os.path.basename(filein))[0]
    clean_base = get_clean_base_name(base_name)
    if output_dir:
        pathout = os.path.join(output_dir, clean_base, 'sprites')
    else:
        pathout = os.path.join(os.getcwd(), clean_base + '_out')
    if not (os.path.exists(pathout)):
        os.makedirs(pathout)
    return pathout

def region_rotation(region: dict) -> dict:
    sumSheet = 0
    sumShape = 0
    for z in range(region['NumPoints']):
        p1_sheet = region['SheetPoints'][z]
        p2_sheet = region['SheetPoints'][(z + 1) % (region['NumPoints'])]
        sumSheet += (p2_sheet['x'] - p1_sheet['x']) * (p2_sheet['y'] + p1_sheet['y'])
        
        p1_shape = region['ShapePoints'][z]
        p2_shape = region['ShapePoints'][(z + 1) % (region['NumPoints'])]
        sumShape += (p2_shape['x'] - p1_shape['x']) * (p2_shape['y'] + p1_shape['y'])

    sheetOrientation = -1 if (sumSheet < 0) else 1
    shapeOrientation = -1 if (sumShape < 0) else 1

    region['Mirroring'] = 0 if (shapeOrientation == sheetOrientation) else 1

    if (region['Mirroring'] == 1):
        for x in range(region['NumPoints']):
            region['ShapePoints'][x]['x'] *= -1

    if region['NumPoints'] < 2:
        region['Rotation'] = 0
        return region

    if (region['SheetPoints'][1]['x'] > region['SheetPoints'][0]['x']): px = 'M'
    elif (region['SheetPoints'][1]['x'] < region['SheetPoints'][0]['x']): px = 'L'
    else: px = 'S'

    if (region['SheetPoints'][1]['y'] < region['SheetPoints'][0]['y']): py = 'M'
    elif (region['SheetPoints'][1]['y'] > region['SheetPoints'][0]['y']): py = 'L'
    else: py = 'S'

    if (region['ShapePoints'][1]['x'] > region['ShapePoints'][0]['x']): qx = 'M'
    elif (region['ShapePoints'][1]['x'] < region['ShapePoints'][0]['x']): qx = 'L'
    else: qx = 'S'

    if (region['ShapePoints'][1]['y'] > region['ShapePoints'][0]['y']): qy = 'M'
    elif (region['ShapePoints'][1]['y'] < region['ShapePoints'][0]['y']): qy = 'L'
    else: qy = 'S'

    rotation = 0
    if (px == qx and py == qy): rotation = 0
    elif (px == 'S'):
        if (px == qy):
            if (py == qx): rotation = 90
            elif (py != qx): rotation = 270
        elif (px != qy): rotation = 180
    elif (py == 'S'):
        if (py == qx):
            if (px == qy): rotation = 270
            elif (px != qy): rotation = 90
        elif (py != qx): rotation = 180
    elif (px != qx and py != qy): rotation = 180
    elif (px == py):
        if (px != qx): rotation = 270
        elif (py != qy): rotation = 90
    elif (px != py):
        if (px != qx): rotation = 90
        elif (py != qy): rotation = 270

    if (sheetOrientation == -1 and (rotation == 90 or rotation == 270)):
        rotation = (rotation + 180) % 360

    region['Rotation'] = rotation
    return region

def save_atlas_json(spritedata, sheetdata, base_name, output_path, animations=None, exports=None):
    atlas = {
        "name": base_name,
        "sheets": [],
        "sprites": [],
        "exports": exports or {},
        "animations": animations or []
    }
    
    # Sheets info
    for i, sheet in enumerate(sheetdata):
        atlas["sheets"].append({
            "id": i,
            "file": f"{base_name}_{i}.png",
            "width": sheet['x'],
            "height": sheet['y']
        })

    # Sprites info
    for sprite in spritedata:
        sprite_def = {
            "id": sprite["ID"],
            "regions": []
        }
        for region in sprite["Regions"]:
            # Calculate bounding box on sheet
            if not region['SheetPoints']: continue
            
            min_x = min(p['x'] for p in region['SheetPoints'])
            max_x = max(p['x'] for p in region['SheetPoints'])
            min_y = min(p['y'] for p in region['SheetPoints'])
            max_y = max(p['y'] for p in region['SheetPoints'])
            
            sprite_def["regions"].append({
                "sheet_id": region["SheetID"],
                "x": min_x,
                "y": min_y,
                "width": max_x - min_x,
                "height": max_y - min_y,
                "rotation": region.get('Rotation', 0),
                "mirroring": region.get('Mirroring', 0),
                "points": [{"x": p['x'], "y": p['y']} for p in region['SheetPoints']]
            })
        atlas["sprites"].append(sprite_def)

    try:
        with open(os.path.join(output_path, f"{base_name}.json"), "w") as f:
            json.dump(atlas, f, indent=2)
        logging.info(f"Saved atlas JSON to {os.path.join(output_path, f'{base_name}.json')}")
    except Exception as e:
        logging.error(f"Failed to save atlas JSON: {e}")

def process_single_shape(x, spritedata, sheetimage, spriteglobals, pathout, base_filename, maxrange, export_layers, export_map):
    outImage = Image.new('RGBA', (spriteglobals['SpriteWidth'], spriteglobals['SpriteHeight']), None)
    has_content = False
    
    sprite_id = spritedata[x]['ID']
    export_name = export_map.get(sprite_id, "")
    
    layer_path = ""
    if export_layers:
         layer_path = os.path.join(pathout, f"{base_filename}_sprite_{str(x).rjust(maxrange, '0')}_layers")
         os.makedirs(layer_path, exist_ok=True)

    for y in range(spritedata[x]['TotalRegions']):
        sheetID = spritedata[x]['Regions'][y]['SheetID']
        if sheetID >= len(sheetimage) or sheetimage[sheetID] is None:
            continue

        polygon = []
        for z in range(spritedata[x]['Regions'][y]['NumPoints']):
            polygon.append((spritedata[x]['Regions'][y]['SheetPoints'][z]['x'],
                            spritedata[x]['Regions'][y]['SheetPoints'][z]['y']))

        if not polygon: continue

        if sheetimage[sheetID].width == 1 and sheetimage[sheetID].height == 1:
             continue

        # Optimization: Calculate polygon bbox and create small mask
        min_px = min(p[0] for p in polygon)
        max_px = max(p[0] for p in polygon)
        min_py = min(p[1] for p in polygon)
        max_py = max(p[1] for p in polygon)
        
        bbox = (int(min_px), int(min_py), int(max_px) + 1, int(max_py) + 1)
        bbox = (
            max(0, bbox[0]),
            max(0, bbox[1]),
            min(sheetimage[sheetID].width, bbox[2]),
            min(sheetimage[sheetID].height, bbox[3])
        )
        
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]: continue
        
        regionsize = (bbox[2] - bbox[0], bbox[3] - bbox[1])
        imMask = Image.new('L', regionsize, 0)
        offset_polygon = [(p[0] - bbox[0], p[1] - bbox[1]) for p in polygon]
        ImageDraw.Draw(imMask).polygon(offset_polygon, fill=255)
        
        if not imMask.getbbox(): continue

        tmpRegion = Image.new('RGBA', regionsize, None)
        tmpRegion.paste(sheetimage[sheetID].crop(bbox), None, imMask)
        
        if (spritedata[x]['Regions'][y]['Mirroring'] == 1):
            tmpRegion = tmpRegion.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

        if spritedata[x]['Regions'][y]['Rotation'] != 0:
            tmpRegion = tmpRegion.rotate(spritedata[x]['Regions'][y]['Rotation'], expand=True)

        # Resize to match ShapePoints dimensions
        target_w = int(spritedata[x]['Regions'][y]['SpriteWidth'])
        target_h = int(spritedata[x]['Regions'][y]['SpriteHeight'])
        
        if target_w > 0 and target_h > 0 and (target_w != tmpRegion.width or target_h != tmpRegion.height):
             tmpRegion = tmpRegion.resize((target_w, target_h), Image.Resampling.BILINEAR)

        pasteLeft = spriteglobals['GlobalZeroX'] - spritedata[x]['Regions'][y]['RegionZeroX']
        pasteTop = spriteglobals['GlobalZeroY'] - spritedata[x]['Regions'][y]['RegionZeroY']

        if export_layers:
            layerImage = Image.new('RGBA', (spriteglobals['SpriteWidth'], spriteglobals['SpriteHeight']), None)
            layerImage.paste(tmpRegion, (pasteLeft, pasteTop), tmpRegion)
            layerImage.save(os.path.join(layer_path, f"layer_{y}.png"))

        outImage.paste(tmpRegion, (pasteLeft, pasteTop), tmpRegion)
        has_content = True
        
    if has_content:
        # Crop to content and store anchor
        bbox = outImage.getbbox()
        if bbox:
            cropped = outImage.crop(bbox)
            anchor_x = spriteglobals['GlobalZeroX'] - bbox[0]
            anchor_y = spriteglobals['GlobalZeroY'] - bbox[1]
            
            name_suffix = f"_{export_name}" if export_name else ""
            save_name = f"{base_filename}_sprite_{str(x).rjust(maxrange, '0')}{name_suffix}.png"
            cropped.save(os.path.join(pathout, save_name))
            return 1, cropped, anchor_x, anchor_y
            
    return 0, None, 0, 0

def apply_color_transform(image, ct):
    if not ct: return image
    # ct: [rmul, gmul, bmul, amul, radd, gadd, badd, aadd]
    rm, gm, bm, am, ra, ga, ba, aa = ct
    
    if rm == 255 and gm == 255 and bm == 255 and am == 255 and ra == 0 and ga == 0 and ba == 0 and aa == 0:
        return image

    r, g, b, a = image.split()
    
    def transform(c, mul, add):
        return c.point(lambda i: min(255, max(0, int(i * mul / 255 + add))))

    r = transform(r, rm, ra)
    g = transform(g, gm, ga)
    b = transform(b, bm, ba)
    a = transform(a, am, aa)
    
    return Image.merge("RGBA", (r, g, b, a))

def render_movie_clip_frame(mc, frame_index, sprite_cache, matrices, color_transforms, movie_clips_dict, depth=0):
    if depth > 10: return None
    
    elements_to_render = mc['elements']
    if mc.get('frames') and frame_index < len(mc['frames']):
        start_el = sum(f['elements_count'] for f in mc['frames'][:frame_index])
        end_el = start_el + mc['frames'][frame_index]['elements_count']
        elements_to_render = mc['elements'][start_el:end_el]

    render_elements = []
    min_x, min_y, max_x, max_y = 32767, 32767, -32767, -32767

    for el in elements_to_render:
        inst_idx = el['instance_index']
        inst_id = mc['instances'][inst_idx] if mc['instances'] and inst_idx < len(mc['instances']) else inst_idx
            
        matrix_raw = matrices[el['matrix_index']] if el['matrix_index'] < len(matrices) else [1024, 0, 0, 1024, 0, 0]
        ct = color_transforms[el['color_transform_index']] if el['color_transform_index'] < len(color_transforms) else None
        
        a, b, c, d, tx, ty = [v / 1024.0 if i < 4 else v / 20.0 for i, v in enumerate(matrix_raw)]
        
        sprite_data = sprite_cache.get(inst_id)
        if sprite_data:
            img, ax, ay = sprite_data
            render_elements.append({"type": "img", "data": img, "anchor": (ax, ay), "matrix": (a, b, c, d, tx, ty), "ct": ct})
            # Update bounds
            sw, sh = img.size
            for lx, ly in [(0-ax, 0-ay), (sw-ax, 0-ay), (0-ax, sh-ay), (sw-ax, sh-ay)]:
                x, y = lx * a + ly * c + tx, lx * b + ly * d + ty
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
        else:
            nested_mc = movie_clips_dict.get(inst_id)
            if nested_mc and nested_mc['id'] != mc['id']:
                nested_img = render_movie_clip_frame(nested_mc, 0, sprite_cache, matrices, color_transforms, movie_clips_dict, depth + 1)
                if nested_img:
                    render_elements.append({"type": "mc", "data": nested_img, "anchor": (0,0), "matrix": (a, b, c, d, tx, ty), "ct": ct})
                    nw, nh = nested_img.size
                    for lx, ly in [(0,0), (nw,0), (0,nh), (nw,nh)]:
                        x, y = lx * a + ly * c + tx, lx * b + ly * d + ty
                        min_x, min_y, max_x, max_y = min(min_x, x), min(min_y, y), max(max_x, x), max(max_y, y)

    if not render_elements: return None
    
    width, height = int(max_x - min_x) + 4, int(max_y - min_y) + 4
    if width <= 0 or height <= 0 or width > 8192 or height > 8192: return None
    
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    
    for el in render_elements:
        a, b, c, d, tx, ty = el['matrix']
        img = el['data']
        ax, ay = el['anchor']
        
        # Apply Color Transform
        img = apply_color_transform(img, el['ct'])
        
        # Apply Affine Transform
        # PIL transform matrix is (a, b, c, d, e, f) mapping (x,y) to (ax+by+c, dx+ey+f)
        # We want to map screen (sx, sy) to local (lx, ly)
        # sx = lx*a + ly*c + tx -> lx = ...
        # Simplified: use resize and paste for now, but account for anchor
        target_w, target_h = int(img.width * abs(a)), int(img.height * abs(d))
        if target_w > 0 and target_h > 0:
            img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
        
        # Visual position of anchor (tx, ty) relative to canvas top-left (min_x, min_y)
        px = (tx - ax * a - ay * c) - min_x
        py = (ty - ax * b - ay * d) - min_y
        
        canvas.paste(img, (int(px), int(py)), img)
            
    return canvas

def WriteShape(spritedata: List[dict], sheetdata: List[dict], ShapeCount: int, TotalsTexture: int, filein: str, output_dir: Optional[str] = None, texture_dir: Optional[str] = None, input_dir: Optional[str] = None, export_layers: bool = False, animations=None, export_map=None, matrices=None, color_transforms=None, do_renders=False):
    pathout = path_out(filein, output_dir)
    maxLeft, maxRight, maxAbove, maxBelow = 0, 0, 0, 0
    
    for x in range(ShapeCount):
        for y in range(spritedata[x]['TotalRegions']):
            for z in range(spritedata[x]['Regions'][y]['NumPoints']):
                tmpX, tmpY = spritedata[x]['Regions'][y]['ShapePoints'][z]['x'], spritedata[x]['Regions'][y]['ShapePoints'][z]['y']
                spritedata[x]['Regions'][y]['Top'] = max(spritedata[x]['Regions'][y]['Top'], tmpY)
                spritedata[x]['Regions'][y]['Left'] = min(spritedata[x]['Regions'][y]['Left'], tmpX)
                spritedata[x]['Regions'][y]['Bottom'] = min(spritedata[x]['Regions'][y]['Bottom'], tmpY)
                spritedata[x]['Regions'][y]['Right'] = max(spritedata[x]['Regions'][y]['Right'], tmpX)

            spritedata[x]['Regions'][y] = region_rotation(spritedata[x]['Regions'][y])
            r = spritedata[x]['Regions'][y]
            if r['Rotation'] in [90, 270]:
                r['SpriteWidth'], r['SpriteHeight'] = r['Right'] - r['Left'], r['Top'] - r['Bottom'] # Simplified
            else:
                r['SpriteWidth'], r['SpriteHeight'] = r['Right'] - r['Left'], r['Top'] - r['Bottom']

            try: r['RegionZeroX'] = int(round(abs(r['Left'])))
            except: r['RegionZeroX'] = 0
            try: r['RegionZeroY'] = int(round(abs(r['Bottom'])))
            except: r['RegionZeroY'] = 0

            maxLeft, maxAbove = max(maxLeft, r['RegionZeroX']), max(maxAbove, r['RegionZeroY'])
            maxRight, maxBelow = max(maxRight, r['SpriteWidth'] - r['RegionZeroX']), max(maxBelow, r['SpriteHeight'] - r['RegionZeroY'])

    spriteglobals = {'SpriteWidth': maxLeft + maxRight + 2, 'SpriteHeight': maxAbove + maxBelow + 2, 'GlobalZeroX': maxLeft, 'GlobalZeroY': maxAbove}
    maxrange = len(str(ShapeCount))

    sheetimage = []
    base_filename = os.path.splitext(os.path.basename(filein))[0]
    search_dir = texture_dir if texture_dir else os.getcwd()
    
    for x in range(TotalsTexture):
        img_path = find_texture(base_filename, x, search_dir, input_dir)
        if img_path:
            try:
                img = Image.open(img_path).convert('RGBA')
                img.load() # Force load to avoid lazy reading spam
                sheetimage.append(img)
            except: sheetimage.append(None)
        else: sheetimage.append(Image.new('RGBA', (1, 1), (255, 0, 255, 255)))

    sprite_cache = {}
    saved_count = 0
    for x in range(ShapeCount):
        success, img, ax, ay = process_single_shape(x, spritedata, sheetimage, spriteglobals, pathout, base_filename, maxrange, export_layers, export_map or {})
        if success:
            saved_count += 1
            sprite_cache[spritedata[x]['ID']] = (img, ax, ay)

    if do_renders and animations:
        render_path = os.path.join(os.path.dirname(pathout), 'renders')
        os.makedirs(render_path, exist_ok=True)
        mc_dict = {mc['id']: mc for mc in animations}
        mc_render_cache = {}

        for mc in animations:
            if mc['name']:
                logging.info(f"Rendering MovieClip: {mc['name']}")
                img = render_movie_clip_frame(mc, 0, sprite_cache, matrices, color_transforms, mc_dict)
                if img: img.save(os.path.join(render_path, f"{mc['name']}.png"))

    logging.info(f"Reconstructed sprites for {filein}. Saved {saved_count} sprites.")
    save_atlas_json(spritedata, sheetdata, base_filename, pathout, animations, export_map)

def process_sc_sprites(filein: str, output_dir: Optional[str] = None, export_layers: bool = False, do_renders: bool = False):
    try:
        with open(filein, 'rb') as f: data = f.read()
    except: return

    if data[:2] == b"SC":
        offset = find_payload_offset(data, os.path.basename(filein))
        data = decompress(data[offset:]) if offset != -1 else decompress(data)
    if not data: return

    Stream = Reader(data)
    stream_data = Stream.getvalue()
    
    def is_valid_header(pos):
        if pos + 17 > len(stream_data): return False
        if stream_data[pos+12:pos+17] != b'\x00\x00\x00\x00\x00': return False
        sc = int.from_bytes(stream_data[pos:pos+2], 'little')
        tt = int.from_bytes(stream_data[pos+4:pos+6], 'little')
        return 0 < sc < 5000 and tt < 100

    header_found = False
    for i in range(min(len(stream_data), 20000)):
        if is_valid_header(i):
            Stream.seek(i)
            header_found = True
            break
    if not header_found: Stream.seek(0)

    ShapeCount, TotalsAniamtions, TotalsTexture, TextFieldCount, MatrixCount, ColorTransformationCount = [Stream.read_uint16() for _ in range(6)]
    sheetdata = [{'x': 0, 'y': 0} for _ in range(TotalsTexture)]
    spritedata = [{'ID': 0, 'TotalRegions': 0, 'Regions': []} for _ in range(ShapeCount)]

    Stream.read(5)
    ExportCount = Stream.read_uint16()
    ExportIDs = [Stream.read_uint16() for _ in range(ExportCount)]
    ExportNames = [Stream.read_string() for _ in range(ExportCount)]
    export_map = dict(zip(ExportIDs, ExportNames))

    OffsetShape, OffsetSheet = 0, 0
    base_filename = os.path.splitext(os.path.basename(filein))[0]
    search_dir = output_dir if output_dir else os.getcwd()
    input_dir = os.path.dirname(filein)
    matrices, color_transforms, movie_clips = [], [], []

    while len(Stream) != 0:
        tag, size = Stream.read(1).hex(), Stream.read_uint32()
        if size == 0: continue
        block_reader = Reader(Stream.read(size))

        if tag in ["01", "18"]:
            block_reader.read_byte()
            if OffsetSheet < len(sheetdata):
                sheetdata[OffsetSheet]['x'], sheetdata[OffsetSheet]['y'] = block_reader.read_uint16(), block_reader.read_uint16()
            OffsetSheet += 1
        elif tag == "08": matrices.append([block_reader.read_int32() for _ in range(6)])
        elif tag == "09": color_transforms.append([block_reader.read_byte() for _ in range(8)])
        elif tag in ["03", "0e", "23", "31"]:
            mc_id, frame_rate, frame_count = block_reader.read_uint16(), block_reader.read_byte(), block_reader.read_uint16()
            el_count = block_reader.read_uint32()
            if el_count > 10000: block_reader.seek(block_reader.tell()-4); el_count = block_reader.read_uint16(); block_reader.read_uint16()
            elements = [{"instance_index": block_reader.read_uint16(), "matrix_index": block_reader.read_uint16(), "color_transform_index": block_reader.read_uint16()} for _ in range(el_count)]
            inst_count = block_reader.read_uint16()
            instances = [block_reader.read_uint16() for _ in range(inst_count)]
            frames = [{"elements_count": block_reader.read_uint16()} for _ in range(block_reader.read_uint16() if block_reader._bytes_left >= 2 else 0)]
            movie_clips.append({"id": mc_id, "frame_rate": frame_rate, "frame_count": frame_count, "elements": elements, "instances": instances, "frames": frames, "name": export_map.get(mc_id, "")})
        elif tag == "12":
            if OffsetShape >= len(spritedata): spritedata.append({'ID': 0, 'TotalRegions': 0, 'Regions': []})
            spritedata[OffsetShape]['ID'], spritedata[OffsetShape]['TotalRegions'] = block_reader.read_uint16(), block_reader.read_uint16()
            block_reader.read_uint16()
            spritedata[OffsetShape]['Regions'] = [{'SheetID': 0, 'NumPoints': 0, 'Rotation': 0, 'Mirroring': 0, 'ShapePoints': [], 'SheetPoints': [], 'SpriteWidth': 0, 'SpriteHeight': 0, 'RegionZeroX': 0, 'RegionZeroY': 0, 'Top': -32767, 'Left': 32767, 'Bottom': 32767, 'Right': -32767} for _ in range(spritedata[OffsetShape]['TotalRegions'])]
            for y in range(spritedata[OffsetShape]['TotalRegions']):
                if block_reader.read(1).hex() == "16":
                    block_reader.read_uint32()
                    sid, num_points = block_reader.read_byte(), block_reader.read_byte()
                    spritedata[OffsetShape]['Regions'][y]['SheetID'], spritedata[OffsetShape]['Regions'][y]['NumPoints'] = sid, num_points
                    spritedata[OffsetShape]['Regions'][y]['ShapePoints'] = [{'x': block_reader.read_int32(), 'y': block_reader.read_int32()} for _ in range(num_points)]
                    img_path = find_texture(base_filename, sid, search_dir, input_dir)
                    if sid < len(sheetdata) and sheetdata[sid]['x'] == 0 and img_path:
                        with Image.open(img_path) as img: sheetdata[sid]['x'], sheetdata[sid]['y'] = img.width, img.height
                    scale = 1.0
                    if img_path and sid < len(sheetdata) and sheetdata[sid]['x'] != 0:
                        with Image.open(img_path) as img: scale = img.width / sheetdata[sid]['x']
                    if sid < len(sheetdata) and sheetdata[sid]['x'] != 0:
                        spritedata[OffsetShape]['Regions'][y]['SheetPoints'] = [{'x': int(round(block_reader.read_uint16() * sheetdata[sid]['x'] / 65535 * scale)), 'y': int(round(block_reader.read_uint16() * sheetdata[sid]['y'] / 65535 * scale))} for _ in range(num_points)]
                    else: spritedata[OffsetShape]['Regions'][y]['SheetPoints'] = [{'x': 0, 'y': 0} for _ in range(num_points)]
            OffsetShape += 1

    WriteShape(spritedata, sheetdata, len(spritedata), len(sheetdata), filein, output_dir, search_dir, input_dir, export_layers, movie_clips, export_map, matrices, color_transforms, do_renders)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--old", action="store_true")
    parser.add_argument("-o", type=str)
    parser.add_argument("--group", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("-s", "--sprites", action="store_true")
    parser.add_argument("--sprites-only", action="store_true")
    parser.add_argument("--renders", action="store_true")
    parser.add_argument("--layers", action="store_true")
    args = parser.parse_args()
    
    path = os.path.normpath(args.o) if args.o else os.getcwd()
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if args.verbose else logging.INFO)
    
    all_files = []
    for input_path in args.inputs:
        if os.path.isdir(input_path):
            for root, _, files in os.walk(input_path):
                for file in files:
                    if file.endswith(".sc") or file.endswith(".csv"): all_files.append(os.path.join(root, file))
        else: all_files.append(input_path)

    if not args.sprites_only:
        for full_path in all_files: process_file(full_path, path, args.group, args.old)

    if args.sprites or args.sprites_only or args.renders:
        for full_path in all_files:
            if full_path.endswith(".sc") and not full_path.endswith("_tex.sc"):
                process_sc_sprites(full_path, path, args.layers, args.renders)
