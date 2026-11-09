import struct

from import_fixture import fixture as import_fixture


def check_arguments():
    code = bytearray()
    branches = []
    for instructions, branch in (
        ("833f02", "85"),
        ("48837f0800", "84"),
        ("488b47104885c0", "84"),
        ("80385a", "85"),
        ("80780100", "85"),
        ("48837f1800", "85"),
    ):
        code += bytes.fromhex(instructions + "0f" + branch)
        branches.append(len(code))
        code += bytes(4)
    code += bytes.fromhex("31c0c3")
    failure = len(code)
    code += bytes.fromhex("b801000000c3")
    for offset in branches:
        struct.pack_into("<i", code, offset, failure - offset - 4)
    return code


def fixture():
    image = import_fixture()
    code = bytearray.fromhex("4883ec08e80000000089c7")
    code += b"\xff\x15" + struct.pack("<i", 0x5008 - (0x4000 + len(code) + 6))
    code += b"\x0f\x0b"
    struct.pack_into("<i", code, 5, len(code) - 9)
    code += check_arguments()
    image[0x4000:0x4000 + len(code)] = code
    return image
