import enum
from abc import ABC, abstractmethod, abstractproperty
from typing import OrderedDict


class EvictionPolicy(enum.Enum):
    """Enum for eviction policy used by make_evictor to instantiate the correct
       Evictor subclass.
    """
    LRU = enum.auto()


class Evictor(ABC):
    """The Evictor subclasses should be used by the BlockAllocator class to
    handle eviction of freed PhysicalTokenBlocks.
    """

    @abstractmethod
    def __init__(self):
        pass

    @abstractmethod
    def __contains__(self, content_hash: int) -> bool:
        pass

    @abstractmethod
    def evict(self) -> int:
        """Runs the eviction algorithm and returns the evicted block's
        content hash
        """
        pass

    @abstractmethod
    def add(self, content_hash: int, num_hashed_tokens: int,
            last_accessed: int):
        """Adds block to the evictor, making it a candidate for eviction"""
        pass

    @abstractmethod
    def remove(self, content_hash: int):
        """remove block from the evictor of the same hash"""
        pass

    @abstractproperty
    def num_blocks(self) -> int:
        pass


class Block():

    def __init__(self, content_hash: int, num_hashed_tokens: int,
                 last_accessed: int):
        self.content_hash = content_hash
        self.num_hashed_tokens = num_hashed_tokens
        self.last_accessed = last_accessed


class LRUEvictor(Evictor):
    """Evicts in a least-recently-used order using the last_accessed timestamp
    that's recorded in the PhysicalTokenBlock. If there are multiple blocks with
    the same last_accessed time, then the one with the largest num_hashed_tokens
    will be evicted. If two blocks each have the lowest last_accessed time and
    highest num_hashed_tokens value, then one will be chose arbitrarily
    """

    def __init__(self):
        self.free_table: OrderedDict[int, Block] = OrderedDict()

    def __contains__(self, content_hash: int) -> bool:
        return content_hash in self.free_table

    def evict(self) -> int:
        if len(self.free_table) == 0:
            raise ValueError("No usable cache memory left")

        evicted_block = next(iter(self.free_table.values()))
        # The blocks with the lowest timestamps should be placed consecutively
        # at the start of OrderedDict. Loop through all these blocks to
        # find the one with maximum number of hashed tokens.
        for _, block in self.free_table.items():
            if evicted_block.last_accessed < block.last_accessed:
                break
            if evicted_block.num_hashed_tokens < block.num_hashed_tokens:
                evicted_block = block

        self.free_table.pop(evicted_block.content_hash)

        return evicted_block.content_hash

    def add(self, content_hash: int, num_hashed_tokens: int,
            last_accessed: int):
        self.free_table[content_hash] = Block(content_hash, num_hashed_tokens,
                                              last_accessed)

    def remove(self, content_hash: int):
        if content_hash not in self.free_table:
            raise ValueError(
                "Attempting to remove block that's not in the evictor")
        self.free_table.pop(content_hash)

    @property
    def num_blocks(self) -> int:
        return len(self.free_table)


def make_evictor(eviction_policy: EvictionPolicy) -> Evictor:
    if eviction_policy == EvictionPolicy.LRU:
        return LRUEvictor()
    else:
        raise ValueError(f"Unknown cache eviction policy: {eviction_policy}")
