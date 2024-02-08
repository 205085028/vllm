#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>

//
#include <iostream>

#include "dispatch_utils.h"

#include <c10/util/BFloat16.h>
#include <c10/cuda/CUDAStream.h>

// #include "cutlass/platform/platform.h"
// #include "cutlass/bfloat16.h"
// #include "cutlass/complex.h"
// #include "cutlass/gemm/kernel/gemm_grouped.h"
// #include "cutlass/gemm/kernel/default_gemm_grouped.h"
// #include "cutlass/gemm/device/gemm_grouped.h"

#include "cutlass/cutlass.h"

#include "cute/tensor.hpp"
#include "cutlass/tensor_ref.h"
#include "cutlass/epilogue/collective/default_epilogue.hpp"
#include "cutlass/epilogue/thread/linear_combination.h"
#include "cutlass/gemm/dispatch_policy.hpp"
#include "cutlass/gemm/group_array_problem_shape.hpp"
#include "cutlass/gemm/collective/collective_builder.hpp"
#include "cutlass/epilogue/collective/collective_builder.hpp"
#include "cutlass/gemm/device/gemm_universal_adapter.h"
#include "cutlass/gemm/kernel/gemm_universal.hpp"

#include "cutlass/util/packed_stride.hpp"
#include "cutlass/util/device_memory.h"

using namespace cute;

