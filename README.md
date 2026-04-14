# KeyBLEpy - a Python implementation to control eq3 Smart Locks via Bluetooth

## Current status

Features                           | supported          | tested
---|---|---
Decrypt Message                    | :heavy_check_mark: | :heavy_check_mark:
Encrypt Message                    | :heavy_check_mark: | :heavy_check_mark:
Timeout on operation               | :heavy_check_mark: | :heavy_check_mark:
Discover BLE devices               | :heavy_check_mark: | :heavy_check_mark:
Registering new user               | :heavy_check_mark: | :heavy_check_mark:
Open/Unlock/Lock                   | :heavy_check_mark: | :heavy_check_mark:
Status                             | :heavy_check_mark: | :heavy_check_mark:
BLE bonded / encrypted connect     | :heavy_check_mark: | :heavy_check_mark:

## Usage

```
python3 keyble.py [options] --status|--lock|--unlock|--open \
    --device <MAC> --user-id <N> --user-key <32-hex>
```

Common options:

- `--device`            Lock MAC (e.g. `00:CA:FF:EE:DE:AD`)
- `--user-id`           User id registered on the lock (0..255)
- `--user-key`          16-byte user key as 32 hex chars
- `--timeout`           Overall wall-clock timeout (seconds). Forces an exit via `os._exit` when reached.
- `--iface`             HCI interface index (e.g. `1` for `hci1`). Default: bluepy picks one.
- `--connect-timeout`   Seconds to wait for the LE connection to complete.
- `--sec-level`         BlueZ link security level: `low` (default), `medium`, `high`. See "Pairing" below.
- `--verbose`           Enable debug logging.

Example (status, bonded + encrypted, using hci1 with a 60s connect budget):

```
python3 keyble.py --device 00:CA:FF:EE:DE:AD \
    --user-id 4 --user-key 01234567890123456789012345678901 \
    --iface 1 --sec-level medium --connect-timeout 60 --timeout 90 \
    --status
```

Sample output:

```
device status = {'lock_status': 'UNLOCKED', 'raw': '64012a0010170000', 'counter': 1}
```

## Pairing and encrypted connections

An unpaired lock accepts GATT reads/writes over an unencrypted link. That
works but is slow on some stacks: the lock sends an SMP Pairing Request
post-connect, and when the host (bluepy / noble) doesn't respond, the lock
waits ~40s before releasing the link for GATT traffic. Connecting via a
bonded + encrypted link avoids this entirely -- our measurements drop from
~45s per command to ~6-8s.

To bond once:

```
# Put the lock in pairing mode (see the lock's manual, typically a
# button sequence on the lock itself).
sudo bluetoothctl
> select <controller-mac>              # pick the HCI you want to bond from
> power on
> agent NoInputNoOutput
> default-agent
> scan le                              # wait until the lock shows up
> pair <lock-mac>
> trust <lock-mac>
> info <lock-mac>                      # should show Paired: yes, Bonded: yes
> exit
```

BlueZ now stores the LTK in `/var/lib/bluetooth/<ctrl>/<lock>/info`. From
this point, passing `--sec-level medium` to keyble.py tells bluepy to
set up LE encryption from the cached LTK as soon as the connection is up,
bypassing the SMP stall.

The bond does not prevent other peers (e.g. the manufacturer's phone app)
from also operating the lock -- the lock holds one bond entry per peer.

To remove the bond:

```
sudo bluetoothctl -- remove <lock-mac>
```

### Recovering from a lost bond

Eqiva locks keep a finite number of BLE bond entries (hardware-dependent,
roughly up to a handful). If another peer pairs and the lock's bond table
is full, your peer can be evicted without warning. Symptoms:

- Commands that previously finished in ~7 s suddenly take 40-90 s again,
  because the lock has reverted to sending SMP Pairing Request post-
  connect (which the bonded fast path was avoiding).
- `bluepy` logs `Failed to connect to peripheral ... addr type: public`
  while the lock still appears in `bluetoothctl scan le`.
- `sudo bluetoothctl -- info <lock-mac>` still shows `Bonded: yes` on
  *our* side (the stored LTK), but encrypting the link no longer works
  because the lock no longer has a matching entry.

Recovery is a full re-pair:

```
# 1. Wipe the stale bond on our side so BlueZ doesn't try to reuse it.
sudo bluetoothctl -- remove <lock-mac>

# 2. Put the lock back in pairing mode (the physical button sequence
#    from the lock manual).

# 3. Pair again exactly as for the first-time setup (see above).
sudo bluetoothctl
> select <controller-mac>
> agent NoInputNoOutput
> default-agent
> scan le
> pair <lock-mac>
> trust <lock-mac>
> exit
```

Once `info <lock-mac>` shows `Paired: yes` / `Bonded: yes` again, the
`--sec-level medium` fast path resumes at its normal ~7 s per command.

If you maintain multiple peers (phone + a Pi, or several Pis) on the
same lock, be aware that bond evictions are driven by the lock, not by
BlueZ. Pairing from a new peer can displace the oldest bond. Pair the
lock with fewer peers, or factory-reset and re-pair all peers in order
of priority, to keep the bond table predictable.

## Wireshark dissector

The wireshark dissector is written in lua and can be loaded via cmdline

`wireshark -X lua_script:wireshark-evlock.lua`

It has been tested with bluetooth captures from Android (btsnoop\_hci.log).
Enable bluetooth hci snoop log and copy it to your wireshark host.

The dissector supports only unfragmented frames. For encrypted packages only
the message name is shown.
