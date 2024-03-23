#"""Token blocks."""
#from typing import List, Optional, Set, Iterable, Tuple, Dict
#from abc import ABC, abstractmethod, abstractproperty
#
#from vllm.utils import Device
#
#_BLANK_TOKEN_ID = -1
#
#DEFAULT_LAST_ACCESSED_TIME = -1
#
#"""
#Missing pieces:
#- CoW
#- Compose NaiveBlock within prefix caching block
#- Integrate into BlockSpaceManager
#    - CoW
#    - Swap
#    - append_slots logistics (who allocates)
#
#Sliding window could maybe be done inside the block
#    (incr refcount of prev block when sliding window -> trigger CoW)
#
#How to get to upper API layer?
#    - start with Allocate
#        Sequence->BlockTable map
#
#"""
#
#class BlockTable:
#    """
#    Each sequence ID has a list of blocks.
#    """
#    pass
#
#class BlockSpaceManager2:
#    def can_allocate(self, seq_group) -> bool:
#        """
#        For each sequence, get number of blocks req
#        Get num free blocks
#        
#
#        """
#        pass
#
#    def allocate(self, seq):
#        pass
#
#
#class BlockSpaceManager:
#
#    def __init__(self):
#        pass
#
#    def can_allocate(self, seq_group) -> bool:
#        """
#        Assume each block in seq will consume a new block
#            (sliding window is less)
#
#        some notion of watermark
#        """
#        pass
#
#    def allocate(self, seq_group) -> None:
#        """
#        For each logical block, allocate a block.
#            sliding window rewrites old
#            store in block table
#
#        duplicate the block table of each sequence to others in seq
#            group
#        """
#
#        """
#        Have scheduler loop over waiting sequences.
#        """
#        pass
#
#    def can_append_slot(self, seq_group) -> None:
#        """
#        Assume each running sequence in a group will require a new block
#        Can we allocate that many blocks ?
#        """
#        pass
#
#    def append_slot(self, seq) -> Optional[Tuple[int, int]]:
#        """
#        if block table is smaller than logical blocks
#            allocate a new one
#                if sliding window use an old one
#                else if block is full, try to get a cached block
#                else if block is not full, get any block
#            check if the last one is "appendable"
#                if refcount == 1, maybe promote the last block
#                if refcount > 1, allocate a new one (maybe via prefix caching)
#            return any CoW
#        """
#        pass
#
#    def fork(self, parent_seq, child_seq) -> None:
#        # called by scheduler::fork_seq
#        """
#        Copy the block table
#        increment refcount of each.
#        """
#        pass
#
#    def can_swap_in(self, seq_group) -> bool:
#        pass
#
#    def swap_in(self, seq_group) -> Dict[int, int]:
#        """
#        for each sequence in the group that is swapped
#            for each cpu block in the block table
#                if the cpu block is scheduled to be copied
#                    increase the refcount
#                    use the destination gpu block
#                else schedule a copy by allocating a gpu block
#            free the cpu block
#
#        return the mapping of cpu block number to gpu block number
#        """
#        pass
#
#    def can_swap_out(self, seq_group) -> bool:
#        pass
#
#    def swap_out(self, seq_group) -> Dict[int, int]:
#        pass
#
#    def free(self, seq) -> None:
#        # called by scheduler::free_seq
#        pass
#
#        """
#        if seq in block tables
#            for each block in the block table
#                free the block (using the appropriate device allocator)
#        """
#
#    def reset(self) -> None:
#        # unused?
#        pass
#
#    def get_block_table(self, seq) -> List[int]:
#        # used to get physical mappings of seq blocks, in scheduler
#        pass
#
#    def get_num_free_gpu_blocks(self) -> int:
#        # used to print stats
#        pass
#
#    def get_num_free_cpu_blocks(self) -> int:
#        # used to print stats
#        pass




"""A block manager that manages token blocks."""
import enum
from itertools import count
from os.path import commonprefix
from typing import Dict, List, Optional, Set, Tuple

from vllm.block import BlockTable, PhysicalTokenBlock
from vllm.sequence import Sequence, SequenceGroup, SequenceStatus
from vllm.utils import Device
from vllm.core.evictor import Evictor, EvictionPolicy, make_evictor
from vllm.core.block.naive_block import NaiveBlockAllocator, NaiveBlock

class AllocStatus(enum.Enum):
    """Result for BlockSpaceManager.can_allocate

    1. Ok: seq_group can be allocated now.
    2. Later: seq_group cannot be allocated.
      The capacity of allocator is larger than seq_group required.
    3. Never: seq_group can never be allocated.
      The seq_group is too large to allocated in GPU.
    """
    OK = enum.auto()
    LATER = enum.auto()
    NEVER = enum.auto()


