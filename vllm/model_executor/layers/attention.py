"""Multi-head attention."""
from typing import List, Optional

import importlib
import torch
import torch.nn as nn
from xformers import ops as xops
from xformers.ops.fmha.attn_bias import (BlockDiagonalCausalMask,
                                         LowerTriangularMaskWithTensorBias)

from vllm._C import ops
from vllm._C import cache_ops
from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.triton_kernel.prefix_prefill import (
    context_attention_fwd)
from vllm.utils import is_hip

# try:
#     from flash_attn import flash_attn_with_kvcache
# except ImportError:
#     flash_attn_with_kvcache = None
try:
    from flash_attn import (flash_attn_with_page_attention,
                            flash_attn_varlen_with_page_attention)
except Exception as e:
    flash_attn_with_page_attention = e
    flash_attn_varlen_with_page_attention = e

_SUPPORTED_HEAD_SIZES = [64, 80, 96, 112, 128, 256]
# Should be the same as PARTITION_SIZE in `paged_attention_v2_launcher`.
_PARTITION_SIZE = 512


class PagedAttention(nn.Module):
    """MHA/MQA/GQA layer with PagedAttention.

    This class takes query, key, and value tensors as input. The input tensors
    can either contain prompt tokens or generation tokens.
    The class does the following:

    1. Reshape and store the input key and value tensors in the KV cache.
    2. Perform (multi-head/multi-query/grouped-query) attention using either
        xformers or the PagedAttention custom op.
    3. Return the output tensor.
    """

    def __init__(
        self,
        num_heads: int,
        head_size: int,
        scale: float,
        num_kv_heads: Optional[int] = None,
        alibi_slopes: Optional[List[float]] = None,
        sliding_window: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.num_heads = num_heads
        self.head_size = head_size
        self.scale = float(scale)
        self.num_kv_heads = num_heads if num_kv_heads is None else num_kv_heads
        self.sliding_window = sliding_window
        if alibi_slopes is not None:
            alibi_slopes = torch.tensor(alibi_slopes, dtype=torch.float32)
        self.register_buffer("alibi_slopes", alibi_slopes, persistent=False)

        assert self.num_heads % self.num_kv_heads == 0
        self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        if self.head_size not in _SUPPORTED_HEAD_SIZES:
            raise ValueError(f"head_size ({self.head_size}) is not supported. "
                             f"Supported head sizes: {_SUPPORTED_HEAD_SIZES}.")

        self.use_ref_attention = self.check_use_ref_attention()

    def check_use_ref_attention(self) -> bool:
        if not is_hip():
            return False
        # For ROCm, check whether flash attention is installed or not.
        # if not, use_ref_attention needs to be True
        return importlib.util.find_spec("flash_attn") is None

    def ref_masked_attention(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
    ) -> torch.Tensor:
        query = query.view(-1, self.num_heads, self.head_size)
        key = key.view(-1, self.num_kv_heads, self.head_size)
        value = value.view(-1, self.num_kv_heads, self.head_size)

        seq_len, _, _ = query.shape
        attn_mask = torch.triu(torch.ones(seq_len,
                                          seq_len,
                                          dtype=query.dtype,
                                          device=query.device),
                               diagonal=1)
        attn_mask = attn_mask * torch.finfo(query.dtype).min

        attn_weights = self.scale * torch.einsum("qhd,khd->hqk", query,
                                                 key).float()
        attn_weights = attn_weights + attn_mask.float()
        attn_weights = torch.softmax(attn_weights, dim=-1).to(value.dtype)
        out = torch.einsum("hqk,khd->qhd", attn_weights, value)
        return out

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        key_cache: Optional[torch.Tensor],
        value_cache: Optional[torch.Tensor],
        input_metadata: InputMetadata,
    ) -> torch.Tensor:
        """PagedAttention forward pass.

        Args:
            query: shape = [batch_size, seq_len, num_heads * head_size]
            key: shape = [batch_size, seq_len, num_kv_heads * head_size]
            value: shape = [batch_size, seq_len, num_kv_heads * head_size]
            key_cache: shape = [num_blocks, num_kv_heads, head_size/x,
                block_size, x]
            value_cache: shape = [num_blocks, num_kv_heads, head_size,
                block_size]
            input_metadata: metadata for the inputs.
        Returns:
            shape = [batch_size, seq_len, num_heads * head_size]
        """
        batch_size, seq_len, hidden_size = query.shape
        # print("SANG-TODO query size: ", query.size())
        # Reshape the query, key, and value tensors.
        query = query.view(-1, self.num_heads, self.head_size)
        key = key.view(-1, self.num_kv_heads, self.head_size)
        value = value.view(-1, self.num_kv_heads, self.head_size)

        # Reshape the keys and values and store them in the cache.
        # If key_cache and value_cache are not provided, the new key and value
        # vectors will not be cached. This happens during the initial memory
        # profiling run.
        if key_cache is not None and value_cache is not None:
            if input_metadata.flash_style:
                # print("SANG-TODO reshape cache flash.")
                cache_ops.reshape_and_cache_flash(
                    key, value, key_cache, value_cache,
                    input_metadata.slot_mapping.flatten())
            else:
                # print("SANG-TODO reshape cache.")
                cache_ops.reshape_and_cache(
                    key,
                    value,
                    key_cache,
                    value_cache,
                    input_metadata.slot_mapping.flatten(),
                    input_metadata.kv_cache_dtype,
                )

        if input_metadata.is_prompt:
            # normal attention
            if (key_cache is None or value_cache is None
                    # or input_metadata.block_tables.numel() == 0):
                    or not input_metadata.prefix_enabled):
                # print("SANG-TODO flash attn is used.")
                # print(
                #     "SANG-TODO query size: ",
                #     query.view(batch_size, seq_len, self.num_heads,
                #                self.head_size).size())
                if self.num_kv_heads != self.num_heads:
                    # As of Nov 2023, xformers only supports MHA. For MQA/GQA,
                    # project the key and value tensors to the desired number of
                    # heads.
                    # TODO(woosuk): Use MQA/GQA kernels for higher performance.
                    query = query.view(query.shape[0], self.num_kv_heads,
                                       self.num_queries_per_kv,
                                       query.shape[-1])
                    key = key[:, :,
                              None, :].expand(key.shape[0], self.num_kv_heads,
                                              self.num_queries_per_kv,
                                              key.shape[-1])
                    value = value[:, :,
                                  None, :].expand(value.shape[0],
                                                  self.num_kv_heads,
                                                  self.num_queries_per_kv,
                                                  value.shape[-1])

                # Set attention bias if not provided. This typically happens at
                # the very attention layer of every iteration.
                # FIXME(woosuk): This is a hack.
                if input_metadata.attn_bias is None:
                    if self.alibi_slopes is None:
                        attn_bias = BlockDiagonalCausalMask.from_seqlens(
                            [seq_len] * batch_size)
                        if self.sliding_window is not None:
                            attn_bias = attn_bias.make_local_attention(
                                self.sliding_window)
                        input_metadata.attn_bias = attn_bias
                    else:
                        input_metadata.attn_bias = _make_alibi_bias(
                            self.alibi_slopes, self.num_kv_heads, batch_size,
                            seq_len, query.dtype)

                if self.use_ref_attention:
                    output = self.ref_masked_attention(
                        query,
                        key,
                        value,
                    )
                    # Using view got RuntimeError: view size is not compatible with input tensor's size and stride
                    # (at least one dimension spans across two contiguous subspaces). Use reshape instead
                    return output.reshape(batch_size, seq_len, hidden_size)

                # TODO(woosuk): Too many view operations. Let's try to reduce
                # them in the future for code readability.
                if self.alibi_slopes is None:
                    query = query.unsqueeze(0)
                    key = key.unsqueeze(0)
                    value = value.unsqueeze(0)
                else:
                    query = query.unflatten(0, (batch_size, seq_len))
                    key = key.unflatten(0, (batch_size, seq_len))
                    value = value.unflatten(0, (batch_size, seq_len))
                out = xops.memory_efficient_attention_forward(
                    query,
                    key,
                    value,
                    attn_bias=input_metadata.attn_bias,
                    p=0.0,
                    scale=self.scale,
                    op=xops.fmha.MemoryEfficientAttentionFlashAttentionOp[0] if
                    (is_hip()) else None,
                )
                output = out.view_as(query)
                # if key_cache is not None and value_cache is not None:
                #     output2 = flash_single_query_cached_kv_attention(
                #         None,
                #         query.view(batch_size, seq_len, self.num_heads, self.head_size),
                #         key_cache,
                #         value_cache,
                #         self.scale,
                #         input_metadata.block_tables,
                #         input_metadata.context_lens,
                #         alibi_slopes=self.alibi_slopes,
                #     )
                # output3 = flash_attn_func(
                #         query.view(batch_size, seq_len, self.num_heads, self.head_size),
                #         key.view(batch_size, seq_len, self.num_heads, self.head_size),
                #         value.view(batch_size, seq_len, self.num_heads, self.head_size),
                #         softmax_scale=self.scale,
                #         causal=True,
                #     ).view_as(query)
                # output4 = flash_attn_func(
                #         query,
                #         key,
                #         value,
                #         softmax_scale=self.scale,
                #         causal=True,
                #     ).view_as(query)
            # if key_cache is not None and value_cache is not None:
            #     output2 = flash_attn_with_kvcache_paged(
            #         query.view(batch_size, seq_len, self.num_heads,
            #                     self.head_size),
            #         key_cache,
            #         value_cache,
            #         self.scale,
            #         input_metadata.block_tables,
            #         input_metadata.context_lens,
            #         self.alibi_slopes,
            #     )
            else:
                if input_metadata.flash_style:
                    output = flash_single_query_cached_kv_attention(
                        None,
                        query.view(batch_size, seq_len, self.num_heads,
                                   self.head_size),
                        key_cache,
                        value_cache,
                        self.scale,
                        input_metadata.block_tables,
                        input_metadata.context_lens,
                        alibi_slopes=self.alibi_slopes,
                    )
                    # output = flash_multi_query_cached_kv_attention_varlen(
                    #     None,
                    #     query,
                    #     key_cache,
                    #     value_cache,
                    #     self.scale,
                    #     self.block_tables,
                    #     input_metadata.start_loc,
                    #     input_metadata.start_loc,
                    #     max_query_len,
                    #     max_context_len,
                    #     alibi_slopes=self.alibi_slopes,
                    # )
                else:
                    # print("SANG-TODO context attention")
                    # prefix-enabled attention
                    output = torch.empty_like(query)
                    context_attention_fwd(
                        query,
                        key,
                        value,
                        output,
                        key_cache,
                        value_cache,
                        input_metadata.
                        block_tables,  # [BS, max_block_per_request]
                        input_metadata.start_loc,
                        input_metadata.prompt_lens,
                        input_metadata.context_lens,
                        input_metadata.max_seq_len,
                        getattr(self, "alibi_slopes", None),
                    )

        else:
            # Decoding run.
            if input_metadata.flash_style:
                # output = flash_attn_with_kvcache_paged(
                #     query.view(batch_size, seq_len, self.num_heads,
                #                self.head_size), key_cache, value_cache,
                #     self.scale, input_metadata.block_tables,
                #     input_metadata.context_lens, self.alibi_slopes)
                output = flash_single_query_cached_kv_attention(
                    None,
                    query.view(batch_size, 1, self.num_heads, self.head_size),
                    key_cache,
                    value_cache,
                    self.scale,
                    input_metadata.block_tables,
                    input_metadata.context_lens,
                    alibi_slopes=self.alibi_slopes,
                )
            else:
                output = _paged_attention(
                    query,
                    key_cache,
                    value_cache,
                    input_metadata,
                    self.num_kv_heads,
                    self.scale,
                    self.alibi_slopes,
                )

        # Reshape the output tensor.
        return output.view(batch_size, seq_len, hidden_size)


