#!/usr/bin/env python3
"""Un-tristate the 6 motor pins on a Jetson Orin Nano (JetPack 6). Run as root.

Clears bit 4 (TRISTATE) of each pad's pinmux register. Lost on reboot.
Addresses were verified on this board against
/sys/kernel/debug/pinctrl/2430000.pinmux/pinconf-groups.
"""
import mmap, os, struct

PADS = {
    "pin15 soc_gpio39_pn1": 0x02440020,
    "pin29 soc_gpio32_pq5": 0x02430068,
    "pin31 soc_gpio33_pq6": 0x02430070,
    "pin33 soc_gpio21_ph0": 0x02434040,
    "pin35 soc_gpio44_pi2": 0x024340A0,
    "pin37 spi3_mosi_py2":  0x0243D048,
}
TRISTATE = 1 << 4

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
for name, addr in PADS.items():
    page = addr & ~0xFFF
    m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=page)
    o = addr - page
    v = struct.unpack("<I", m[o:o + 4])[0]
    m[o:o + 4] = struct.pack("<I", v & ~TRISTATE)
    print(f"{name}: 0x{v:08x} -> 0x{v & ~TRISTATE:08x}")
    m.close()
os.close(fd)

# Marker for the agent: /run is cleared on every boot, so this means "fixed since boot".
os.makedirs("/run/rosey", exist_ok=True)
with open("/run/rosey/pinmux.ok", "w") as f:
    f.write("ok\n")
