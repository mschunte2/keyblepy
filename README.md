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

Command output for `--lock` / `--unlock` / `--open`:

```
device locked
device unlocked
device opened
```

(These print only the past-tense action; use `--status` afterwards for
the structured dict if your code needs to parse the result.)

## Status output format

`--status` prints `device status = <dict>` where the dict has three keys:

- `lock_status` -- one of `"UNKNOWN"`, `"MOVING"`, `"UNLOCKED"`,
  `"LOCKED"`, `"OPENED"`. Extracted from the low 3 bits of plaintext
  byte 2, matching the reference JS implementation.
- `raw` -- the full 8-byte decrypted plaintext body of the lock's
  StatusInfoMessage, hex-encoded. Callers that want fields beyond
  `lock_status` (user rights, firmware version, flag bits) can decode
  this themselves. Byte layout, as observed on eQ-3 firmware 16.23:
  `[0]` user-rights+flags, `[1]` flag byte, `[2]` status/flags (low
  3 bits = lock position), `[4]` firmware major, `[5]` firmware minor.
- `counter` -- the peer (lock) security counter of the reply frame.
  Starts at 1 for the first reply in each session and increments per
  message. `_decrypt_received` rejects any frame whose counter is not
  strictly greater than the last accepted one (replay guard).

## Python API

```python
from fsm import Device

device = Device(
    mac="00:CA:FF:EE:DE:AD",
    userid=4,
    userkey=bytes.fromhex("01234567890123456789012345678901"),
    iface=1,              # optional: HCI index, default bluepy picks
    connect_timeout=60,   # optional: bluepy connect timeout (seconds)
    sec_level="medium",   # optional: "low"|"medium"|"high"; "medium"
                          # enables LE encryption from a stored LTK
)

status = device.status(timeout=30.0)
# -> {"lock_status": "UNLOCKED", "raw": "64012a0010170000", "counter": 1}
# or False on timeout / malformed reply.

device.open()       # returns 'open' on success, error string on timeout
device.unlock()     # returns 'unlock'
device.lock()       # returns 'lock'
```

All four operations open a fresh BLE connection on demand and close it
on completion.

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

## Bugs fixed / behaviour changes

The status/command paths were previously unreliable or broken. This
section documents the bugs found and the corresponding fixes, so future
maintainers can understand the delta against the earlier code base.

### Bug: notifications were never enabled on the lock's notify characteristic

**Symptom.** Every high-level call (status, open, lock, unlock) that
expected a reply from the lock hung until the CLI `--timeout` fired.

**Root cause.** `LowerLayer._connect()` resolved the LOCK_SEND and
LOCK_RECV characteristics but never wrote `0x0001` to the
Client Characteristic Configuration Descriptor (CCCD, UUID 0x2902) on
LOCK_RECV. Without that write the lock has no knowledge that any peer
is subscribed, so it never emits a notification. `handleNotification`
therefore never fired and the FSM never advanced past "connected".

**Fix.** `LowerLayer._connect()` now resolves the CCCD via
`getDescriptors(forUUID=0x2902)` and writes `b"\x01\x00"` with response
immediately after characteristic resolution, before firing the
`ev_connected` transition.

### Bug: `Device._on_receive` raised `TypeError` on the first reply

**Symptom.** After a fresh `Device` instance dispatched the first
command, the kernel / BlueZ delivered a notification and `_on_receive`
raised `isinstance() arg 2 must be a type, a tuple of types, or a
union`. Subsequent behaviour was undefined.

**Root cause.** `_on_receive` dispatched on
`isinstance(message, self.msg_type)`. `self.msg_type` is initialised to
`None` in `__init__` and was only set by `wait_for()`. The initial
ConnectionInfoMessage arrived before any code path called `wait_for`,
so the `isinstance` comparison ran with `None` as the type argument and
raised.

**Fix.** Guard the dispatch with
`self.msg_type is not None and isinstance(message, self.msg_type)`. No
behavioural change on the normal path; just prevents the TypeError in
the initial phase.

### Bug: `open() / lock() / unlock()` never called `wait_for()`

**Symptom.** Commands completed *eventually*, but their reply from the
lock was never captured, so callers that depended on the reply (the
planned status-confirmation path) had no way to observe it. Combined
with the `_on_receive` bug above, it also meant the lock's reply was
the first message that triggered the TypeError.

**Root cause.** The command methods sent the encrypted
`CommandMessage(COMMAND_*)` PDU and then called `self.wait(timeout)`
without first calling `self.wait_for(StatusInfoMessage)` to register
the expected reply type. The reply would arrive on the notify
characteristic, `_on_receive` would dispatch, and with `msg_type=None`
fail the isinstance check, so `self.msg_pdu` was never populated and
`self.msg.set()` never fired.

**Fix.** Each of `open()`, `unlock()`, `lock()` now calls
`self.wait_for(StatusInfoMessage)` immediately before
`self.ll.send(pdu)`. The reply is captured and the FSM advances
normally.

### Bug: `Device.decrypt_message()` was syntactically broken and unused

**Symptom.** Any caller of `decrypt_message(data)` got a `TypeError`
before doing any work.

**Root cause.** Several issues in one function:
- `data[-6, -4]` -- a tuple index, not a slice. Should have been
  `data[-6:-4]`.