class BlockSpaceManager:
    """Manages the mapping between logical and physical token blocks."""

    def __init__(
        self,
        block_size: int,
        num_gpu_blocks: int,
        num_cpu_blocks: int,
        watermark: float = 0.01,
        sliding_window: Optional[int] = None,
        enable_caching: bool = False,
    ) -> None:
        self.block_size = block_size
        self.num_total_gpu_blocks = num_gpu_blocks
        self.num_total_cpu_blocks = num_cpu_blocks

        self.block_sliding_window = None
        if sliding_window is not None:
            assert sliding_window % block_size == 0, (sliding_window,
                                                      block_size)
            self.block_sliding_window = sliding_window // block_size

        self.watermark = watermark
        assert watermark >= 0.0

        self.enable_caching = enable_caching

        self.watermark_blocks = int(watermark * num_gpu_blocks)
        self.gpu_allocator = NaiveBlockAllocator(
            block_size=block_size,
            create_block=NaiveBlock,
            # TODO determine number of GPU and CPU blocks separately.
            num_blocks=num_gpu_blocks,
        )

        #self.gpu_allocator = BlockAllocator(Device.GPU,
        #                                    block_size,
        #                                    num_gpu_blocks,
        #                                    enable_caching=enable_caching)
        #self.cpu_allocator = BlockAllocator(Device.CPU,
        #                                    block_size,
        #                                    num_cpu_blocks,
        #                                    enable_caching=enable_caching)
        ## Mapping: seq_id -> BlockTable.
        #self.block_tables: Dict[int, BlockTable] = {}

    def can_allocate(self, seq_group: SequenceGroup) -> AllocStatus:
        # FIXME(woosuk): Here we assume that all sequences in the group share
        # the same prompt. This may not be true for preempted sequences.
        seq = seq_group.get_seqs(status=SequenceStatus.WAITING)[0]
        num_required_blocks = len(seq.logical_token_blocks)

        if self.block_sliding_window is not None:
            num_required_blocks = min(num_required_blocks,
                                      self.block_sliding_window)
        num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks()

        # Use watermark to avoid frequent cache eviction.
        if (self.num_total_gpu_blocks - num_required_blocks <
                self.watermark_blocks):
            return AllocStatus.NEVER
        if num_free_gpu_blocks - num_required_blocks >= self.watermark_blocks:
            return AllocStatus.OK
        else:
            return AllocStatus.LATER

    def allocate(self, seq_group: SequenceGroup) -> None:
        # NOTE: Here we assume that all sequences in the group have the same
        # prompt.
        seq = seq_group.get_seqs(status=SequenceStatus.WAITING)[0]

        # Allocate new physical token blocks that will store the prompt tokens.
        num_prompt_blocks = len(seq.logical_token_blocks)

        block_table: BlockTable = []
        for logical_idx in range(num_prompt_blocks):
            # This is sequence-level logic for allocating.
            # If sliding window, then the block table refers back to itself
            # Otherwise it has new allocations.

            if (self.block_sliding_window is not None
                    and logical_idx >= self.block_sliding_window):
                block = block_table[logical_idx % self.block_sliding_window]
            else:
                block = self.gpu_allocator.allocate(
                    seq.hash_of_block(logical_idx),
                    seq.num_hashed_tokens_of_block(logical_idx))
            block_table.append(block)

        # Assign the block table for each sequence.
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
            self.block_tables[seq.seq_id] = block_table.copy()

    def can_append_slot(self, seq_group: SequenceGroup) -> bool:
        # Simple heuristic: If there is at least one free block
        # for each sequence, we can append.
        num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks()
        num_seqs = seq_group.num_seqs(status=SequenceStatus.RUNNING)
        return num_seqs <= num_free_gpu_blocks

    def _promote_last_block(
        self,
        seq: Sequence,
        last_block: PhysicalTokenBlock,
    ) -> PhysicalTokenBlock:
        # Compute a new hash for the block so that it can be shared by other Sequences
        new_hash = seq.hash_of_block(len(seq.logical_token_blocks) - 1)

        # if new_hash is already in the cached table, then free last_block and return the cached version
        if self.gpu_allocator.contains_block(new_hash):
            self.gpu_allocator.free(last_block)
            return self.gpu_allocator.allocate(new_hash)
        else:
            self.gpu_allocator.update_hash(new_hash, last_block)
            return last_block

    def _is_last_block_full(
        self,
        seq: Sequence,
    ) -> bool:
        token_ids_len = len(seq.data.get_token_ids())
        return token_ids_len > 0 and token_ids_len % seq.block_size == 0

    def _maybe_promote_last_block(
        self,
        seq: Sequence,
        last_block: PhysicalTokenBlock,
    ) -> PhysicalTokenBlock:
        if self._is_last_block_full(seq):
            return self._promote_last_block(seq, last_block)
        else:
            return last_block

    def _allocate_last_physical_block(
        self,
        seq: Sequence,
    ) -> PhysicalTokenBlock:
        # Called before a new block is appended.
        # This is in charge of allocating a new physical block (to be appended).

        # None if the last block is not full. Otherwise, we set it to the content hash.
        block_hash: Optional[int] = None
        if (self._is_last_block_full(seq)):
            block_hash = seq.hash_of_block(len(seq.logical_token_blocks) - 1)
        num_hashed_tokens = seq.num_hashed_tokens_of_block(
            len(seq.logical_token_blocks) - 1)

        # num_hashed_tokens is used to compute future hashes
        # (e.g. in the hashing function, it is used to ask the sequence for prefix tokens)
        new_block = self.gpu_allocator.allocate(block_hash, num_hashed_tokens)

        # If the block has is None, then the block is not full.
        # If the block is not full, then we expect it to have a refcount of 1.
        # This doesn't feel quite justified but it's not the worst assertion..
        # (I'm thinking of beam search / CoW)
        if block_hash is None:
            assert new_block.ref_count == 1
        return new_block

    def append_slot(
        self,
        seq: Sequence,
    ) -> Optional[Tuple[int, int]]:
        """Allocate a physical slot for a new token."""
        logical_blocks = seq.logical_token_blocks
        block_table = self.block_tables[seq.seq_id]
        # If we need to allocate a new physical block
        if len(block_table) < len(logical_blocks):
            # Currently this code only supports adding one physical block
            assert len(block_table) == len(logical_blocks) - 1

            if (self.block_sliding_window
                    and len(block_table) >= self.block_sliding_window):
                # reuse a block
                block_table.append(block_table[len(block_table) %
                                               self.block_sliding_window])
            else:
                # The sequence has a new logical block.
                # Allocate a new physical block.
                new_block = self._allocate_last_physical_block(seq)
                block_table.append(new_block)
                return None

        # We want to append the token to the last physical block.
        last_block = block_table[-1]
        assert last_block.device == Device.GPU
        if last_block.ref_count == 1:
            # Not shared with other sequences. Appendable.
            # If the last block is now complete, promote it to a full block so that it can be shared
            new_block = self._maybe_promote_last_block(seq, last_block)
            block_table[-1] = new_block
            return None
        else:
            # The last block is shared with other sequences.
            # Copy on Write: Allocate a new block and copy the tokens.
            new_block = self._allocate_last_physical_block(seq)

            block_table[-1] = new_block
            self.gpu_allocator.free(last_block)
            return last_block.block_number, new_block.block_number

    def fork(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        # NOTE: fork does not allocate a new physical block.
        # Thus, it is always safe from OOM.
        src_block_table = self.block_tables[parent_seq.seq_id]
        self.block_tables[child_seq.seq_id] = src_block_table.copy()
        for block in src_block_table:
            block.ref_count += 1

    def _get_physical_blocks(
            self, seq_group: SequenceGroup) -> List[PhysicalTokenBlock]:
        # NOTE: Here, we assume that the physical blocks are only shared by
        # the sequences in the same group.
        blocks: Set[PhysicalTokenBlock] = set()
        for seq in seq_group.get_seqs():
            if seq.is_finished():
                continue
            blocks.update(self.block_tables[seq.seq_id])
        return list(blocks)

    def can_swap_in(self, seq_group: SequenceGroup) -> bool:
        blocks = self._get_physical_blocks(seq_group)
        num_swapped_seqs = seq_group.num_seqs(status=SequenceStatus.SWAPPED)
        num_free_blocks = self.gpu_allocator.get_num_free_blocks()
        # NOTE: Conservatively, we assume that every sequence will allocate
        # at least one free block right after the swap-in.
        # NOTE: This should match the logic in can_append_slot().
        num_required_blocks = len(blocks) + num_swapped_seqs
        return num_free_blocks - num_required_blocks >= self.watermark_blocks

    def swap_in(self, seq_group: SequenceGroup) -> Dict[int, int]:
        # CPU block -> GPU block.
        mapping: Dict[PhysicalTokenBlock, PhysicalTokenBlock] = {}
        for seq in seq_group.get_seqs(status=SequenceStatus.SWAPPED):
            new_block_table: BlockTable = []
            block_table = self.block_tables[seq.seq_id]

            for cpu_block in block_table:
                if cpu_block in mapping:
                    # This is an example of logic that should be subsumed by
                    # prefix caching. If blocks are shared in a sequence group,
                    # there is no need for refcounting logic -- should be handled
                    # by layer below.
                    gpu_block = mapping[cpu_block]
                    gpu_block.ref_count += 1
                else:
                    gpu_block = self.gpu_allocator.allocate(
                        cpu_block.block_hash, cpu_block.num_hashed_tokens)
                    mapping[cpu_block] = gpu_block
                new_block_table.append(gpu_block)
                # Free the CPU block swapped in to GPU.
                self.cpu_allocator.free(cpu_block)
            self.block_tables[seq.seq_id] = new_block_table

        block_number_mapping = {
            cpu_block.block_number: gpu_block.block_number
            for cpu_block, gpu_block in mapping.items()
        }
        return block_number_mapping

    def can_swap_out(self, seq_group: SequenceGroup) -> bool:
        blocks = self._get_physical_blocks(seq_group)
        return len(blocks) <= self.cpu_allocator.get_num_free_blocks()

    def swap_out(self, seq_group: SequenceGroup) -> Dict[int, int]:
        # GPU block -> CPU block.
        mapping: Dict[PhysicalTokenBlock, PhysicalTokenBlock] = {}
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            new_block_table: BlockTable = []
            block_table = self.block_tables[seq.seq_id]

            for gpu_block in block_table:
                if gpu_block in mapping:
                    cpu_block = mapping[gpu_block]
                    cpu_block.ref_count += 1
                else:
                    cpu_block = self.cpu_allocator.allocate(
                        gpu_block.block_hash, gpu_block.num_hashed_tokens)
                    mapping[gpu_block] = cpu_block
                new_block_table.append(cpu_block)
                # Free the GPU block swapped out to CPU.
                self.gpu_allocator.free(gpu_block)
            self.block_tables[seq.seq_id] = new_block_table

        block_number_mapping = {
            gpu_block.block_number: cpu_block.block_number
            for gpu_block, cpu_block in mapping.items()
        }
        return block_number_mapping

    def _free_block_table(self, block_table: BlockTable) -> None:
        for block in set(block_table):
            if block.device == Device.GPU:
                self.gpu_allocator.free(block)
            else:
                self.cpu_allocator.free(block)

    def free(self, seq: Sequence) -> None:
        if seq.seq_id not in self.block_tables:
            # Already freed or haven't been scheduled yet.
            return
        block_table = self.block_tables[seq.seq_id]
        self._free_block_table(block_table)
        del self.block_tables[seq.seq_id]

    def reset(self) -> None:
        for block_table in self.block_tables.values():
            self._free_block_table(block_table)
        self.block_tables.clear()

    def get_block_table(self, seq: Sequence) -> List[int]:
        block_table = self.block_tables[seq.seq_id]
        return [block.block_number for block in block_table]

    def get_num_free_gpu_blocks(self) -> int:
        return self.gpu_allocator.get_num_free_blocks()

    def get_num_free_cpu_blocks(self) -> int:
        return self.cpu_allocator.get_num_free_blocks()

    def access_all_blocks_in_seq(
        self,
        seq: Sequence,
        access_time: float,
    ) -> None:
        block_table = self.block_tables[seq.seq_id]
        for block in block_table:
            block.last_accessed = access_time

    def compute_last_full_block_in_seq(self, seq: Sequence):
        if seq.seq_id not in self.block_tables:
            return
        max_full_block = seq.get_len() // self.block_size - 1
        block_table = self.block_tables[seq.seq_id]
        if max_full_block == -1:
            return
        block_table[max_full_block].computed = True

    def get_all_block_ids_till_computed(self, seq: Sequence) -> List[int]:
        if seq.seq_id not in self.block_tables:
            return []
        block_table = self.block_tables[seq.seq_id]
        for block_idx in reversed(range(len(block_table))):
            if block_table[block_idx].computed:
                return [b.block_number for b in block_table[:block_idx + 1]]
        return []

    def get_common_computed_block_ids(self,
                                      seq_group: SequenceGroup) -> List[int]:
        """Return the block ids that are common for a given sequence group.

        Used in prefill (can skip prefill of some blocks).
        """
        # Can return non-empty result only with prefix caching enabled.
        if not self.enable_caching:
            return []

        ids_list = [
            self.get_all_block_ids_till_computed(seq)
            for seq in iter(seq_group.seqs_dict.values())
        ]
        return commonprefix([ids for ids in ids_list if ids != []])

    def mark_blocks_as_computed(self, seq_group: SequenceGroup):
        # NOTE: We only mark the last full block because with prefix caching,
        # all blocks until the marked one are guaranteed to be computed.
        if self.enable_caching:
            for seq in seq_group.seqs_dict.values():
                self.compute_last_full_block_in_seq(seq)