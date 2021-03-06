
from datetime import datetime
from enum import IntEnum

import pytz
from web3 import Web3
from web3utils import STRING_ENCODING
from web3utils.hex import is_empty_hex

from ens import abis, main

REGISTRAR_NAME = 'eth'

GAS_DEFAULT = {
    'bid': 500000,
    'reveal': 150000,
    'finalize': 120000,
    }

START_GAS_CONSTANT = 25000
START_GAS_MARGINAL = 39000

MIN_BID = Web3.toWei('0.01', 'ether')
MIN_NAME_LENGTH = 7


class Status(IntEnum):
    '''
    Current status of the auction for a label. For more info:
    http://docs.ens.domains/en/latest/userguide.html#starting-an-auction
    '''
    Open = 0
    Auction = 1
    Owned = 2
    Forbidden = 3
    Reveal = 4
    NotYetAvailable = 5


class Registrar:
    """
    Terminology:
        Name: a fully qualified ENS name, for example: 'tickets.eth'
        Label: a label that the registrar auctions, for example: 'tickets'

    The registrar does not directly manage subdomains multiple layers down, like: 'fotc.tickets.eth'
    """

    def __init__(self, ens):
        self.ens = ens
        self.web3 = ens.web3
        self._coreContract = ens._contract(abi=abis.AUCTION_REGISTRAR)
        # delay generating this contract so that this class can be created before web3 is online
        self._core = None
        self._deedContract = ens._contract(abi=abis.DEED)
        self._short_invalid = True

    def status(self, label):
        return self.entries(label)[0]

    def entries(self, label):
        label = self._to_label(label)
        label_hash = self.ens.labelhash(label)
        return self.entries_by_hash(label_hash)

    def start(self, labels, **modifier_dict):
        if not labels:
            return
        if isinstance(labels, (str, bytes)):
            labels = [labels]
        if not modifier_dict:
            modifier_dict = {'transact': {}}
        if 'transact' in modifier_dict:
            transact_dict = modifier_dict['transact']
            if 'gas' not in transact_dict:
                transact_dict['gas'] = self._estimate_start_gas(labels)
            if transact_dict['gas'] > self._last_gaslimit():
                raise ValueError('There are too many auctions to fit in a block -- start fewer.')
        labels = [self._to_label(label) for label in labels]
        label_hashes = [self.ens.labelhash(label) for label in labels]
        return self.core.startAuctions(label_hashes, **modifier_dict)

    def bid(self, label, amount, secret, **modifier_dict):
        """
        @param label to bid on
        @param amount (in wei) to bid
        @param secret you MUST keep a copy of this to avoid burning your entire bid
        """
        if not modifier_dict:
            modifier_dict = {'transact': {}}
        if 'transact' in modifier_dict:
            transact = modifier_dict['transact']
            self.__default_gas(transact, 'bid')
            if 'value' not in transact:
                transact['value'] = amount
            elif transact['value'] < amount:
                raise UnderfundedBid("Bid of %s ETH was only funded with %s ETH" % (
                                     Web3.fromWei(amount, 'ether'),
                                     Web3.fromWei(transact['value'], 'ether')))
        label = self._to_label(label)
        sender = self.__require_sender(modifier_dict)
        if amount < MIN_BID:
            raise BidTooLow("You must bid at least %s ether" % Web3.fromWei(MIN_BID, 'ether'))
        bid_hash = self._bid_hash(label, sender, amount, secret)
        return self.core.newBid(bid_hash, **modifier_dict)

    def reveal(self, label, amount, secret, **modifier_dict):
        if not modifier_dict:
            modifier_dict = {'transact': {}}
        if 'transact' in modifier_dict:
            self.__default_gas(modifier_dict['transact'], 'reveal')
        sender = self.__require_sender(modifier_dict)
        label = self._to_label(label)
        bid_hash = self._bid_hash(label, sender, amount, secret)
        if not self.core.sealedBids(sender, bid_hash):
            raise InvalidBidHash
        label_hash = self.ens.labelhash(label)
        if isinstance(secret, str):
            secret = secret.encode(STRING_ENCODING)
        secret_hash = self.web3.sha3(secret)
        return self.core.unsealBid(label_hash, amount, secret_hash, **modifier_dict)
    unseal = reveal

    def finalize(self, label, **modifier_dict):
        if not modifier_dict:
            modifier_dict = {'transact': {}}
        if 'transact' in modifier_dict:
            self.__default_gas(modifier_dict['transact'], 'finalize')
        label = self._to_label(label)
        label_hash = self.ens.labelhash(label)
        return self.core.finalizeAuction(label_hash, **modifier_dict)

    def entries_by_hash(self, label_hash):
        '''
        @returns a 5-item collection in this order:
            # Status
            # deed contract
            # registration datetime (in UTC)
            # value held on deposit
            # value of largest bid
        '''
        assert isinstance(label_hash, (bytes, bytearray))
        entries = self.core.entries(label_hash)
        entries[0] = Status(entries[0])
        entries[1] = None if is_empty_hex(entries[1]) else self._deedContract(entries[1])
        close_date = None
        if entries[2]:
            close_date = datetime.fromtimestamp(entries[2], pytz.utc)
        entries[2] = close_date
        return entries

    @property
    def core(self):
        if not self._core:
            self._core = self._coreContract(address=self.ens.owner(REGISTRAR_NAME))
        return self._core

    def __default_gas(self, transact_dict, action):
        if 'gas' not in transact_dict:
            transact_dict['gas'] = GAS_DEFAULT[action]

    def _estimate_start_gas(self, labels):
        return START_GAS_CONSTANT + START_GAS_MARGINAL * len(labels)

    def __require_sender(self, modifier_dict):
        modifier_vals = modifier_dict[list(modifier_dict).pop()]
        if 'from' not in modifier_vals:
            raise TypeError("You must specify the sending account")
        return modifier_vals['from']

    def _last_gaslimit(self):
        last_block = self.web3.eth.getBlock('latest')
        return last_block.gasLimit

    def _bid_hash(self, label, bidder, bid_amount, secret):
        label_hash = self.ens.labelhash(label)
        secret_hash = self.web3.sha3(secret, encoding='bytes')
        bid_hash = self.core.shaBid(label_hash, bidder, bid_amount, secret_hash)
        # deal with web3.py returning a string instead of bytes:
        if isinstance(bid_hash, str):
            bid_hash = Web3.toHex(bid_hash)
            bid_hash = Web3.toAscii(bid_hash)
        return bid_hash

    def _to_label(self, label_or_name):
        '''
        Convert from a name, like 'ethfinex.eth', to a label, like 'ethfinex'
        If name is already a label, this should be a noop, except for converting to a string
        '''
        label = label_or_name
        if isinstance(label, (bytes, bytearray)):
            label = str(label, encoding=main.STRING_ENCODING)
        if '.' in label:
            pieces = label.split('.')
            if len(pieces) != 2:
                raise ValueError(
                        "You must specify a label, like 'tickets' "
                        "or a fully-qualified name, like 'tickets.eth'")
            if pieces[-1] != REGISTRAR_NAME:
                raise ValueError("This interface only manages names under .%s " % REGISTRAR_NAME)
            label = pieces[-2]
        if self._short_invalid and len(label) < MIN_NAME_LENGTH:
            raise InvalidLabel('name %r is too shart' % label)
        return label


class BidTooLow(ValueError):
    pass


class InvalidBidHash(ValueError):
    pass


class InvalidLabel(ValueError):
    pass


class UnderfundedBid(ValueError):
    pass
