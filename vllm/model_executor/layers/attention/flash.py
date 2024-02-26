"""Attention layer with Flash and PagedAttention."""
from typing import List, Optional

from flash_attn import flash_attn_func
import torch

from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.attention.base import BaseAttention
# from vllm.model_executor.layers.attention.paged_attn import PagedAttentionImpl
from vllm.model_executor.layers.attention.flash_infer import FlashInferImpl
from vllm.model_executor.layers.attention.utils import expand_gqa


class Attention(BaseAttention):

    def __init__(
        self,
        num_heads: int,
        head_size: int,
        scale: float,
        num_kv_heads: Optional[int] = None,
        alibi_slopes: Optional[List[float]] = None,
        sliding_window: Optional[int] = None,
    ) -> None:
        super().__init__(num_heads, head_size, scale, num_kv_heads,
                         alibi_slopes, sliding_window)
        suppored_head_sizes = FlashInferImpl.get_supported_head_sizes()
        if head_size not in suppored_head_sizes:
            raise ValueError(
                f"Head size {head_size} is not supported by PagedAttention. "
                f"Supported head sizes are: {suppored_head_sizes}.")
        self.sliding_window = ((self.sliding_window, self.sliding_window) if
                               self.sliding_window is not None else (-1, -1))

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        kv_cache: Optional[torch.Tensor],
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
        # Reshape the query, key, and value tensors.
        query = query.view(-1, self.num_heads, self.head_size)
        key = key.view(-1, self.num_kv_heads, self.head_size)
        value = value.view(-1, self.num_kv_heads, self.head_size)

        # Reshape the keys and values and store them in the cache.
        # If key_cache and value_cache are not provided, the new key and value
        # vectors will not be cached. This happens during the initial memory
        # profiling run.
        if kv_cache is not None:
            FlashInferImpl.reshape_and_cache(key, value, kv_cache, input_metadata)

        if input_metadata.is_prompt:
            # Prompt run.
            if True:
                # normal attention
                query = query.unflatten(0, (batch_size, seq_len))
                key = key.unflatten(0, (batch_size, seq_len))
                value = value.unflatten(0, (batch_size, seq_len))
                output = flash_attn_func(
                    query,
                    key,
                    value,
                    softmax_scale=self.scale,
                    causal=True,
                    window_size=self.sliding_window,
                    alibi_slopes=self.alibi_slopes,
                )
            else:
                assert False
        else:
            # Decoding run.
            output = FlashInferImpl.decode(
                query,
                kv_cache,
                input_metadata.decode_wrapper,
            )

        # Reshape the output tensor.
        return output.view(batch_size, seq_len, hidden_size)
