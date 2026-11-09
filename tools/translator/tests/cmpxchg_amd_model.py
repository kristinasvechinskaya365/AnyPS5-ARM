import json


CONTRACT = "AMD64 CMPXCHG conditional register writes"
VALUES = (0, 1, 0x7f, 0x80, 0x7fff, 0x8000, 0x7fffffff, 0x80000000,
          0x7fffffffffffffff, 0x8000000000000000, 0xffffffffffffffff, 0xa5a55a5affff5501)


def exchange(accumulator, destination, source, bits):
    mask = (1 << bits) - 1
    left, right = accumulator & mask, destination & mask
    difference = (left - right) & mask
    flags = int(left < right)
    flags |= int((difference & 0xff).bit_count() % 2 == 0) << 1
    flags |= int(difference == 0) << 2
    flags |= ((difference >> (bits - 1)) & 1) << 3
    flags |= (((left ^ right) & (left ^ difference)) >> (bits - 1) & 1) << 4
    if left == right:
        destination = (destination & ~mask if bits < 32 else 0) | (source & mask)
    else:
        accumulator = (accumulator & ~mask if bits < 32 else 0) | right
    return accumulator, destination, flags


def reference():
    checksum = 0xcbf29ce484222325
    mask = (1 << 64) - 1
    cases = 0
    for bits in (8, 16, 32, 64):
        for index, accumulator in enumerate(VALUES):
            source = VALUES[(index + 3) % len(VALUES)] ^ mask
            for destination in VALUES:
                for value in exchange(accumulator, destination, source, bits):
                    checksum = ((checksum ^ value) * 0x100000001b3) & mask
                    checksum ^= checksum >> 29
                cases += 1
    return {"schema_version": 1, "contract": CONTRACT, "cases": cases,
            "expected": {"exit_code": checksum & 0x7f, "stdout": f"{checksum:016x}"}}


if __name__ == "__main__":
    print(json.dumps(reference(), sort_keys=True))
