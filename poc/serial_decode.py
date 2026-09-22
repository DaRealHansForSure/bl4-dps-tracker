"""
A Python port of Nicnl's Borderlands 4 item serial decoder
(github.com/Nicnl/borderlands4-serials, MIT): strip `@U`, base85 with a custom alphabet,
mirror every byte, then read the token stream (a 7-bit header, `100` varints of 4-bit
blocks, `110` varbits, `101` parts, `111` strings, `00` and `01` separators).

Written on 2026-09-22 to test whether the serial (in the item cards, the inventory list
and the item records) holds the seed bytes the mod keys items on. It does not: the first
number is the item type (one to one with the id's first four bytes), the other numbers
never contain the id's remaining eight bytes. Research tooling; not part of the mod.

    python poc/serial_decode.py            decodes the table's serials next to their ids
"""
import json

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{/}~"
INDEX = {c: i for i, c in enumerate(ALPHABET)}
PADDING = ord("~")


def mirror8(b):
    return int(f"{b:08b}"[::-1], 2)


def base85(serial):
    text = serial[2:]
    out = bytearray()
    i = 0
    while i < len(text):
        v = 0
        n = 0
        while i < len(text) and n < 5:
            c = text[i]
            i += 1
            if c in INDEX:
                v = v * 85 + INDEX[c]
                n += 1
        if n == 0:
            break
        if n < 5:
            for _ in range(5 - n):
                v = v * 85 + PADDING
        count = 4 if n == 5 else n - 1
        for k in range(count):
            out.append((v >> (24 - 8 * k)) & 0xFF)
    return bytes(mirror8(b) for b in out)


class Bits:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.total = len(data) * 8

    def read(self):
        if self.pos >= self.total:
            raise EOFError
        b = self.data[self.pos // 8]
        bit = (b >> (7 - self.pos % 8)) & 1
        self.pos += 1
        return bit

    def read_n(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read()
        return v


def mirror_n(value, n):
    return int(f"{value:0{n}b}"[::-1], 2)


def varint(bits):
    out = 0
    shift = 0
    for _ in range(4):
        block = bits.read_n(4)
        out |= mirror_n(block, 4) << shift
        shift += 4
        if bits.read() == 0:
            break
    return out


def varbit(bits):
    length = mirror_n(bits.read_n(5), 5)
    v = 0
    for i in range(length):
        v |= bits.read() << i
    return v


def token(bits):
    t = bits.read_n(2)
    if t == 0:
        return "|"
    if t == 1:
        return ","
    t = (t << 1) | bits.read()
    return {0b100: "varint", 0b110: "varbit", 0b101: "part", 0b111: "string"}[t]


def part(bits):
    index = varint(bits)
    if bits.read() == 1:
        value = varint(bits)
        assert bits.read_n(3) == 0
        return f"{{{index}:{value}}}"
    flag = bits.read_n(2)
    if flag == 0b10:
        return f"{{{index}}}"
    if flag == 0b01:
        assert token(bits) == ","
        values = []
        while True:
            t = token(bits)
            if t == "|":
                break
            values.append(varint(bits) if t == "varint" else varbit(bits))
        return f"{{{index}:[{' '.join(map(str, values))}]}}"
    raise ValueError(f"part flag {flag:02b}")


def decode(serial):
    bits = Bits(base85(serial))
    header = bits.read_n(7)
    assert header == 0b0010000, f"header {header:07b}"
    tokens = []
    while True:
        try:
            t = token(bits)
        except EOFError:
            break
        if t == "|":
            tokens.append("|")
            # the terminator is a "|" followed by padding zeros; keep going while data remains
            if bits.total - bits.pos < 3:
                break
            continue
        if t == ",":
            tokens.append(",")
        elif t == "varint":
            tokens.append(("i", varint(bits)))
        elif t == "varbit":
            tokens.append(("b", varbit(bits)))
        elif t == "part":
            tokens.append(part(bits))
        elif t == "string":
            length = varint(bits)
            tokens.append('"' + "".join(chr(mirror_n(bits.read_n(7), 7)) for _ in range(length)) + '"')
    return tokens


def text(tokens):
    parts = []
    for t in tokens:
        if isinstance(t, tuple):
            parts.append(str(t[1]))
        else:
            parts.append(t)
    return " ".join(parts)


if __name__ == "__main__":
    print(text(decode("@Ugy3L+2}TYg%$yC%i7M2gZldO)@}cgb!l34$a-qf{00")))
    print(text(decode("@Ugr$WBm/$!m!X=5&qXxA;nj3OOD#<4R")))
    table = json.load(open(r"C:\Users\SevenX\AppData\Local\BL4DamageTracker\item-names.json", encoding="utf-8"))
    items = table.get("items") or table.get("names")
    for item_id, info in items.items():
        if not isinstance(info, dict) or not info.get("serial"):
            continue
        seed = bytes.fromhex(item_id)
        type_hash = int.from_bytes(seed[:4], "little")
        rest = seed[4:]
        try:
            tokens = decode(info["serial"])
        except Exception as ex:
            print(item_id[:12], "decode failed:", ex, info["serial"])
            continue
        numbers = [t[1] for t in tokens if isinstance(t, tuple)]
        matches = []
        for value in numbers:
            for width in (4, 8):
                if width * 8 < value.bit_length():
                    continue
                for order in ("little", "big"):
                    raw = value.to_bytes(width, order)
                    if raw in seed or raw[::-1] in seed:
                        matches.append((value, width, order))
            if value == type_hash or value == int.from_bytes(seed[:4], "big"):
                matches.append((value, "type hash"))
        print(f"{item_id[:12]} type {seed[:4].hex()} rest {rest.hex()} | {text(tokens)[:110]} | matches {matches}")
