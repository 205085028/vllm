from typing import Dict, List

from vllm.core.interfaces import AllocStatus, BlockSpaceManager
from vllm.sequence import Sequence, SequenceGroup


class BlockSpaceManagerV3(BlockSpaceManager):
    """A simple version of BlockSpaceManager for use in environments
    where block management is not required.

    This class provides the same interface as BlockSpaceManager, but its
    methods perform no actions or return simple values like True in specific
    actions. It's designed to be used in scenarios where the overhead of
    block management is unnecessary, such as in an embedding environment.
    """

    def __init__(
        self,
        **kwargs,
    ) -> None:
        pass

    def can_allocate(self, seq_group: SequenceGroup) -> AllocStatus:
        # Always return OK for dummy purposes
        return AllocStatus.OK

    def allocate(self, seq_group: SequenceGroup) -> None:
        # No actual allocation logic needed
        pass

    def can_append_slots(self, seq_group: SequenceGroup,
                         num_lookahead_slots: int) -> bool:
        pass

    def append_slots(
        self,
        seq: Sequence,
        num_lookahead_slots: int,
    ) -> Dict[int, List[int]]:
        pass

    def fork(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        pass

    def can_swap_in(self, seq_group: SequenceGroup,
                    num_lookahead_slots: int) -> bool:
        return True

    def swap_in(self, seq_group: SequenceGroup,
                num_lookahead_slots: int) -> Dict[int, int]:
        pass

    def can_swap_out(self, seq_group: SequenceGroup) -> bool:
        return True

    def swap_out(self, seq_group: SequenceGroup) -> Dict[int, int]:
        pass

    def free(self, seq: Sequence) -> None:
        # No operation on free
        pass

    def get_block_table(self, seq: Sequence) -> List[int]:
        return []

    def get_num_free_gpu_blocks(self) -> int:
        return 1

    def get_num_free_cpu_blocks(self) -> int:
        return 1

    def access_all_blocks_in_seq(
        self,
        seq: Sequence,
        access_time: float,
    ) -> None:
        pass

    def get_common_computed_block_ids(self,
                                      seq_group: SequenceGroup) -> List[int]:
        return []

    def mark_blocks_as_computed(self, seq_group: SequenceGroup):
        pass