- `unpack_from('>Q', data[-4:])` -- asks for 8 bytes (`>Q`) from a
  4-byte slice; guaranteed exception.
- `self.local_nonce` referenced but never set (only `self.nonce`
  exists).
- `if compute_authentication_value != message_auth` -- compared the
  function object itself, not its return value
  (`computed_authentication_value`).
- `log.info(...)` referenced a module-level logger `log` that does
  not exist in this file; the module logger is `LOG`.

**Fix.** The function is replaced with two helpers:

- `Device._decrypt_received(raw)` -- takes the raw wire bytes of an
  encrypted reply frame, extracts the ciphertext body, validates the
  counter (replay guard, strictly increasing), and XOR-decrypts using
  `crypt_data(cryptdata, msg_type_id, self.nonce, counter, userkey)`.
  Returns the plaintext body bytes, or `None` on malformed / replayed
  frames.
- `Device._parse_lock_status(plaintext)` -- converts the low 3 bits of
  plaintext byte 2 into a human-readable state (`UNKNOWN`, `MOVING`,
  `UNLOCKED`, `LOCKED`, `OPENED`), matching the JS reference.

### Bug: `Device.status()` always returned the literal `"No Status Yet"`

**Symptom.** Calling `status()` either hung (because of the CCCD bug)
or, if notifications reached it somehow, always returned the fixed
string `"No Status Yet"` regardless of the actual lock state.

**Root cause.** The method's tail was:

```python
info = self.decrypt(self.msg_pdu)   # non-existent method
from pprint import pprint
pprint(info)
return "No Status Yet"              # unconditional
```

The `decrypt` call would have failed (no such method), and the return
value was hard-coded.

**Fix.** `status()` now:
1. Calls `self._decrypt_received(getattr(self.msg_pdu, "raw", None))`
   (the `raw` attribute is attached by `LowerLayer` on every received
   message, see next bug).
2. Calls `self._parse_lock_status(plaintext)` on the result.
3. Returns a dict `{"lock_status": str, "raw": str, "counter": int}`,
   or `False` on timeout / malformed reply.

### Bug: raw frame bytes were not retained on decoded messages

**Symptom.** Even once `status()` was fixed, there was no way for it
to get at the encrypted ciphertext of the reply -- `LowerLayer`
discarded the raw bytes after running `message_cls.decode()`.

**Root cause.** `LowerLayer.work()` called `message_cls.decode(message)`
and passed only the decoded object to the `_recv_cb`. The raw frame
(including the ciphertext) was never exposed to higher layers.

**Fix.** After decoding, `LowerLayer` attaches the raw wire bytes to
the decoded message as `message.raw = raw`. `Device._decrypt_received`
pulls those bytes back via `getattr(self.msg_pdu, "raw", None)`.
`try/except` guards the attribute assignment in case a message class
defines `__slots__` or otherwise refuses new attributes.

### Bug: `StatusRequestMessage.encode()` printed to stdout on every call

**Symptom.** Every `--status` invocation produced two extra stdout
lines before the real reply, e.g.

```
2026-04-14 21:26:04.282534
<class 'datetime.datetime'>
device status = {...}
```

These leaked into any program capturing stdout, including a Delta Chat
bot that forwards `subprocess.run(...)` stdout line-by-line as chat
messages.

**Root cause.** `messages.py:287-288` held two unconditional
`print(self.date)` / `print(type(self.date))` debug statements in
`StatusRequestMessage.encode()`.

**Fix.** Both prints removed. `encode()` now only returns the packed
bytes.

### Bug: `ui_status` left non-daemon threads running, so the process
didn't exit

**Symptom.** `keyble.py --status` printed the result line and then
hung until the program-level `--timeout` fired, at which point the
watchdog thread called `os._exit(2)`. Any caller that waited for
the child to exit naturally (e.g. `subprocess.run`) accrued an
unnecessary ~90 s delay.

**Root cause.** `ui_status` printed the status and returned, but
`ui_command` was the only exit path that called `os._exit(0)`.
The worker thread and transitions-machine timers kept the process
alive.

**Fix.** Added `os._exit(0)` at the end of `ui_status` after the
status print, matching the behaviour of `ui_command`.

## Python API additions

Beyond the bug fixes, the public surface gained optional parameters
for selecting the HCI adapter and configuring the BLE link:

- `Device(..., iface=int, connect_timeout=float, sec_level="medium")`
- `LowerLayer(..., iface=int, connect_timeout=float, sec_level=str)`
- CLI: `--iface`, `--connect-timeout`, `--sec-level`

All of these default to `None`, so existing callers continue to work
without any change. Passing `sec_level="medium"` together with a
BlueZ-stored LTK (i.e. after `bluetoothctl pair + trust`) is the
supported fast path (~7 s per command on a Pi Zero 2 W).

## Wireshark dissector

The wireshark dissector is written in lua and can be loaded via cmdline

`wireshark -X lua_script:wireshark-evlock.lua`

It has been tested with bluetooth captures from Android (btsnoop\_hci.log).
Enable bluetooth hci snoop log and copy it to your wireshark host.

The dissector supports only unfragmented frames. For encrypted packages only
the message name is shown.
