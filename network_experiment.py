"""Collect this Mac's network configuration into the course host model."""

import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from datetime import datetime

import psutil

HERE = Path(__file__).resolve().parent


def command(args):
    """Run a read-only command and retain failures as visible warnings."""
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        print(f"WARNING: {args[0]} exited {result.returncode}")
    return result.stdout


def prefix_length(netmask, version):
    """Convert a contiguous address-form netmask into a prefix length."""
    if netmask is None:
        raise ValueError("missing netmask")

    mask = ipaddress.ip_address(netmask.split("%", 1)[0])
    if mask.version != version:
        raise ValueError("address and netmask versions differ")

    bits = format(int(mask), f"0{mask.max_prefixlen}b")
    if "01" in bits:
        raise ValueError("non-contiguous netmask")
    return bits.count("1")


def hardware_types():
    """Map macOS hardware-port labels to the model's interface types."""
    output = command(["/usr/sbin/networksetup", "-listallhardwareports"])
    types = {}
    label = ""

    for line in output.splitlines():
        if line.startswith("Hardware Port:"):
            label = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            name = line.split(":", 1)[1].strip()
            if "wi-fi" in label.lower():
                types[name] = "wifi"
            elif any(word in label.lower() for word in ("ethernet", "lan")):
                types[name] = "ethernet"
            else:
                types[name] = "other"
    return types


def collect():
    types = hardware_types()
    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    interfaces = []

    # Collect all visible interfaces, including those absent from Settings.
    for name in sorted(set(stats) | set(addresses)):
        status = stats.get(name)
        if status is None:
            print(f"WARNING: {name}: status unavailable; interface omitted")
            continue

        kind = types.get(name, "other")
        if name.startswith("lo"):
            kind = "loopback"
        elif name.startswith(("utun", "bridge", "awdl", "llw", "gif", "stf")):
            kind = "virtual"

        interface = {
            "name": name,
            "type": kind,
            "oper-status": "up" if status.isup else "down",
            "mtu": status.mtu,
            "ipv4": [],
            "ipv6": [],
        }

        for entry in addresses.get(name, []):
            if entry.family == psutil.AF_LINK:
                mac = entry.address.lower().replace("-", ":")
                # Interfaces without a hardware address need no MAC field.
                if mac:
                    interface["mac"] = mac
            elif entry.family in (socket.AF_INET, socket.AF_INET6):
                version = 4 if entry.family == socket.AF_INET else 6
                try:
                    prefix = prefix_length(entry.netmask, version)
                    address = entry.address.split("%", 1)[0]
                    ipaddress.ip_address(address)
                    interface[f"ipv{version}"].append(f"{address}/{prefix}")
                except ValueError as error:
                    print(f"WARNING: {name}: IPv{version}: {error}")

        interfaces.append(interface)

    host = {
        "hostname": socket.gethostname(),
        "os": "macos",
        "collected-at": datetime.now().astimezone().isoformat(),
        "interface": interfaces,
    }

    route = command(["/sbin/route", "-n", "get", "default"])
    gateway = re.search(r"^\s*gateway:\s*(\S+)", route, re.MULTILINE)
    if gateway:
        host["default-gateway"] = gateway.group(1)
    else:
        print("WARNING: no default gateway found")

    dns = command(["/usr/sbin/scutil", "--dns"])
    host["dns-server"] = sorted(set(re.findall(
        r"^\s*nameserver\[\d+\]\s*:\s*(\S+)", dns, re.MULTILINE
    )))

    return {"student-host:host": host}



def compare(intended, observed):
    differences = []

    for field, expected in intended.items():
        if field == "interface":
            continue

        if field not in observed:
            differences.append((field, expected, "<missing>"))
        elif expected != observed[field]:
            differences.append((field, expected, observed[field]))

    # Interface order can change, so match by name rather than position.
    actual_interfaces = {
        item["name"]: item for item in observed.get("interface", [])
    }

    for expected_interface in intended.get("interface", []):
        name = expected_interface["name"]
        actual = actual_interfaces.get(name)

        if actual is None:
            differences.append((f"interface {name}", "present", "<missing>"))
            continue

        for field, expected in expected_interface.items():
            if field == "name":
                continue

            path = f"interface {name} / {field}"
            if field not in actual:
                differences.append((path, expected, "<missing>"))
            elif expected != actual[field]:
                differences.append((path, expected, actual[field]))

    return differences



TOP = "student-host:host"

if __name__ == "__main__":
    from jsonschema import Draft202012Validator
    start = time.perf_counter()
    observed = collect()
    (HERE / "observed.json").write_text(json.dumps(observed, indent=2) + "\n")
    print(f"Collection and save time: {time.perf_counter() - start:.3f} seconds")
    print("Saved observed.json locally; contains private identifiers")
    host = observed[TOP]
    print("Interfaces collected:", len(host["interface"]))
    print("DNS servers collected:", len(host["dns-server"]))

    schema = json.loads((HERE / "host_schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(observed),
                    key=lambda e: tuple(str(p) for p in e.absolute_path))
    print("Schema errors:", len(errors))
    for error in errors:
        path = "/".join(str(p) for p in error.absolute_path) or "/"
        print(f"Path: {path} | Failed rule: {error.validator}")
    if errors:
        raise SystemExit("Resolve validation errors before drift comparison")
    print("VALID: observed.json conforms to host_schema.json")

    intended = json.loads((HERE / "intended.json").read_text())[TOP]
    differences = compare(intended, host)
    print("Drift items:", len(differences))
    for field, expected, actual in differences:
        print(f"{field}: intended {expected!r}, observed {actual!r}")
    if not differences:
        print("NO DRIFT in the selected intended fields")
