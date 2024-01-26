from typing import Dict, List, Optional

import torch
import triton
import triton.language as tl
# FIXME(woosuk): The performance model is not designed for quantized matmul.
# For the best performance, we need to implement a new performance model.
from triton.ops.matmul_perf_model import (early_config_prune,
                                          estimate_matmul_time)

from vllm.model_executor.layers.quantization.ops.matmul_utils import (
    get_configs_compute_bound, get_configs_io_bound)

# NOTE(woosuk): These variables should be defined outside of the @triton
# decorator to avoid the following parsing error:
# https://github.com/openai/triton/issues/1589
CONFIGS = get_configs_compute_bound() + get_configs_io_bound()
HEURISTICS = {
    'EVEN_K': lambda args: args['K'] %
    (args['BLOCK_K'] * args['SPLIT_K']) == 0,
    'PACKED_BLOCK_N': lambda args: args['BLOCK_N'] // args['AWQ_PACK_FACTOR'],
    'PADDED_M': lambda args: triton.next_power_of_2(args['M']),
}


def _prune_invalid_configs(
    configs: List[triton.Config],
    pack_factor: int,
    group_size: int,
) -> List[triton.Config]:
    valid_configs: List[triton.Config] = []
    for config in configs:
        block_n = config.kwargs['BLOCK_N']
        if block_n % pack_factor != 0:
            continue
        block_k = config.kwargs['BLOCK_K']
        if group_size % block_k != 0:
            continue
        valid_configs.append(config)
    return valid_configs


def _prune_configs(configs, named_args):
    pruned = early_config_prune(configs, named_args)
    pack_factor = named_args['AWQ_PACK_FACTOR']
    group_size = named_args['AWQ_GROUP_SIZE']
    return _prune_invalid_configs(pruned, pack_factor, group_size)


