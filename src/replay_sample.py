import struct
import zlib

from Crypto.Cipher import Blowfish


MAGIC = 0x11343212
HEADER = struct.Struct('<IIf')
KEY = bytes.fromhex('de72bea0de04beb1defebeefdeadbeef')


def decode(file):
    magic, count = struct.unpack_from('<II', file)
    assert magic == MAGIC and count in (1, 2)
    offset = 8
    meta = []
    for _ in range(count):
        length, = struct.unpack_from('<I', file, offset)
        offset += 4
        meta.append(file[offset:offset + length])
        offset += length
    size, packed_size = struct.unpack_from('<II', file, offset)
    cipher = file[offset + 8:]
    blocks = Blowfish.new(KEY, Blowfish.MODE_ECB).decrypt(cipher[:(packed_size + 7)//8*8])
    prev = 0
    plain = bytearray()
    for (value,) in struct.iter_unpack('<Q', blocks):
        prev ^= value
        plain.extend(struct.pack('<Q', prev))
    data = zlib.decompress(plain)
    assert len(data) == size
    packets = []
    pos = 0
    while pos < len(data):
        length, kind, stamp = HEADER.unpack_from(data, pos)
        pos += HEADER.size
        assert pos + length <= len(data)
        packets.append((kind, stamp, data[pos:pos+length]))
        pos += length
    return meta, packets


def encode(meta, packets):
    file = bytearray(struct.pack('<II', MAGIC, len(meta)))
    for blob in meta:
        file.extend(struct.pack('<I', len(blob)) + blob)
    data = b''.join(HEADER.pack(len(payload), kind, stamp) + payload
                    for kind, stamp, payload in packets)
    compressed = zlib.compress(data)
    padded = compressed + bytes(-len(compressed) % 8)
    transformed = bytearray()
    prev = 0
    for (value,) in struct.iter_unpack('<Q', padded):
        transformed.extend(struct.pack('<Q', value ^ prev))
        prev = value
    return bytes(file) + struct.pack('<II', len(data), len(compressed)) + Blowfish.new(KEY, Blowfish.MODE_ECB).encrypt(transformed)
