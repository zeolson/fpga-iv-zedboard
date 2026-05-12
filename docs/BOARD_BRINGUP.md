# Zedboard bring-up — concrete steps

## Jumper settings for SD boot

The Zedboard has five mode jumpers JP7-JP11. For booting from SD card:

```
JP7 = GND  (lower)
JP8 = GND  (lower)
JP9 = 3V3  (upper)
JP10 = 3V3 (upper)
JP11 = GND (lower)
```

This is the SD-boot configuration from the Zedboard Hardware User's
Guide (Avnet/Digilent). Out of the factory the jumpers are usually set
for JTAG boot, which won't load PYNQ.

## Flashing the SD card

1. Download `pynq_z1_v2.7.0.img` from https://www.pynq.io/board.html.
   (It's labeled Z1 but the Zedboard variant is in the same release.
   Confirm the image filename mentions "zedboard" — there are two.)
2. Flash with **balenaEtcher**:
   - Pick the .img file
   - Pick your microSD (CHECK THE DRIVE LETTER, you can wipe your laptop)
   - Flash + verify: ~10 minutes
3. Eject cleanly.

## First boot

1. SD card in the slot on the Zedboard (front-facing edge).
2. Ethernet cable: board → router (or board → laptop).
3. Micro-USB cable: board UART connector (PROG, near power) → laptop.
   This is for the serial console only, not for power.
4. 12V barrel jack power.
5. Slide power switch to ON.

LEDs:
- Red POWER: solid red immediately
- Green DONE (LD12, near FPGA): off → solid green after ~30s = bitstream
  loaded. If this never lights, the SD card didn't boot.
- Blue PYNQ heartbeat (LD13): blinks at 1 Hz once Linux is up (~60s).

## Finding the board's IP

**Option A — UART console (most reliable):**
1. Open a serial terminal: PuTTY (Windows), `screen /dev/ttyACM0 115200`
   (Linux/Mac), or VS Code's serial monitor.
2. Settings: 115200 baud, 8 data bits, no parity, 1 stop bit, no flow
   control.
3. Press Enter — you should see a `pynq login:` prompt within 90s of
   power-on. Login `xilinx` / password `xilinx`.
4. `ip a` to see the assigned IP.

**Option B — router DHCP table:**
1. Log into your router admin page.
2. Look for a device named `pynq` or with MAC starting `00:0a:35:...`
   (Xilinx OUI).

**Option C — static fallback:**
If your router didn't lease an IP within 60s, PYNQ falls back to
`192.168.2.99`. Set your laptop's Ethernet to `192.168.2.1/24` and ssh
there.

## Sanity check

```
ssh xilinx@<ip>
# password: xilinx
sudo -i
python3 -c "import pynq; print('PYNQ', pynq.__version__)"
# Expect: PYNQ 2.7.0
```

Then the LED-blink smoke test:

```python
from pynq.overlays.base import BaseOverlay
ol = BaseOverlay("base.bit")
ol.leds[0].on(); time.sleep(0.5); ol.leds[0].off()
```

If LD0 on the board lights up, the PS-PL pipeline works and you can
trust the board for your own bitstream.

## Common boot failures

| Symptom | Cause | Fix |
|---|---|---|
| DONE LED never lights | Wrong jumpers | Recheck JP7-11 against the table above |
| DONE solid, PYNQ never blinks | Corrupted SD | Reflash with balenaEtcher, verify checksum |
| `pynq login:` never appears | Wrong baud rate | 115200, not 9600 |
| `ssh: connection refused` | sshd not running yet | Wait 30 more seconds, retry |
| `sudo: command not found` | Logged in as root already | Drop the `sudo`, you're fine |
| ModuleNotFoundError pynq | Wrong image | You probably grabbed the Pi version. Re-download |
