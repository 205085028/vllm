#include <flashinfer_decl.h>

#include <flashinfer.cuh>

using namespace flashinfer;

INST_SinglePrefill(nv_bfloat16, 1, 64, false, true, QKVLayout::kHND, PosEncodingMode::kRoPELlama)