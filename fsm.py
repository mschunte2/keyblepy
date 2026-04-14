#!/usr/bin/env python3
#
# 2019 Alexander 'lynxis' Couzens <lynxis@fe80.eu>
# GPLv3

import logging
import threading
from exceptions import *
from messages import *
from encrypt import encrypt_message
from struct import pack, unpack
import random
from lowerlayer import LowerLayer
from bluepy.btle import Peripheral, BTLEException
from transitions import Machine
from transitions.extensions.states import add_state_features, Timeout

from datetime import datetime

LOCK_SERVICE = '58e06900-15d8-11e6-b737-0002a5d5c51b'
LOCK_SEND_CHAR = '3141dd40-15db-11e6-a24b-0002a5d5c51b'
LOCK_RECV_CHAR = '359d4820-15db-11e6-82bd-0002a5d5c51b'

COMMAND_LOCK = 0
COMMAND_UNLOCK = 1
COMMAND_OPEN = 2

LOG = logging.getLogger("fsm")

@add_state_features(Timeout)
class TimeoutMachine(Machine):
    pass

class Device(object):
    states = [
            { 'name': 'disconnected'}, # complete disconnected
            { 'name': 'connected'}, # connected on the BLE level (connection oriented}
            { 'name': 'exchanged_nonce'}, # do authentication
            { 'name': 'secured'}, # on successful auth
            { 'name': 'unsecured'}, # on failed auth
            { 'name': "action"},
    ]

    transitions = [
        {
            'trigger': 'ev_connected',
            'source': 'disconnected',
            'dest': 'connected',
        },
        {
            'trigger': 'ev_nonce_received',
            'source': 'connected',
            'dest': 'exchanged_nonce',
        },
        {
            'trigger': 'ev_secured',
            'source': 'authenticate',
            'dest': 'secured',
        },
    ]

    def __init__(self, mac, userid, userkey=None, iface=None, connect_timeout=None, sec_level=None):
        """Construct a KeyBLE Device client.

        :param mac:             lock MAC address ("XX:XX:XX:XX:XX:XX")
        :param userid:          app-level user id (0..255) registered on the lock
        :param userkey:         16-byte shared secret for this user (bytes/bytearray)
        :param iface:           HCI interface index (e.g. 1 for hci1). Defaults to
                                whichever adapter bluepy picks (usually hci0).
        :param connect_timeout: seconds to wait for the LE connection to complete
                                before giving up. None uses bluepy-helper default.
        :param sec_level:       BlueZ security level for the GATT link -- one of
                                "low", "medium", "high". Use "medium" when the lock
                                has been BLE-bonded (via `bluetoothctl pair`) so
                                that the link is encrypted with the stored LTK.
                                Required for bonded operation; on an unbonded lock
                                the default (None / "low") is correct.
        """
        # should it raise Exception on invalid data?
        self.ignore_invalid = False
        self.mac = mac
        self.iface = iface
        self.connect_timeout = connect_timeout
        self.sec_level = sec_level
        self.ll = None
        self.machine = TimeoutMachine(self,
                                      states=Device.states,
                                      transitions=Device.transitions,
                                      initial='disconnected')

        self.nonce = int(random.getrandbits(64))
        self.nonce_byte = bytearray(pack('>Q', self.nonce))

        self.remote_nonce = None
        self.remote_nonce_byte = None

        # The connection info
        self.connection_info = None

        self.security_counter = 1
        self.remote_security_counter = 0

        self.userid = userid
        self.userkey = userkey

        self.ready = threading.Event()
        self.ready.clear()

        # wait for a message
        self.msg = threading.Event()
        self.msg.clear()
        self.msg_type = None
        self.msg_pdu = None

    def _on_error(self, message):
        """ entrypoint when received an error from the lower layer """
        LOG.info("Receive error from lower layer %s", message)

    def _on_receive(self, message):
        """ entrypoint when received a message from the lower layer """
        LOG.info("Receive message %s", message)
        if isinstance(message, ConnectionInfoMessage):
            LOG.info("Receive ConnectionInfoMessage")
            self.remote_nonce = message.remote_session_nonce
            self.remote_nonce_byte = bytearray(pack('>Q', self.remote_nonce))
            self.connection_info = message
            if self.userid == 0xff:
                LOG.info("Using new Userid %d" % message.userid)
                self.userid = message.userid
            self.ev_nonce_received()
        elif isinstance(message, AnswerWithSecurity):
            pass
        elif message==None:
            pass
        elif isinstance(message, AnswerWithoutSecurity):
            pass
        elif self.msg_type is not None and isinstance(message, self.msg_type):
            self.msg_pdu = message
            self.msg.set()
        else:
            LOG.info("Unknown message %s", message)

    def _connect(self):
        if self.state != 'disconnected':
            return

        self.ll = LowerLayer(self.mac, iface=self.iface, connect_timeout=self.connect_timeout, sec_level=self.sec_level)
        self.ll.set_on_receive(self._on_receive)
        self.ll.set_on_error(self._on_error)
        self.ll.connect()
        self.ev_connected()

    def on_enter_connected(self):
        # if userid given, go to the next state
        self.ll.send(ConnectionRequestMessage(self.userid, self.nonce).encode())

    def on_enter_authenticate(self):
        # self.ll.send(Authenticate(self.userid, self.nonce).encode())
        pass

    def on_enter_exchanged_nonce(self):
        LOG.info("Exchanged nonce reached")
        self.ready.set()

    def on_enter_secured(self):
        pass

    def status_on_recv(self):
        pass

    def encrypt_message(self, message):
        """ :param message a Message object
        """
        pdu = encrypt_message(message, self.remote_nonce, self.security_counter, self.userkey)
        self.security_counter += 1
        return pdu

    def _decrypt_received(self, raw):
        """Decrypt an encrypted reply received from the lock.

        Frame layout (notify characteristic payload after fragment header):
            [1 byte msgtype][N byte ciphertext][2 byte counter][4 byte MAC]

        Returns the N-byte plaintext body, or None if the frame is malformed
        or its security counter has already been seen (replay guard).

        crypt_data() is symmetric: it rebuilds the AES-CTR keystream from
        (msgtype, our session nonce, peer counter, user key) and XORs it
        with the ciphertext, producing the plaintext in the same call that
        was used to encrypt on the sending side.
        """
        from encrypt import crypt_data
        if raw is None or len(raw) < 15:
            return None
        msg_type_id = raw[0]
        cryptdata = raw[1:-6]
        counter = unpack(">H", raw[-6:-4])[0]
        if counter <= self.remote_security_counter:
            LOG.info("Stale security counter %d <= %d", counter, self.remote_security_counter)
            return None
        self.remote_security_counter = counter
        return bytes(crypt_data(cryptdata, msg_type_id, self.nonce, counter, bytes(self.userkey)))

    @staticmethod
    def _parse_lock_status(plaintext):
        """Extract a human-readable lock state from a decrypted StatusInfoMessage.

        The lock encodes its current bolt position in the low 3 bits of
        plaintext[2]. Mapping matches the reference JS implementation
        (keyble-node).
        """
        names = {0: "UNKNOWN", 1: "MOVING", 2: "UNLOCKED", 3: "LOCKED", 4: "OPENED"}
        code = plaintext[2] & 0x07
        return names.get(code, "code-%d" % code)

    # interface
    def pair(self, userkey, cardkey):
        """ :param user_key as bytearray (128 bit / 16 byte)
            :param card_Key the key from the card as bytearray (128 bit / 16 byte)

            a userid must be also given via the device class.
            """
        LOG.info("Starting to pair")

        self._connect()
        self.ready.wait()
        LOG.info("userkey: %s %s" % (userkey, str(type(userkey))))
        _userkey = bytearray(userkey)
        _cardkey = bytearray(cardkey)
        self.userkey = _userkey
        pdu = PairingRequestMessage.create(
            self.userid,
            _userkey,
            self.remote_nonce,
            self.security_counter,
            _cardkey).encode()
        self.ll.send(pdu)
        return True

    def wait_for(self, msg_type):
        self.msg_type = msg_type
        self.msg.clear()

    def wait(self, timeout=None):
        self.msg.wait(timeout)

    def discover(self):
        """ return bootloader and application info """
        if self.userid is None:
            raise RuntimeError("Missing user id!")

        self._connect()
        self.ready.wait()
        self.disconnect()
        return {"bootloader": self.connection_info.bootloader,
                "application": self.connection_info.application,}

    def disconnect(self):
        self.ll.disconnect()

    def status(self, timeout=30.0):
        """Query and return the lock's current status.

        Returns a dict::

            {"lock_status": "UNLOCKED"|"LOCKED"|"OPENED"|"MOVING"|"UNKNOWN",
             "raw":         hex-encoded 8-byte plaintext body,
             "counter":     peer security counter of the reply frame}

        Returns False on timeout or on a malformed/replayed reply.
        :param timeout: seconds to wait for the reply after sending the request.
        """
        self.require_autenticate = True

        if self.state == 'disconnected':
            self._connect()

        if not self.ready.wait(timeout):
            LOG.warning("Failed to setup the connection")
            return False

        message = StatusRequestMessage(datetime.now())
        pdu = self.encrypt_message(message)

        self.wait_for(StatusInfoMessage)
        self.ll.send(pdu)
        if self.wait(timeout):
            self.disconnect()
            return "Timeout - failed to get the StatusInfoMessage"

        plaintext = self._decrypt_received(getattr(self.msg_pdu, "raw", None))
        if plaintext is None:
            return False
        return {
            "lock_status": self._parse_lock_status(plaintext),
            "raw": plaintext.hex(),
            "counter": self.remote_security_counter,
        }

    def open(self, timeout=10.0):
        """Send an OPEN command to the lock (motor retracts the bolt fully).

        Connects on demand if disconnected. The lock replies with a
        StatusInfoMessage carrying its new bolt position; the reply is
        captured by wait_for/wait.

        Returns the string 'open' on success, or a human-readable error
        string on timeout. (Callers should treat any string != 'open' as
        failure.) Use status() afterwards if you need the decoded bolt
        position.
        """
        if self.state == 'disconnected':
            self._connect()
            self.ready.wait()

        message = CommandMessage(COMMAND_OPEN)
        pdu = self.encrypt_message(message)
        self.wait_for(StatusInfoMessage)
        self.ll.send(pdu)
        if not self.wait(timeout):
            self.disconnect()
            return "failed to get the Command Open response"

        return 'open'

    def unlock(self, timeout=10.0):
        """Send an UNLOCK command to the lock (retract the bolt).

        See open() for the reply-handling contract. Returns 'unlock' on
        success or an error string on timeout.
        """
        if self.state == 'disconnected':
            self._connect()
            self.ready.wait()

        message = CommandMessage(COMMAND_UNLOCK)
        pdu = self.encrypt_message(message)
        self.wait_for(StatusInfoMessage)
        self.ll.send(pdu)
        if not self.wait(timeout):
            self.disconnect()
            return "failed to get the Command Unlock response"

        return 'unlock'

    def lock(self, timeout=10.0):
        """Send a LOCK command to the lock (engage the bolt).

        See open() for the reply-handling contract. Returns 'lock' on
        success or an error string on timeout.
        """
        if self.state == 'disconnected':
            self._connect()
            self.ready.wait()

        message = CommandMessage(COMMAND_LOCK)
        pdu = self.encrypt_message(message)
        self.wait_for(StatusInfoMessage)
        self.ll.send(pdu)
        if not self.wait(timeout):
            self.disconnect()
            return "failed to get the Command Lock response"

        return 'lock'

    def register(self):
        """ Register a new user to the evlock. It requires the QR code. """
        pass