def _make_alibi_bias(
    alibi_slopes: torch.Tensor,
    num_kv_heads: int,
    batch_size: int,
    seq_len: int,
    dtype: torch.dtype,
) -> LowerTriangularMaskWithTensorBias:
    bias = torch.arange(seq_len, dtype=dtype)
    # NOTE(zhuohan): HF uses
    #     `bias = bias[None, :].repeat(prompt_len, 1)`
    # here. We find that both biases give the same results, but
    # the bias below more accurately follows the original ALiBi
    # paper.
    bias = bias[None, :] - bias[:, None]

    # When using custom attention bias, xformers requires the bias to
    # be sliced from a tensor whose length is a multiple of 8.
    padded_len = (seq_len + 7) // 8 * 8
    num_heads = alibi_slopes.shape[0]
    bias = torch.empty(
        batch_size,
        num_heads,
        seq_len,
        padded_len,
        device=alibi_slopes.device,
        dtype=dtype,
    )[:, :, :, :seq_len].copy_(bias)
    bias.mul_(alibi_slopes[:, None, None])
    if num_heads != num_kv_heads:
        bias = bias.unflatten(1, (num_kv_heads, num_heads // num_kv_heads))
    attn_bias = LowerTriangularMaskWithTensorBias(bias)
    return attn_bias


def _paged_attention(
    query: torch.Tensor,
    key_cache: torch.Tensor,
    value_cache: torch.Tensor,
    input_metadata: InputMetadata,
    num_kv_heads: int,
    scale: float,
    alibi_slopes: Optional[torch.Tensor],
) -> torch.Tensor:
    output = torch.empty_like(query)

    block_size = value_cache.shape[3]
    num_seqs, num_heads, head_size = query.shape
    max_num_partitions = (
        (input_metadata.max_context_len + _PARTITION_SIZE - 1) //
        _PARTITION_SIZE)
    # NOTE(woosuk): We use a simple heuristic to decide whether to use
    # PagedAttention V1 or V2. If the number of partitions is 1, we use
    # V1 to avoid the overhead of reduction. Also, if the number of
    # sequences or heads is large, we use V1 since there is enough work
    # to parallelize.
    # TODO(woosuk): Tune this heuristic.
    # For context len > 8192, use V2 kernel to avoid shared memory shortage.
    use_v1 = input_metadata.max_context_len <= 8192 and (
        max_num_partitions == 1 or num_seqs * num_heads > 512)

    if use_v1:
        # Run PagedAttention V1.
        ops.paged_attention_v1(
            output,
            query,
            key_cache,
            value_cache,
            num_kv_heads,
            scale,
            input_metadata.block_tables,
            input_metadata.context_lens,
            block_size,
            input_metadata.max_context_len,
            alibi_slopes,
            input_metadata.kv_cache_dtype,
        )
    else:
        # Run PagedAttention V2.
        assert _PARTITION_SIZE % block_size == 0
        tmp_output = torch.empty(
            size=(num_seqs, num_heads, max_num_partitions, head_size),
            dtype=output.dtype,
            device=output.device,
        )
        exp_sums = torch.empty(
            size=(num_seqs, num_heads, max_num_partitions),
            dtype=torch.float32,
            device=output.device,
        )
        max_logits = torch.empty_like(exp_sums)
        ops.paged_attention_v2(
            output,
            exp_sums,
            max_logits,
            tmp_output,
            query,
            key_cache,
            value_cache,
            num_kv_heads,
            scale,
            input_metadata.block_tables,
            input_metadata.context_lens,
            block_size,
            input_metadata.max_context_len,
            alibi_slopes,
            input_metadata.kv_cache_dtype,
        )
    return output


# OSS version.
# def flash_attn_with_kvcache_paged(
#     query: torch.Tensor,
#     key_cache: torch.Tensor,
#     value_cache: torch.Tensor,
#     scale: float,
#     block_tables: torch.Tensor,
#     context_lens: torch.Tensor,
#     alibi_slopes: Optional[torch.Tensor] = None,
# ) -> torch.Tensor:
#     """Similar to vLLM's page attention, calculates a single token's attention
#     based on key/value caches. The main difference is this uses flash attention
#     style key-value caches.

#     Arguments:
#         See https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/flash_attn_interface.py
#         for other arguments.

#     Returns:
#         output: [num_tokens, num_heads, head_size]
#     """
#     block_size = value_cache.shape[1]
#     assert block_size % 256 == 0, ("only support block_size divisible by 256. "
#                                    f"Current block size: {block_size}")
#     _, _, num_heads, head_size = query.shape
#     out = flash_attn_with_kvcache(
#         query,
#         key_cache,
#         value_cache,
#         # Inplace update is slow. We don't use it.
#         # We assume kvcache is already updated before
#         # calling this API.
#         None,  # key
#         None,  # value
#         cache_seqlens=context_lens,
#         block_table=block_tables,
#         softmax_scale=scale,
#         causal=True,
#         alibi_slopes=alibi_slopes,
#         num_splits=0,
#     )

#     # num_tokens == batch_size * seqlen
#     return out.view(-1, num_heads, head_size)


def flash_single_query_cached_kv_attention(
    output: torch.Tensor,
    query: torch.Tensor,
    key_cache: torch.Tensor,
    value_cache: torch.Tensor,
    scale: float,
    block_tables: torch.Tensor,
    context_lens: torch.Tensor,
    alibi_slopes: Optional[torch.Tensor],
    actual_batch_size: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Similar to vLLM's page attention, calculates a single token's attention
    based on key/value caches. The main difference is this uses flash attention
    style key-value caches.

    Arguments:
        output: [num_padded_tokens, num_heads, head_size], output tensor
            to write. if None an new output tensor will be created.
        query: [batch_size, num_padded_tokens, num_heads, head_size], query tensor.
        key_cache: [num_blocks, block_size, num_heads, head_size], key cache.
        value_cache: [num_blocks, block_size, num_heads, head_size], value
            cache.
        scale: attention scale.
        block_tables: [num_padded_tokens, max_context_len / block_size],
            block tables.
        context_lens: [num_padded_tokens], context lengths.
        block_size: block size.
        alibi_slopes: unused.
        actual_batch_size: [1] actual batch size.

    Returns:
        output: [num_padded_tokens, num_heads, head_size]
    """
    block_size = value_cache.shape[1]
    assert block_size >= 32, "only support block_size >= 32 for flash attention"
    # TODO: support alibi_slopes
    assert alibi_slopes is None, "doesn't support alibi_slopes"
    batch_size, seqlen, num_heads, head_size = query.shape
    assert seqlen == 1, (
        "Single query attention can be only used for decoding phase.")
    num_tokens = batch_size * seqlen
    out = flash_attn_with_page_attention(
        query,
        key_cache,
        value_cache,
        block_tables,
        None,  # key
        None,  # value
        None,  # cos
        None,  # sin
        context_lens,
        None,  # cache_batch_idx
        softmax_scale=scale,
        causal=True,
        window_size=(-1, -1),
        rotary_interleaved=False,
        num_splits=0,
        actual_batch_size=actual_batch_size,
    )
    if output is not None:
        # in case that output is padded, only copy the valid part.
        output[:num_tokens].copy_(out.view(num_tokens, num_heads, head_size))
    return out.view(num_tokens, num_heads, head_size)


def flash_multi_query_cached_kv_attention_varlen(
    output: Optional[torch.Tensor],
    query: torch.Tensor,
    key_cache: torch.Tensor,
    value_cache: torch.Tensor,
    scale: float,
    block_tables: torch.Tensor,
    cum_seqlens_q: torch.Tensor,
    cum_context_len: torch.Tensor,
    max_query_len: int,
    max_context_len: int,
    alibi_slopes: Optional[torch.Tensor],
    actual_batch_size: Optional[torch.Tensor] = None,
):
    """Efficient multi-query paged attention based on flash attention.
    It calculates attentions of list of sequences packed in a single batch,
    indexed by cum_seqlens_q where the seq_i's index is
    [cum_seqlens_q[i], cum_seqlensq[i+1]].
    Similarlly, the length of context is stored in cum_seqlens_k with similar
    fashions.
    It also supports calculating attention incrementally, where context length
    is longer than sequence length.

    Arguments:
        output: [num_padded_tokens, num_heads, head_size], output tensor to
            write to. if None an new output tensor will be created.

        prefill -> always 1 batch
        query, head_size, head_dim

        varlen -> provides cumulative lengths of queries

        decoding -> always 1 batch
        1 * number_of_batch, head_size, head_dim

        query: [num_padded_tokens, num_heads, head_size], query tensor.
        key_cache: [num_blocks, block_size, num_heads, head_size], key cache.
        value_cache: [num_blocks, block_size, num_heads, head_size],
            value cache.
        scale: attention scale.
        block_tables: [num_padded_tokens, max_context_len / block_size],
            block tables.
        - these  two are the same if no chunked prefill (for prefill)
        - If you do chunked prefill, it may be different
            - when? see attention mask setting
            - Each iteration, it can have smaller # of queries than (because it is chunked)
                tokens we should attend to (context len).
            - cum_seqlens_q: actual query length (actual prompt length).
            - cum_context_len: context len that needs to be attended (subquery).
        cum_seqlens_q: [padded_batch_size + 1], cumulative sequence lengths
            of query.
        cum_context_len: [padded_batch_size + 1], cumulative lengths
            of context.
        block_size: block size.
        max_query_len: max query length.
        max_context_len: max context length.
        alibi_slopes: unused.
        actual_batch_size: [1] actual batch size.

    Returns:
        output: [num_padded_tokens, num_heads, head_size]
    """
    block_size = value_cache.shape[1]
    assert block_size >= 32, "only support block_size >= 32 for flash attention"
    # TODO: support alibi_slopes
    assert alibi_slopes is None, "doesn't support alibi_slopes"

    num_tokens, _, _ = query.shape
    out = flash_attn_varlen_with_page_attention(
        query,
        key_cache,
        value_cache,
        block_tables,
        cum_seqlens_q,
        cum_context_len,
        max_query_len,
        max_context_len,
        None,  # key
        None,  # value
        None,  # cos_cache
        None,  # sin_cache
        None,  # cache_batch_idx
        softmax_scale=scale,
        causal=True,
        window_size=(-1, -1),
        rotary_interleaved=False,
        num_splits=0,
        actual_batch_size=actual_batch_size,
    )

    if output is not None:
        output[:num_tokens].copy_(out)
    return out