namespace vllm {

#define CUDA_CALL(code)					                    \
  do {                                                      \
    cudaError_t status = code;                              \
    std::string err = cudaGetErrorString(status);           \
    TORCH_CHECK(status == cudaSuccess, err);		        \
  } while (0)

#define GROUPED_GEMM_STRINGIFY_HELPER(x) #x
#define GROUPED_GEMM_STRINGIFY(x) \
  GROUPED_GEMM_STRINGIFY_HELPER(x)

using ProblemShape = cutlass::gemm::GroupProblemShape<Shape<int,int,int>>;  // <M,N,K> per group
using ElementA = cutlass::bfloat16_t;                                       // Element type for A matrix operand
using ElementB = cutlass::bfloat16_t;                                       // Element type for B matrix operand
using ElementC = float;                                                     // Element type for C and D matrix operands

// A matrix configuration
using         LayoutA     = cutlass::layout::RowMajor;                      // Layout type for A matrix operand
constexpr int AlignmentA  = 128 / cutlass::sizeof_bits<ElementA>::value;    // Memory access granularity/alignment of A matrix in units of elements (up to 16 bytes)

// B matrix configuration
using         LayoutB     = cutlass::layout::RowMajor;                   // Layout type for B matrix operand
constexpr int AlignmentB  = 128 / cutlass::sizeof_bits<ElementB>::value;    // Memory access granularity/alignment of B matrix in units of elements (up to 16 bytes)

// C/D matrix configuration
using         LayoutC     = cutlass::layout::RowMajor;                   // Layout type for C and D matrix operands
constexpr int AlignmentC  = 128 / cutlass::sizeof_bits<ElementC>::value;    // Memory access granularity/alignment of C matrix in units of elements (up to 16 bytes)

// Core kernel configurations
using ElementAccumulator  = float;                                          // Element type for internal accumulation
using ArchTag             = cutlass::arch::Sm90;                            // Tag indicating the minimum SM that supports the intended feature
using OperatorClass       = cutlass::arch::OpClassTensorOp;                 // Operator class tag
using TileShape           = Shape<_128,_128,_64>;                           // Threadblock-level tile size
using ClusterShape        = Shape<_1,_1,_1>;                                // Shape of the threadblocks in a cluster
using StageCountType = cutlass::gemm::collective::StageCountAuto;           // Stage count maximized based on the tile size
using KernelSchedule = cutlass::gemm::KernelGroupTmaWarpSpecializedCooperative; // Kernel to launch
using EpilogueSchedule = cutlass::epilogue::NoSmemWarpSpecializedGroup;                     // Epilogue to launch

using CollectiveEpilogue = typename cutlass::epilogue::collective::CollectiveBuilder<
    cutlass::arch::Sm90, cutlass::arch::OpClassTensorOp,
    TileShape, ClusterShape,
    cutlass::epilogue::collective::EpilogueTileAuto,
    ElementAccumulator, ElementAccumulator,
    ElementC, LayoutC, AlignmentC,
    ElementC, LayoutC, AlignmentC,
    EpilogueSchedule
  >::CollectiveOp;

using CollectiveMainloop = typename cutlass::gemm::collective::CollectiveBuilder<
    ArchTag, OperatorClass,
    ElementA, LayoutA, AlignmentA,
    ElementB, LayoutB, AlignmentB,
    ElementAccumulator,
    TileShape, ClusterShape,
    cutlass::gemm::collective::StageCountAutoCarveout<
      static_cast<int>(sizeof(typename CollectiveEpilogue::SharedStorage))>,
    KernelSchedule
  >::CollectiveOp;

using GemmKernel = cutlass::gemm::kernel::GemmUniversal<
    ProblemShape,
    CollectiveMainloop,
    CollectiveEpilogue
>;

using Gemm = cutlass::gemm::device::GemmUniversalAdapter<GemmKernel>;

std::vector<typename ProblemShape::UnderlyingProblemShape> MakeProblemSizes(torch::Tensor b, torch::Tensor batch_sizes) {
  const size_t num_experts = batch_sizes.size(0);
  const size_t k = b.size(1), n = b.size(2);
  std::vector<typename ProblemShape::UnderlyingProblemShape> problem_sizes(num_experts);
  for (int i = 0; i < num_experts; ++i) {
    int64_t batch_size = batch_sizes.data_ptr<int64_t>()[i];
    problem_sizes[i] = {batch_size, n, k};
  }
  return problem_sizes;
}

template <typename T>
torch::Tensor CopyToDevice(const std::vector<T> &x, const torch::Device &device) {
  size_t bytes = x.size() * sizeof(T);
  auto options = torch::TensorOptions().dtype(torch::kInt8).device(device);
  torch::Tensor out = torch::empty(bytes, options);

  CUDA_CALL(cudaMemcpyAsync(out.data_ptr(),
			    x.data(), bytes,
			    cudaMemcpyHostToDevice,
			    c10::cuda::getCurrentCUDAStream()));
  return out;
}

template <typename Gemm>
struct ProblemData {
  std::vector<typename ProblemShape::UnderlyingProblemShape> problem_sizes_host;
  cutlass::DeviceAllocation<typename ProblemShape::UnderlyingProblemShape> problem_sizes;
  cutlass::DeviceAllocation<typename Gemm::ElementA *> ptr_A;
  cutlass::DeviceAllocation<typename Gemm::ElementB *> ptr_B;
  cutlass::DeviceAllocation<typename Gemm::ElementC *> ptr_C;
  cutlass::DeviceAllocation<typename Gemm::GemmKernel::StrideA> stride_A;
  cutlass::DeviceAllocation<typename Gemm::GemmKernel::StrideB> stride_B;
  cutlass::DeviceAllocation<typename Gemm::GemmKernel::StrideC> stride_C;
};

template <typename T>
void CopyDataToDevice(const std::vector<T> &src, cutlass::DeviceAllocation<T> &target) {
  target.resize(src.size());
  target.copy_from_host(target.data());
}

template <typename Gemm>
typename Gemm::Arguments MakeArguments(ProblemData<Gemm>& problem_data,
               torch::Tensor a,
				       torch::Tensor b,
				       torch::Tensor c,
				       torch::Tensor batch_sizes) {
  problem_data.problem_sizes_host = MakeProblemSizes(b, batch_sizes);

  // Calculate the number of threadblocks to use and validate the result.
  int64_t num_experts = problem_data.problem_sizes_host.size();

  std::cout << "num_experts = " << num_experts << std::endl;

  // Create the host arrays of leading dimension data and pointer data.
  using StrideA = typename Gemm::GemmKernel::StrideA;
  using StrideB = typename Gemm::GemmKernel::StrideB;
  using StrideC = typename Gemm::GemmKernel::StrideC;

  std::vector<int64_t>  offsets_a(num_experts);
  std::vector<int64_t> offsets_b(num_experts);
  std::vector<int64_t> offsets_c(num_experts);
  std::vector<StrideA> stride_a_host;
  std::vector<StrideB> stride_b_host;
  std::vector<StrideC> stride_c_host;
  int64_t elements_a = 0, elements_b = 0, elements_c = 0;

  using ElementA = typename Gemm::ElementA;
  using ElementB = typename Gemm::ElementB;
  using ElementC = typename Gemm::ElementC;
  std::vector<ElementA *> ptr_a_host(num_experts);
  std::vector<ElementB *> ptr_b_host(num_experts);
  std::vector<ElementC *> ptr_c_host(num_experts);

  for (int i = 0; i < num_experts; ++i) {
    auto problem = problem_data.problem_sizes_host[i];
    auto M = get<0>(problem);
    auto N = get<1>(problem);
    auto K = get<2>(problem);

    std::cout << "i = " << i << std::endl;
    std::cout << "M = " << M << std::endl;
    std::cout << "N = " << N << std::endl;
    std::cout << "K = " << K << std::endl;

    auto sa = cutlass::make_cute_packed_stride(StrideA{}, cute::make_shape(M, K, Int<1>{}));
    std::cout << "sa[0]" << get<0>(sa) << std::endl;
    std::cout << "sa[1]" << get<1>(sa) << std::endl;
    std::cout << "sa[2]" << get<2>(sa) << std::endl;
    stride_a_host.push_back(sa);
    stride_b_host.push_back(cutlass::make_cute_packed_stride(StrideB{}, cute::make_shape(N, K, Int<1>{})));
    stride_c_host.push_back(cutlass::make_cute_packed_stride(StrideC{}, cute::make_shape(M, N, Int<1>{})));

    offsets_a[i] = elements_a;
    offsets_b[i] = elements_b;
    offsets_c[i] = elements_c;

    ptr_a_host[i] = (ElementA*)a.data_ptr() + offsets_a[i];
    ptr_b_host[i] = (ElementB*)b.data_ptr() + offsets_b[i];
    ptr_c_host[i] = (ElementC*)c.data_ptr() + offsets_c[i];

    elements_a += M * K;
    elements_b += K * N;
    elements_c += M * N;
  }

  // Copy the problem sizes, pointers and leading dimension data to the device.
  CopyDataToDevice(problem_data.problem_sizes_host, problem_data.problem_sizes);

  CopyDataToDevice(ptr_a_host, problem_data.ptr_A);
  CopyDataToDevice(ptr_b_host, problem_data.ptr_B);
  CopyDataToDevice(ptr_c_host, problem_data.ptr_C);

  CopyDataToDevice(stride_a_host, problem_data.stride_A);
  CopyDataToDevice(stride_b_host, problem_data.stride_B);
  CopyDataToDevice(stride_c_host, problem_data.stride_C);

  cutlass::KernelHardwareInfo hw_info;
  hw_info.device_id = b.device().index();
  hw_info.sm_count = cutlass::KernelHardwareInfo::query_device_multiprocessor_count(hw_info.device_id);

  typename Gemm::Arguments arguments{
    cutlass::gemm::GemmUniversalMode::kGrouped,
    {static_cast<int>(num_experts), problem_data.problem_sizes.get(), problem_data.problem_sizes_host.data()},
    {problem_data.ptr_A.get(), problem_data.stride_A.get(),
     problem_data.ptr_B.get(), problem_data.stride_B.get()},
    {{/*alpha=*/1.0f, /*beta=*/0.0f},
     problem_data.ptr_C.get(), problem_data.stride_C.get(),
     problem_data.ptr_C.get(), problem_data.stride_C.get()},
    hw_info
  };

  return arguments;
}

torch::Tensor CutlassGroupedGemm(torch::Tensor a,
				 torch::Tensor b,
				 torch::Tensor c,
				 torch::Tensor batch_sizes) {
  Gemm gemm;
  ProblemData<Gemm> problem_data;

  auto arguments = MakeArguments<Gemm>(problem_data, a, b, c, batch_sizes);
  int64_t workspace_size = gemm.get_workspace_size(arguments);
  auto options = torch::TensorOptions().dtype(torch::kInt8).device(a.device());
  torch::Tensor workspace = torch::empty(workspace_size, options);

  // Check if the problem size is supported or not
  auto status = gemm.can_implement(arguments);
  TORCH_CHECK(status == cutlass::Status::kSuccess, cutlass::cutlassGetStatusString(status));

  // Initialize the kernel.
  if(gemm.initialize(arguments, workspace.data_ptr()) != cutlass::Status::kSuccess) {
    TORCH_CHECK(false, "Failed to initialize CUTLASS Grouped GEMM");
  }

  // Execute the kernel in the current stream.
  if(gemm.run(c10::cuda::getCurrentCUDAStream()) != cutlass::Status::kSuccess) {
    TORCH_CHECK(false, "Failed to run CUTLASS Grouped GEMM");
  }
  return c;
}

}

void fused_moe(
    torch::Tensor A,
    torch::Tensor B,
    torch::Tensor C,
    torch::Tensor topk_weights,
    torch::Tensor topk_ids //,
    // torch::Tensor sorted_token_ids,
    // torch::Tensor expert_ids,
    // torch::Tensor num_tokens_post_padded,
    // bool MUL_ROUTED_WEIGHT,
    // int top_k,
    // int parallelism
    ) {
    const cudaStream_t stream = at::cuda::getCurrentCUDAStream();
    
    vllm::CutlassGroupedGemm(A, B, C, topk_weights);
}