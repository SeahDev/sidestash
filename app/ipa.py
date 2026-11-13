"""Read what the source needs straight out of an .ipa."""
import hashlib
import plistlib
import re
import struct
import zipfile
import zlib

INFO_RE = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
CPU_ARM64 = 0x0100000C


class IPAError(Exception):
    pass


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_ipa(path) -> dict:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise IPAError("That file isn't a valid .ipa (not a zip archive).") from e
    with z:
        info_name = next((n for n in z.namelist() if INFO_RE.match(n)), None)
        if not info_name:
            raise IPAError("No Payload/*.app/Info.plist found in the .ipa.")
        info = plistlib.loads(z.read(info_name))
        app_dir = info_name.rsplit("/", 1)[0]

        privacy = {
            k: v for k, v in info.items()
            if isinstance(v, str) and k.startswith("NS") and k.endswith("UsageDescription")
        }

        entitlements = {}
        exe = info.get("CFBundleExecutable")
        if exe:
            try:
                entitlements = macho_entitlements(z.read(f"{app_dir}/{exe}"))
            except (KeyError, struct.error, plistlib.InvalidFileException, ValueError):
                entitlements = {}
        if not entitlements:
            try:
                entitlements = provision_entitlements(z.read(f"{app_dir}/embedded.mobileprovision"))
            except KeyError:
                pass

    return {
        "bundleIdentifier": info.get("CFBundleIdentifier", ""),
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or "",
        "version": str(info.get("CFBundleShortVersionString", "")),
        "buildVersion": str(info.get("CFBundleVersion", "")),
        "minOSVersion": str(info.get("MinimumOSVersion", "")),
        "privacy": privacy,
        "entitlements": sorted(entitlements.keys()),
    }


# ---- entitlements from the Mach-O code signature ----

def macho_entitlements(data: bytes) -> dict:
    magic = struct.unpack(">I", data[:4])[0]
    if magic == 0xCAFEBABE:  # universal binary: pick the arm64 slice
        count = struct.unpack(">I", data[4:8])[0]
        slices = []
        for i in range(count):
            cpu, _sub, off, size, _align = struct.unpack(">iiIII", data[8 + i * 20: 28 + i * 20])
            slices.append((cpu, off, size))
        cpu, off, size = next((s for s in slices if s[0] == CPU_ARM64), slices[0])
        return _thin_entitlements(data[off: off + size])
    return _thin_entitlements(data)


def _thin_entitlements(d: bytes) -> dict:
    if struct.unpack("<I", d[:4])[0] != 0xFEEDFACF:
        return {}
    ncmds = struct.unpack("<I", d[16:20])[0]
    off = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack("<II", d[off: off + 8])
        if cmd == 0x1D:  # LC_CODE_SIGNATURE
            dataoff, datasize = struct.unpack("<II", d[off + 8: off + 16])
            return _superblob_entitlements(d[dataoff: dataoff + datasize])
        off += size
    return {}


def _superblob_entitlements(b: bytes) -> dict:
    if len(b) < 12:
        return {}
    magic, _length, count = struct.unpack(">III", b[:12])
    if magic != 0xFADE0CC0:
        return {}
    for i in range(count):
        slot, off = struct.unpack(">II", b[12 + i * 8: 20 + i * 8])
        if slot == 5:
            m, length = struct.unpack(">II", b[off: off + 8])
            if m == 0xFADE7171:
                return plistlib.loads(b[off + 8: off + length])
    return {}


def provision_entitlements(raw: bytes) -> dict:
    start, end = raw.find(b"<?xml"), raw.find(b"</plist>")
    if start == -1 or end == -1:
        return {}
    return plistlib.loads(raw[start: end + 8]).get("Entitlements", {})


# ---- app icon ----

def _icon_stems(info: dict) -> list[str]:
    """File-name stems Info.plist gives for the primary icon, e.g. 'AppIcon60x60'."""
    stems = []
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        primary = (info.get(key) or {}).get("CFBundlePrimaryIcon") or {}
        stems += primary.get("CFBundleIconFiles") or []
    stems += info.get("CFBundleIconFiles") or []          # pre-iOS 5 keys
    if info.get("CFBundleIconFile"):
        stems.append(info["CFBundleIconFile"])
    return [s.removesuffix(".png") for s in stems if isinstance(s, str) and s]


def extract_icon(path) -> bytes | None:
    """The app's own home-screen icon as a standard PNG, or None.

    Uses the largest loose PNG that Info.plist names as the primary icon (usually
    120 or 152 px). The full-size icon inside Assets.car is in Apple's compressed
    format, which isn't read here.
    """
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    with z:
        names = z.namelist()
        info_name = next((n for n in names if INFO_RE.match(n)), None)
        if not info_name:
            return None
        app_dir = info_name.rsplit("/", 1)[0] + "/"
        stems = _icon_stems(plistlib.loads(z.read(info_name)))
        best = None
        for n in names:
            rest = n[len(app_dir):]
            if not n.startswith(app_dir) or "/" in rest or not rest.lower().endswith(".png"):
                continue
            if not any(rest.startswith(s) for s in stems):
                continue
            data = z.read(n)
            try:
                w, h = _png_size(data)
            except ValueError:
                continue
            if w == h and (best is None or w > best[0]):
                best = (w, data)
    if not best:
        return None
    try:
        return normalize_png(best[1])
    except (ValueError, zlib.error, struct.error):
        return None


def _chunks(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    off = 8
    while off + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[off: off + 8])
        yield kind, data[off + 8: off + 8 + length]
        off += 12 + length
        if kind == b"IEND":
            break


def _png_size(data: bytes) -> tuple[int, int]:
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            return struct.unpack(">II", body[:8])
    raise ValueError("no IHDR")


def normalize_png(data: bytes) -> bytes:
    """Return a PNG any browser can show. Xcode "crushes" many app PNGs into
    Apple's CgBI variant (raw deflate, premultiplied BGRA), which only Apple
    platforms read; those are decoded and re-encoded. Normal PNGs pass through."""
    chunks = list(_chunks(data))
    if not any(kind == b"CgBI" for kind, _ in chunks):
        return data
    ihdr = next(body for kind, body in chunks if kind == b"IHDR")
    w, h, depth, color = struct.unpack(">IIBB", ihdr[:10])
    if depth != 8 or color != 6:
        raise ValueError("unsupported CgBI format")
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"), -15)
    stride, bpp = w * 4, 4
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        ftype, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        row = bytearray([0])
        for x in range(0, stride, 4):
            bl, g, r, al = line[x: x + 4]
            if al and al < 255:   # undo premultiplied alpha
                r, g, bl = min(255, r * 255 // al), min(255, g * 255 // al), min(255, bl * 255 // al)
            row += bytes((r, g, bl, al))
        out += row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(out), 9)) + chunk(b"IEND", b""))
