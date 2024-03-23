import random
import pytest
from typing import Optional, List
import random
from unittest.mock import MagicMock
import math

from vllm.utils import Device
from vllm.core.block.interfaces import BlockAllocator, Block
from vllm.core.block.naive_block import NaiveBlockAllocator, NaiveBlock
from vllm.core.block.device_aware_block_allocator import DeviceAwareBlockAllocator
#from vllm.core.block.interfaces import NaiveBlockAllocator, NaiveBlock, BlockAllocator, Block
#from vllm.block2 import RefCounter
#from vllm.block2 import PrefixCachingBlock, PrefixCachingBlockAllocator

@pytest.mark.parametrize("num_cpu_blocks", [0, 512])
@pytest.mark.parametrize("num_gpu_blocks", [1024])
@pytest.mark.parametrize("block_size", [16])
@pytest.mark.parametrize("allocator_type", ["naive", "prefix_caching"])
def test_allocate_mutable(num_cpu_blocks: int, num_gpu_blocks: int, block_size: int, allocator_type: str):
    allocator = DeviceAwareBlockAllocator.create(
        allocator_type=allocator_type,
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=num_cpu_blocks,
        block_size=block_size,
    )

    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks
    
    cpu_blocks = [allocator.allocate_mutable(prev_block=None, device=Device.CPU) for _ in range(num_cpu_blocks)]
    assert allocator.get_num_free_blocks(Device.CPU) == 0
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks
    
    gpu_blocks = [allocator.allocate_mutable(prev_block=None, device=Device.GPU) for _ in range(num_gpu_blocks)]
    assert allocator.get_num_free_blocks(Device.CPU) == 0
    assert allocator.get_num_free_blocks(Device.GPU) == 0

    _ = [allocator.free(block) for block in cpu_blocks]
    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == 0

    _ = [allocator.free(block) for block in gpu_blocks]
    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks

def chunk_list(lst, chunk_size):
    """Yield successive chunk_size chunks from lst."""
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]

@pytest.mark.parametrize("num_cpu_blocks", [0, 512])
@pytest.mark.parametrize("num_gpu_blocks", [1024])
@pytest.mark.parametrize("block_size", [2])
@pytest.mark.parametrize("allocator_type", ["naive", "prefix_caching"])
def test_allocate_immutable(num_cpu_blocks: int, num_gpu_blocks: int, block_size: int, allocator_type: str):
    allocator = DeviceAwareBlockAllocator.create(
        allocator_type=allocator_type,
        num_gpu_blocks=num_gpu_blocks,
        num_cpu_blocks=num_cpu_blocks,
        block_size=block_size,
    )

    unique_token_ids = list(range((num_cpu_blocks + num_gpu_blocks) * block_size))
    gpu_token_ids = chunk_list(unique_token_ids[:num_gpu_blocks * block_size], block_size)
    cpu_token_ids = chunk_list(unique_token_ids[num_gpu_blocks * block_size:], block_size)

    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks
    
    cpu_blocks = [allocator.allocate_immutable(prev_block=None, token_ids=token_ids, device=Device.CPU) for token_ids in cpu_token_ids]
    assert allocator.get_num_free_blocks(Device.CPU) == 0
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks
    
    gpu_blocks = [allocator.allocate_immutable(prev_block=None, token_ids=token_ids, device=Device.GPU) for token_ids in gpu_token_ids]
    assert allocator.get_num_free_blocks(Device.CPU) == 0
    assert allocator.get_num_free_blocks(Device.GPU) == 0

    _ = [allocator.free(block) for block in cpu_blocks]
    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == 0

    _ = [allocator.free(block) for block in gpu_blocks]
    assert allocator.get_num_free_blocks(Device.CPU) == num_cpu_blocks
    assert allocator.get_num_free_blocks(Device.GPU) == num_gpu_blocks

