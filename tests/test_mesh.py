"""Tests for mesh.py: MooraMoora channel decoding and the drongo-only /mesh gate.

Envelopes are built field by field here and encrypted with AES-CTR using the
firmware's nonce layout (packet id as u64 LE, sender as u32 LE, zero counter),
independently of mesh.decrypt, so a nonce or key-expansion slip fails the test.
Run: venv/bin/python -m pytest tests/test_mesh.py  (or plain python).
"""
import base64
import os
import struct
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh  # noqa: E402

KEY = bytes(range(32))
FROM, PID = 0x85D7C1D3, 0x1234ABCD


def varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def ld(num, data):
    return varint(num << 3 | 2) + varint(len(data)) + data


def fixed32(num, v):
    return varint(num << 3 | 5) + struct.pack("<I", v)


def envelope(portnum, payload, key=KEY, frm=FROM, pid=PID, plain=False):
    data = varint(1 << 3) + varint(portnum) + ld(2, payload)
    if plain:
        body = ld(4, data)
    else:
        nonce = struct.pack("<QI", pid, frm) + bytes(4)
        enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
        body = ld(5, enc.update(data) + enc.finalize())
    pkt = fixed32(1, frm) + fixed32(2, 0xFFFFFFFF) + body + fixed32(6, pid) + fixed32(7, 1791400000)
    return ld(1, pkt) + ld(2, b"MooraMoora") + ld(3, b"!85d7c1d3")


def test_decrypts_text():
    p = mesh.decode_envelope(envelope(mesh.TEXT_APP, b"hello lodge"), KEY)
    assert p["from"] == FROM and p["id"] == PID and p["payload"] == b"hello lodge"
    assert p["rx_time"] == 1791400000 and p["gateway"] == "!85d7c1d3"


def test_plaintext_packet():
    p = mesh.decode_envelope(envelope(mesh.TEXT_APP, b"clear", plain=True), None)
    assert p["payload"] == b"clear"


def test_wrong_key_rejected():
    p = mesh.decode_envelope(envelope(mesh.TEXT_APP, b"secret words here"), bytes(32))
    assert p is None or p["payload"] != b"secret words here"


def test_default_key_expansion():
    assert mesh.channel_key("AQ==") == mesh.DEFAULT_PSK
    assert mesh.channel_key("Ag==")[-1] == mesh.DEFAULT_PSK[-1] + 1
    assert mesh.channel_key(base64.b64encode(KEY).decode()) == KEY


class FakeMsg:
    def __init__(self, payload):
        self.payload, self.topic = payload, "msh/ANZ/2/e/MooraMoora/!85d7c1d3"


def fresh_mesh(tmp):
    mesh.STATE_FILE = os.path.join(tmp, "state.json")
    m = mesh.Mesh()
    m._key = KEY
    return m


def test_feed_names_dedup_and_persist():
    with tempfile.TemporaryDirectory() as tmp:
        m = fresh_mesh(tmp)
        user = ld(1, b"!85d7c1d3") + ld(2, b"SmartEnergyLab-Base") + ld(3, b"SELb")
        m._on_message(None, None, FakeMsg(envelope(mesh.NODEINFO_APP, user, pid=1)))
        msg = FakeMsg(envelope(mesh.TEXT_APP, b"Test from the M1", pid=2))
        m._on_message(None, None, msg)
        m._on_message(None, None, msg)  # same packet via a second gateway
        s = m.snapshot()
        assert len(s["messages"]) == 1 and s["nodes_24h"] == 1
        assert s["messages"][0]["text"] == "Test from the M1"
        assert s["messages"][0]["long_name"] == "SmartEnergyLab-Base" and s["messages"][0]["short_name"] == "SELb"
        again = fresh_mesh(tmp).snapshot()  # reloaded from the state file after a restart
        assert again["messages"][0]["text"] == "Test from the M1"


def test_mesh_routes_need_token():
    import app
    client = app.app.test_client()
    app.MESH.token = None
    assert client.get("/mesh").status_code == 404  # no token configured: closed
    app.MESH.token = "s3cret"
    assert client.get("/mesh").status_code == 404
    assert client.get("/api/mesh?k=wrong").status_code == 404
    assert client.get("/mesh?k=s3cret").status_code == 200
    r = client.get("/api/mesh?k=s3cret")
    assert r.status_code == 200 and "messages" in r.get_json()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
