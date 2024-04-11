import pytest

from vllm.core.block_manager_v2 import BlockSpaceManagerV2
from vllm.core.interfaces import AllocStatus
from vllm.sequence import Logprob, SequenceStatus
from vllm.utils import chunk_list

from ..utils import create_dummy_prompt, create_seq_group


@pytest.mark.parametrize("block_size", [16])
@pytest.mark.parametrize("num_gpu_blocks", [8, 40, 80])
@pytest.mark.parametrize("num_seqs_per_group", [1, 4])
@pytest.mark.parametrize("watermark", [0.0, 0.5])
def test_can_allocate_seq_group(block_size: int, num_seqs_per_group: int,
                                num_gpu_blocks: int, watermark: float):
    block_manager = BlockSpaceManagerV2(
        block_size=block_size,
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=1024,
        watermark=watermark,
    )
    num_watermark_blocks = int(watermark * num_gpu_blocks)

    num_output_blocks_per_seq = 1

    # NOTE: This should be num_output_blocks_per_seq * num_seqs_per_group, but
    # the current implementation assumes all seqs are new prompts / don't have
    # different output lens.
    num_output_blocks = num_output_blocks_per_seq

    for num_prompt_blocks in range(1, num_gpu_blocks - num_output_blocks):
        seq_group = create_seq_group(
            seq_prompt_len=block_size * num_prompt_blocks,
            seq_output_lens=[
                block_size * num_output_blocks_per_seq
                for _ in range(num_seqs_per_group)
            ],
        )

        assert num_prompt_blocks + num_output_blocks <= num_gpu_blocks

        can_allocate_result = block_manager.can_allocate(seq_group)

        num_required_blocks = num_prompt_blocks + num_output_blocks

        if num_gpu_blocks - num_required_blocks < num_watermark_blocks:
            assert can_allocate_result == AllocStatus.NEVER
        elif num_gpu_blocks >= num_required_blocks:
            assert can_allocate_result == AllocStatus.OK
        else:
            assert can_allocate_result == AllocStatus.LATER


@pytest.mark.parametrize("block_size", [1, 8])
@pytest.mark.parametrize("prompt_len", [1, 7, 8])
@pytest.mark.parametrize("num_slots_to_append", [1, 8, 129])
@pytest.mark.parametrize("num_lookahead_slots", [0, 10])
def test_append_slots(block_size, prompt_len, num_slots_to_append,
                      num_lookahead_slots):
    """Verify append_slots consumes the correct number of blocks from the block
    table.
    """

    num_gpu_blocks = 1024
    watermark = 0.1
    block_manager = BlockSpaceManagerV2(
        block_size=block_size,
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=0,
        watermark=watermark,
    )

    seq_group = create_seq_group(
        seq_prompt_len=prompt_len,
        seq_output_lens=[0],
    )

    # Allocate seq
    assert block_manager.can_allocate(seq_group)
    block_manager.allocate(seq_group)

    # Seq seq to RUNNING
    seq = seq_group.get_seqs()[0]
    seq.status = SequenceStatus.RUNNING

    # Append tokens to the sequeqnce
    for token_id in range(num_slots_to_append):
        seq.append_token_id(token_id, {token_id: Logprob(0.0)})

    # Append slots for new tokens and lookahead slots.
    free_blocks_before_append = block_manager.get_num_free_gpu_blocks()
    block_manager.append_slots(seq, num_lookahead_slots)
    num_consumed_blocks = (free_blocks_before_append -
                           block_manager.get_num_free_gpu_blocks())

    # Expect consumed blocks to be new blocks required to support the new slots.
    expected_consumed_blocks = len(
        chunk_list(
            list(
                range(prompt_len + num_slots_to_append + num_lookahead_slots)),
            block_size)) - len(chunk_list(list(range(prompt_len)), block_size))
    assert num_consumed_blocks == expected_consumed_blocks


@pytest.mark.parametrize("block_size", [8])
@pytest.mark.parametrize("num_cpu_blocks", [4])
@pytest.mark.parametrize("num_gpu_blocks", [4])
@pytest.mark.parametrize("num_lookahead_slots", [2])
@pytest.mark.parametrize("enable_caching", [False])
def test_swap(block_size, num_cpu_blocks, num_gpu_blocks, num_lookahead_slots,
              enable_caching):
    block_manager = BlockSpaceManagerV2(block_size,
                                        num_cpu_blocks,
                                        num_gpu_blocks,
                                        watermark=0,
                                        enable_caching=enable_caching)
    prompt, seq_group = create_dummy_prompt("1", prompt_length=block_size - 1)
    prompt.status = SequenceStatus.WAITING
    block_manager.allocate(seq_group)
    # Emulate a forward pass by appending a single token.
    # The block manager then knows how many unprocessed
    # tokens will be written in the next forward pass.
    token_id = 0
    prompt.status = SequenceStatus.RUNNING
    prompt.append_token_id(token_id, {token_id: Logprob(0.0)})

    # Swap seq group from GPU -> CPU.
    gpu_blocks = block_manager.get_block_table(prompt)
    assert block_manager.can_swap_out(seq_group)
    before_cpu_blocks = block_manager.get_num_free_cpu_blocks()
    before_gpu_blocks = block_manager.get_num_free_gpu_blocks()
    mapping = block_manager.swap_out(seq_group)
    assert list(mapping.keys()) == gpu_blocks
    after_cpu_blocks = block_manager.get_num_free_cpu_blocks()
    after_gpu_blocks = block_manager.get_num_free_gpu_blocks()
    assert before_cpu_blocks == after_cpu_blocks + len(gpu_blocks)
    assert before_gpu_blocks + len(gpu_blocks) == after_gpu_blocks
    prompt.status = SequenceStatus.SWAPPED

    # Swap seq group from CPU -> GPU.
    assert block_manager.can_swap_in(seq_group, num_lookahead_slots)
    before_cpu_blocks = block_manager.get_num_free_cpu_blocks()
    before_gpu_blocks = block_manager.get_num_free_gpu_blocks()
    mapping = block_manager.swap_in(seq_group, num_lookahead_slots)
    cpu_blocks = block_manager.get_block_table(prompt)
    assert list(mapping.keys()) == [cpu_blocks[0]]
    after_cpu_blocks = block_manager.get_num_free_cpu_blocks()
    after_gpu_blocks = block_manager.get_num_free_gpu_blocks()
    assert before_gpu_blocks == after_gpu_blocks + len(cpu_blocks)
