"""Bounded validation of ComfyUI SaveImage RGB/RGBA PNG output without new dependencies."""

import struct
import zlib


def validate_png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not a PNG")
    position, raster, size, ended = 8, bytearray(), None, False
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position+4], "big")
        kind = data[position+4:position+8]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG")
        chunk = data[position+8:end-4]
        if zlib.crc32(kind + chunk) != int.from_bytes(data[end-4:end], "big"):
            raise ValueError("Invalid PNG CRC")
        if kind == b"IHDR":
            if position != 8 or size is not None or len(chunk) != 13:
                raise ValueError("Invalid PNG header")
            w, h, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if not (0 < w <= 4096 and 0 < h <= 4096 and depth == 8 and color in {2, 6}
                    and compression == filtering == interlace == 0):
                raise ValueError("Unsupported PNG format")
            size = (w, h, 3 if color == 2 else 4)
        elif kind == b"IDAT":
            if size is None:
                raise ValueError("PNG data before header")
            raster.extend(chunk)
        elif kind == b"IEND":
            if length or end != len(data):
                raise ValueError("Invalid PNG ending")
            ended = True
            break
        elif kind[0] < 97:
            raise ValueError("Unknown critical PNG chunk")
        position = end
    if not ended or size is None or not raster:
        raise ValueError("Missing PNG data")
    w, h, channels = size
    row = w * channels + 1
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(raster, row * h + 1)
    if not decoder.eof or decoder.unused_data or len(pixels) != row * h or any(pixels[i] > 4 for i in range(0, len(pixels), row)):
        raise ValueError("Invalid PNG raster")
    return w, h