# Grid: ((M // BLOCK_M) * (N // BLOCK_N), SPLIT_K)
@triton.autotune(
    configs=CONFIGS,
    key=['PADDED_M', 'N', 'K'],
    prune_configs_by={
        'early_config_prune': _prune_configs,
        'perf_model': estimate_matmul_time,
        'top_k': 50,  # FIXME: Too much. Reduce it to 10.
    },
)
@triton.heuristics(HEURISTICS)
@triton.jit
def _awq_kernel(A, B, C, M, N, K, Z, S, shifter_ptr, stride_am, stride_ak,
                stride_bk, stride_bn, stride_cm, stride_cn, stride_zk,
                stride_zn, stride_sk, stride_sn, AWQ_PACK_FACTOR: tl.constexpr,
                AWQ_GROUP_SIZE: tl.constexpr, PACKED_BLOCK_N: tl.constexpr,
                PADDED_M: tl.constexpr, dot_out_dtype: tl.constexpr,
                BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr,
                BLOCK_K: tl.constexpr, GROUP_M: tl.constexpr,
                SPLIT_K: tl.constexpr, EVEN_K: tl.constexpr):
    # matrix multiplication
    pid = tl.program_id(0)
    pid_z = tl.program_id(1)
    grid_m = tl.cdiv(M, BLOCK_M)
    grid_n = tl.cdiv(N, BLOCK_N)
    # re-order program ID for better L2 performance
    width = GROUP_M * grid_n
    group_id = pid // width
    group_size = min(grid_m - group_id * GROUP_M, GROUP_M)
    pid_m = group_id * GROUP_M + (pid % group_size)
    pid_n = (pid % width) // (group_size)
    # do matrix multiplication
    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    packed_rn = pid_n * PACKED_BLOCK_N + tl.arange(0, PACKED_BLOCK_N)
    ram = tl.max_contiguous(tl.multiple_of(rm % M, BLOCK_M), BLOCK_M)
    rbn = tl.max_contiguous(tl.multiple_of(packed_rn % N, PACKED_BLOCK_N),
                            PACKED_BLOCK_N)
    # rbn = packed_rn
    rk = pid_z * BLOCK_K + tl.arange(0, BLOCK_K)
    # pointers
    A = A + (ram[:, None] * stride_am + rk[None, :] * stride_ak)
    B = B + (rk[:, None] * stride_bk + rbn[None, :] * stride_bn)

    AWQ_BIT_WIDTH = 32 // AWQ_PACK_FACTOR
    AWQ_MASK = (1 << AWQ_BIT_WIDTH) - 1
    shifter = tl.load(shifter_ptr + tl.arange(0, AWQ_PACK_FACTOR))
    shifter_tiled = tl.load(shifter_ptr +
                            (tl.arange(0, BLOCK_N) % AWQ_PACK_FACTOR))

    s = tl.zeros([BLOCK_N], dtype=A.dtype.element_ty)
    z = tl.zeros([BLOCK_N], dtype=tl.int32)
    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=dot_out_dtype)
    for k in range(0, tl.cdiv(K, BLOCK_K * SPLIT_K)):
        if EVEN_K:
            a = tl.load(A)
            b = tl.load(B)
        else:
            k_remaining = K - k * (BLOCK_K * SPLIT_K)
            _0 = tl.zeros((1, 1), dtype=C.dtype.element_ty)
            a = tl.load(A, mask=rk[None, :] < k_remaining, other=_0)
            b = tl.load(B, mask=rk[:, None] < k_remaining, other=_0)

        if (k * BLOCK_K * SPLIT_K) % AWQ_GROUP_SIZE == 0:
            k_idx = pid_z * BLOCK_K + k * BLOCK_K * SPLIT_K
            k_idx = tl.where(k_idx < K, k_idx, 0)
            awq_g_idx = k_idx // AWQ_GROUP_SIZE

            z = tl.load(Z + awq_g_idx * stride_zk +
                        (rn // AWQ_PACK_FACTOR) * stride_zn)
            z = (z >> shifter_tiled) & AWQ_MASK
            z = z.to(tl.int32)

            s = tl.load(S + awq_g_idx * stride_sk + rn * stride_sn)

        # Unpack b from [BLOCK_K, PACKED_BLOCK_N] to [BLOCK_K, BLOCK_N]
        b = (b[:, None, :] >> shifter[None, :, None]) & AWQ_MASK
        b = tl.view(b, (BLOCK_K, BLOCK_N))

        # Compute s * (b - z)
        b = s * (b - z).to(A.dtype.element_ty)

        # Compute a @ b
        acc += tl.dot(a, b, out_dtype=dot_out_dtype)

        # Update pointers
        A += BLOCK_K * SPLIT_K * stride_ak
        B += BLOCK_K * SPLIT_K * stride_bk
    acc = acc.to(C.dtype.element_ty)
    # rematerialize rm and rn to save registers
    rm = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    rn = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    C = C + (rm[:, None] * stride_cm + rn[None, :] * stride_cn)
    mask = (rm < M)[:, None] & (rn < N)[None, :]
    # handles write-back with reduction-splitting
    if SPLIT_K == 1:
        tl.store(C, acc, mask=mask)
    else:
        tl.atomic_add(C, acc, mask=mask)


def awq_matmul(
    a: torch.Tensor,
    b: torch.Tensor,
    qzeros: torch.Tensor,
    scales: torch.Tensor,
    pack_factor: int,
    group_size: int,
    shifter: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Matrix multiplication for AWQ quantized weights.

    Args:
        a: An input activation tensor of shape `(M, K)`. FP16 or BF16.
        b: A packed weight tensor of shape `(K, N//P)`. INT32.
        qzeros: A tensor of shape `(K//G, N//P)`. INT32.
        scales: A tensor of shape `(K//G, N)`. FP16 or BF16.
        pack_factor: The packing factor abbreviated as `P`.
        group_size: The quantization group size abbreviated as `G`.
        shifter: A tensor of shape `(P,)`. INT32. The shifter for unpacking
            the packed weight tensor.
    """
    if pack_factor != 8:
        raise NotImplementedError("AWQ pack factor must be 8.")
    if group_size != 128:
        raise NotImplementedError("AWQ group size must be 128.")
    if shifter is None:
        shifter = get_shifter(pack_factor)

    # Check if the tensors are contiguous.
    assert a.is_contiguous()
    assert b.is_contiguous()
    assert qzeros.is_contiguous()
    assert scales.is_contiguous()

    # Check dtypes.
    assert a.dtype in (torch.float16, torch.bfloat16)
    assert b.dtype == torch.int32
    assert qzeros.dtype == torch.int32
    assert scales.dtype == a.dtype

    # Check shapes.
    assert a.shape[1] == b.shape[0]
    M, K = a.shape
    _, PACKED_N = b.shape
    P = pack_factor
    N = P * PACKED_N
    G = group_size
    assert qzeros.shape == (K // G, PACKED_N)
    assert scales.shape == (K // G, N)

    # Allocate output.
    c = torch.empty((M, N), dtype=a.dtype, device=a.device)

    # Launch kernel.
    dot_out_dtype = tl.float32
    grid = lambda META: (triton.cdiv(M, META['BLOCK_M']) * triton.cdiv(
        N, META['BLOCK_N']), META['SPLIT_K'])
    _awq_kernel[grid](a,
                      b,
                      c,
                      M,
                      N,
                      K,
                      qzeros,
                      scales,
                      shifter,
                      a.stride(0),
                      a.stride(1),
                      b.stride(0),
                      b.stride(1),
                      c.stride(0),
                      c.stride(1),
                      qzeros.stride(0),
                      qzeros.stride(1),
                      scales.stride(0),
                      scales.stride(1),
                      P,
                      G,
                      dot_out_dtype=dot_out_dtype,
                      GROUP_M=8)
    return c


_SHIFTER_CACHE: Dict[int, torch.Tensor] = {}


def get_shifter(pack_factor: int) -> torch.Tensor:
    assert pack_factor == 8
    if pack_factor in _SHIFTER_CACHE:
        return _SHIFTER_CACHE[pack_factor]

    shifter = torch.tensor([0, 4, 1, 5, 2, 6, 3, 7],
                           dtype=torch.int32,
                           device="cuda")
    shifter *= 4
    _SHIFTER_CACHE[pack_factor] = shifter
    return shifter


if __name__ == "__main__":
    from vllm._C import ops

    torch.manual_seed(0)
    torch.cuda.manual_seed(0)
    torch.cuda.manual_seed_all(0)

    MAX_INT32 = 0x7fffffff
    MIN_INT32 = -MAX_INT32 - 1
    GROUP_SIZE = 128
    PACK_FACTOR = 8
    M = 12
    K = 768
    N = 768

    a = torch.randn((M, K), dtype=torch.float16, device="cuda")
    b = torch.randint(MIN_INT32,
                      MAX_INT32, (K, N // PACK_FACTOR),
                      dtype=torch.int32,
                      device="cuda")
    qzeros = torch.randint(MIN_INT32,
                           MAX_INT32, (K // GROUP_SIZE, N // PACK_FACTOR),
                           dtype=torch.int32,
                           device="cuda")
    scales = torch.randn((K // GROUP_SIZE, N),
                         dtype=torch.float16,
                         device="cuda")

    c = awq_matmul(a, b, qzeros, scales, PACK_FACTOR, GROUP_SIZE)
    ans = ops.awq_gemm(a, b, scales, qzeros, PACK_FACTOR)

    print((c - ans).abs().max())
