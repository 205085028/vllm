#include <sycl/sycl.hpp>
#include <dpct/dpct.hpp>
#include <torch/extension.h>
#include <ATen/ATen.h>
#include <ipex.h>

#include "dispatch_utils.h"

namespace at{

namespace cuda{

dpct::queue_ptr getCurrentCUDAStream(){

  auto device_type = c10::DeviceType::XPU;
  c10::impl::VirtualGuardImpl impl(device_type);
  c10::Stream c10_stream = impl.getStream(c10::Device(device_type));
  auto& queue = xpu::get_queue_from_stream(c10_stream);
  return &queue;

}

}

}



namespace vllm {

template<typename scalar_t, bool IS_NEOX>
inline void apply_rotary_embedding(
  scalar_t* __restrict__ arr,
  const scalar_t* __restrict__ cos_ptr,
  const scalar_t* __restrict__ sin_ptr,
  int rot_offset,
  int embed_dim)
{
  int x_index, y_index;
  scalar_t cos, sin;
  if (IS_NEOX) {
    // GPT-NeoX style rotary embedding.
    x_index = rot_offset;
    y_index = embed_dim + rot_offset;
    cos = __ldg(cos_ptr + x_index);
    sin = __ldg(sin_ptr + x_index);
  } else {
    // GPT-J style rotary embedding.
    x_index = 2 * rot_offset;
    y_index = 2 * rot_offset + 1;
    cos = __ldg(cos_ptr + x_index / 2);
    sin = __ldg(sin_ptr + x_index / 2);
  }

  const scalar_t x = arr[x_index];
  const scalar_t y = arr[y_index];
  arr[x_index] = x * cos - y * sin;
  arr[y_index] = y * cos + x * sin;
}

template<typename scalar_t, bool IS_NEOX>
void rotary_embedding_kernel(
  const int64_t* __restrict__ positions,        // [batch_size, seq_len] or [num_tokens]
  scalar_t* __restrict__ query,                 // [batch_size, seq_len, num_heads, head_size] or [num_tokens, num_heads, head_size]
  scalar_t* __restrict__ key,                   // [batch_size, seq_len, num_kv_heads, head_size] or [num_tokens, num_kv_heads, head_size]
  const scalar_t* __restrict__ cos_sin_cache,   // [max_position, 2, rot_dim // 2]
  const int rot_dim,
  const int query_stride,
  const int key_stride,
  const int num_heads,
  const int num_kv_heads,
  const int head_size,
  const sycl::nd_item<3> &item_ct1) {
  // Each thread block is responsible for one token.
  const int token_idx = item_ct1.get_group(2);
  int64_t pos = positions[token_idx];
  const scalar_t* cache_ptr = cos_sin_cache + pos * rot_dim;

  const int embed_dim = rot_dim / 2;
  const scalar_t* cos_ptr = cache_ptr;
  const scalar_t* sin_ptr = cache_ptr + embed_dim;

  const int nq = num_heads * embed_dim;
  for (int i = item_ct1.get_local_id(2); i < nq;
       i += item_ct1.get_local_range(2)) {
    const int head_idx = i / embed_dim;
    const int token_head = token_idx * query_stride + head_idx * head_size;
    const int rot_offset = i % embed_dim;
    apply_rotary_embedding<scalar_t, IS_NEOX>(query + token_head, cos_ptr,
                                              sin_ptr, rot_offset, embed_dim);
  }

  const int nk = num_kv_heads * embed_dim;
  for (int i = item_ct1.get_local_id(2); i < nk;
       i += item_ct1.get_local_range(2)) {
    const int head_idx = i / embed_dim;
    const int token_head = token_idx * key_stride + head_idx * head_size;
    const int rot_offset = i % embed_dim;
    apply_rotary_embedding<scalar_t, IS_NEOX>(key + token_head, cos_ptr,
                                              sin_ptr, rot_offset, embed_dim);
  }
}

} // namespace vllm

void rotary_embedding(
  torch::Tensor& positions,         // [batch_size, seq_len] or [num_tokens]
  torch::Tensor& query,             // [batch_size, seq_len, num_heads * head_size] or [num_tokens, num_heads * head_size]
  torch::Tensor& key,               // [batch_size, seq_len, num_kv_heads * head_size] or [num_tokens, num_kv_heads * head_size]
  int head_size,
  torch::Tensor& cos_sin_cache,     // [max_position, rot_dim]
  bool is_neox) {
  int64_t num_tokens = query.numel() / query.size(-1);
  int rot_dim = cos_sin_cache.size(1);
  int num_heads = query.size(-1) / head_size;
  int num_kv_heads = key.size(-1) / head_size;
  int query_stride = query.stride(-2);
  int key_stride = key.stride(-2);

  sycl::range<3> grid(1, 1, num_tokens);
  sycl::range<3> block(std::min(num_heads * rot_dim / 2, 512));
  const dpct::queue_ptr stream = at::cuda::getCurrentCUDAStream();
  
  if (is_neox) {
    
  stream.submit([&](sycl::handler &cgh){
  
  cgh.parallel_for(sycl::nd_range<3>(grid * block, block),
   [=](sycl::nd_item<3> item_ct1){
   vllm::rotary_embedding_kernel<scalar_t, true>(
        positions.data_ptr<int64_t>(),
        query.data_ptr<scalar_t>(),
        key.data_ptr<scalar_t>(),
        cos_sin_cache.data_ptr<scalar_t>(),
        rot_dim,
        query_stride,
        key_stride,
        num_heads,
        num_kv_heads,
        head_size,
        item_ct1);
   });
    }
  );
  
  
  }
  else{
  
  
  stream.submit([&](sycl::handler &cgh){
  
  cgh.parallel_for(sycl::nd_range<3>(grid * block, block),
   [=](sycl::nd_item<3> item_ct1){
   vllm::rotary_embedding_kernel<scalar_t, true>(
        positions.data_ptr<int64_t>(),
        query.data_ptr<scalar_t>(),
        key.data_ptr<scalar_t>(),
        cos_sin_cache.data_ptr<scalar_t>(),
        rot_dim,
        query_stride,
        key_stride,
        num_heads,
        num_kv_heads,
        head_size,
        item_ct1);
   });
    }
  );
  
  
  
  }
  
  /*
  VLLM_DISPATCH_FLOATING_TYPES(
    query.scalar_type(),
    "rotary_embedding",
    [&] {
      if (is_neox) {
        vllm::rotary_embedding_kernel<scalar_t, true><<<grid, block, 0, stream>>>(
          positions.data_ptr<int64_t>(),
          query.data_ptr<scalar_t>(),
          key.data_ptr<scalar_t>(),
          cos_sin_cache.data_ptr<scalar_t>(),
          rot_dim,
          query_stride,
          key_stride,
          num_heads,
          num_kv_heads,
          head_size);
      } else {
        vllm::rotary_embedding_kernel<scalar_t, false><<<grid, block, 0, stream>>>(
          positions.data_ptr<int64_t>(),
          query.data_ptr<scalar_t>(),
          key.data_ptr<scalar_t>(),
          cos_sin_cache.data_ptr<scalar_t>(),
          rot_dim,
          query_stride,
          key_stride,
          num_heads,
          num_kv_heads,
          head_size);
      }
    });
  */
}
