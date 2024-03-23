from typing import List, Optional, Set, Iterable, Tuple, Dict, Protocol
from abc import ABC, abstractmethod, abstractproperty

from vllm.utils import Device

class Block(ABC):

    @abstractmethod
    def append_token_ids(self, token_ids: List[int]) -> None:
        pass

    @abstractproperty
    def physical_block_index(self) -> Optional[int]:
        pass

    class Factory(Protocol):
    
        @abstractmethod
        def __call__(
            self,
            prev_block: Optional["Block"],
            token_ids: List[int],
            block_size: int,
            physical_block_index: Optional[int] = None,
        ) -> "Block":
            pass

class BlockAllocator(ABC):
    @abstractmethod
    def allocate_mutable(self, prev_block: Optional[Block]) -> Block:
        pass

    @abstractmethod
    def allocate_immutable(self, prev_block: Optional[Block], token_ids: List[int]) -> Block:
        pass
 
    @abstractmethod
    def free(self, block: Block) -> None:
        pass

    @abstractmethod
    def get_num_free_blocks(self) -> int:
        pass

    @abstractproperty
    def all_block_ids(self) -> frozenset[int]:
        pass

    class NoFreeBlocksError(ValueError):
        pass

    #@abstractmethod
    #def get_operations(self):
    #    pass

#class DeviceAwareBlockAllocator(ABC):
#    @abstractmethod
#    def allocate_mutable(self, prev_block: Optional[Block], device: Device) -> Block:
#        pass
#
#    @abstractmethod
#    def allocate_immutable(self, prev_block: Optional[Block], token_ids: List[int], device: Device) -> Block:
#        pass
# 
#    @abstractmethod
#    def free(self, block: Block) -> None:
#        pass
#
#    @abstractmethod
#    def get_num_free_blocks(self, device: Device) -> int:
#        pass
#
#    #@abstractmethod
#    #def get_operations(self):
#    #    pass