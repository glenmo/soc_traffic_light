"""
mesh.py - MooraMoora Meshtastic channel messages for the Lodge kiosk.

Subscribes to the channel's encrypted uplink on the public Meshtastic MQTT
server, decrypts it with the channel key (AES-CTR, as the firmware does) and
keeps the latest text messages plus who has been heard. Only the few protobuf
fields needed are decoded, so there is no dependency on the meshtastic package.

Enabled only when mesh_psk.txt (base64 channel key) exists next to this file.
/mesh and /mesh/data answer only with ?k=<token>, checked against mesh_token.txt:
one viewer per line, "<name> <token>" (or a bare token). It is re-read on every
request, so viewers are added or revoked without a restart.
"""
import base64
import collections
import hmac
import json
import logging
import os
import struct
import threading
import time

log = logging.getLogger("mesh")

HERE = os.path.dirname(os.path.abspath(__file__))
PSK_FILE = os.path.join(HERE, "mesh_psk.txt")
TOKEN_FILE = os.path.join(HERE, "mesh_token.txt")
STATE_FILE = os.path.join(HERE, "mesh_state.json")
BROKER, PORT, USER, PASSWORD = "mqtt.meshtastic.org", 1883, "meshdev", "large4cats"
CHANNEL = "MooraMoora"
TOPIC = f"msh/ANZ/2/e/{CHANNEL}/#"
TEXT_APP, NODEINFO_APP = 1, 4
DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")  # firmware's default key, index 1
MAX_MESSAGES = 30


def read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def token_viewer(k, path=None):
    """Name of the viewer whose token is k, or None. No file, or no match, means no access."""
    try:
        with open(path or TOKEN_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for n, line in enumerate(lines):
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        name, tok = (parts[0], parts[1]) if len(parts) > 1 else (f"viewer{n + 1}", parts[0])
        if k and hmac.compare_digest(k.encode(), tok.encode()):
            return name
    return None


def channel_key(b64):
    """Expand a channel PSK the way the firmware does (1-byte keys index the default key)."""
    k = base64.b64decode(b64)
    if len(k) == 1:
        return DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + k[0] - 1) & 0xFF]) if k[0] else b""
    return k


# ---- minimal protobuf reader -------------------------------------------------
def _varint(b, i):
    v = shift = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << shift
        if c < 0x80:
            return v, i
        shift += 7


def pb_fields(b):
    """{field_number: [value, ...]}: varints as int, fixed32 as 4 raw bytes, LEN as bytes."""
    out, i = collections.defaultdict(list), 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out[num].append(v)
    return out


def _u32(raw):
    return struct.unpack("<I", raw)[0] if raw else None


def decrypt(key, from_num, packet_id, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    nonce = struct.pack("<QI", packet_id, from_num) + b"\0\0\0\0"
    d = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return d.update(data) + d.finalize()


def decode_envelope(payload, key):
    """ServiceEnvelope bytes -> dict with from/id/portnum/payload/rx_time, or None."""
    env = pb_fields(payload)
    if not env.get(1):
        return None
    pkt = pb_fields(env[1][0])
    frm, pid = _u32(pkt.get(1, [None])[0]), _u32(pkt.get(6, [None])[0])
    if frm is None or pid is None:
        return None
    if pkt.get(4):
        data = pkt[4][0]
    elif pkt.get(5) and key:
        data = decrypt(key, frm, pid, pkt[5][0])
    else:
        return None
    try:
        d = pb_fields(data)  # garbage from a wrong key usually fails here
    except (ValueError, IndexError):
        return None
    if not d.get(1):
        return None
    return {"from": frm, "id": pid, "portnum": d[1][0], "payload": (d.get(2) or [b""])[0],
            "rx_time": _u32(pkt.get(7, [None])[0]),
            "gateway": (env.get(3) or [b""])[0].decode(errors="replace")}


# ---- the feed ---------------------------------------------------------------
class Mesh:
    def __init__(self):
        self.enabled = False
        self._lock = threading.Lock()
        self._messages = collections.deque(maxlen=MAX_MESSAGES)
        self._names = {}       # "!xxxxxxxx" -> [long, short]
        self._heard = {}       # "!xxxxxxxx" -> unix time last heard
        self._seen = collections.OrderedDict()
        self._connected = False
        self._load()

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            self._messages.extend(s.get("messages", []))
            self._names.update(s.get("names", {}))
            self._heard.update(s.get("heard", {}))
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"messages": list(self._messages), "names": self._names, "heard": self._heard}, f)
        os.replace(tmp, STATE_FILE)

    def start(self):
        psk = read_secret(PSK_FILE)
        if not psk:
            log.info("mesh: %s missing, MooraMoora feed disabled", PSK_FILE)
            return
        self._key = channel_key(psk)
        self.enabled = True
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"mm-kiosk-{os.getpid()}")
        c.username_pw_set(USER, PASSWORD)
        c.reconnect_delay_set(5, 300)

        def on_connect(cl, u, f, rc, p):
            self._connected = not rc.is_failure
            log.info("mesh: MQTT connected rc=%s, subscribing %s", rc, TOPIC)
            cl.subscribe(TOPIC)

        def on_disconnect(cl, u, f, rc, p):
            self._connected = False
            log.warning("mesh: MQTT disconnected rc=%s", rc)

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, self._on_message
        c.connect_async(BROKER, PORT, keepalive=60)
        c.loop_start()
        self._client = c

    def _on_message(self, cl, u, msg):
        try:
            p = decode_envelope(msg.payload, self._key)
        except Exception as e:
            log.debug("mesh: undecodable %s: %s", msg.topic, e)
            return
        if not p:
            return
        key = (p["from"], p["id"])
        if key in self._seen:  # the same packet arrives once per gateway
            return
        self._seen[key] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)
        nid, now = "!%08x" % p["from"], time.time()
        with self._lock:
            self._heard[nid] = now
            changed = False
            if p["portnum"] == NODEINFO_APP:
                user = pb_fields(p["payload"])
                long_ = (user.get(2) or [b""])[0].decode(errors="replace")
                short = (user.get(3) or [b""])[0].decode(errors="replace")
                if long_ and self._names.get(nid) != [long_, short]:
                    self._names[nid] = [long_, short]
                    changed = True
            elif p["portnum"] == TEXT_APP:
                text = p["payload"].decode(errors="replace").strip()
                if text:
                    self._messages.appendleft({"from": nid, "text": text, "time": p["rx_time"] or now})
                    log.info("mesh: message from %s: %r", nid, text)
                    changed = True
            if changed:
                self._save()

    def snapshot(self):
        now = time.time()
        with self._lock:
            name = lambda n: (self._names.get(n) or [n, n[-4:]])
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "channel": CHANNEL,
                "messages": [{**m, "long_name": name(m["from"])[0], "short_name": name(m["from"])[1]}
                             for m in self._messages],
                "nodes_24h": sum(1 for t in self._heard.values() if now - t < 86400),
                "server_time": now,
            }
