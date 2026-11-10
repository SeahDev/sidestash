"""Read what the source needs straight out of an .ipa."""
import hashlib
import plistlib
import re
import struct
import zipfile

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
