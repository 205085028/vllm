import contextlib
import logging
from typing import Optional

import torch
from torch.distributed import ReduceOp

logger = logging.getLogger(__name__)

try:
    from vllm.model_executor.parallel_utils.pynccl import (NCCLCommunicator,
                                                           ncclGetVersion)
    logger.info(f"vLLM is using nccl=={ncclGetVersion()}")
except Exception as e:
    # in non-NVIDIA environments, we can't import the nccl module
    # e.g. when running on machines with AMD GPUs
    logger.info(f"Failed to import NCCL library: {e}")
    logger.info("It is expected if you are not running on NVIDIA GPUs.")
    pass

comm: Optional["NCCLCommunicator"] = None


def is_initialized() -> bool:
    """Returns whether the NCCL backend is initialized."""
    return comm is not None


@contextlib.contextmanager
def set_pynccl_stream(stream: torch.cuda.Stream):
    """Set the cuda stream for communication"""
    try:
        comm.stream = stream
        yield
    finally:
        pass


def init_process_group(world_size: int, rank: int, host: str,
                       port: int) -> None:
    assert not is_initialized()
    global comm
    comm = NCCLCommunicator(init_method=f"tcp://{host}:{port}",
                            world_size=world_size,
                            rank=rank)


def all_reduce(input_: torch.Tensor, op=ReduceOp.SUM) -> None:
    """All-reduces the input tensor across the process group."""
    assert input_.is_cuda, f"{input_} should be a cuda tensor"
    comm.all_reduce(input_, op)


def destroy_process_group() -> None:
    global comm
    comm = None


def get_world_size() -> int:
    """Returns the world size."""
    return comm.world_size


def get_nccl_backend():
    return comm
