from functools import lru_cache
from typing import Optional

import torch
import torch.nn.functional as F

from tabulate import tabulate
from torch.nn.attention.flex_attention import (
    _DEFAULT_SPARSE_BLOCK_SIZE,
    create_block_mask,
    create_mask,
    flex_attention,
    _score_mod_signature,
    _mask_mod_signature,
)

from triton.testing import do_bench

from attn_gym.masks import causal_mask
from attn_gym.mods import generate_alibi_bias


torch.set_default_device("cuda")
torch.manual_seed(0)

torch._dynamo.config.cache_size_limit = 1000

# Compile the flex_attention function
flex_attention = torch.compile(flex_attention, dynamic=False)

# For better performance, you can use:
# flex_attention = torch.compile(_flex_attention, dynamic=False, mode="max-autotune-no-cudagraphs")

data_type = torch.float16

# The kernels will utilize block sparisty to increase performance
print(f"Using the default sparsity block size: {_DEFAULT_SPARSE_BLOCK_SIZE}")


@lru_cache
def create_block_mask_cached(score_mod, B, H, M, N, device="cuda"):
    block_mask = create_block_mask(score_mod, B, H, M, N, device=device)
    return block_mask


def calculate_tflops(flops: float, time_ms: float, multiplier: int) -> float:
    return multiplier * flops * (1e3 / time_ms) / 1e12


def print_header(text):
    width = 91
    print("╔" + "═" * (width - 2) + "╗")
    print(f"║ {text.center(width - 4)} ║")
    print("╚" + "═" * (width - 2) + "╝")


def test_mask(
    score_mod: Optional[_score_mod_signature] = None,
    mask_mod: Optional[_mask_mod_signature] = None,
    B: int = 16,
    H: int = 16,
    S: int = 8192,
    D: int = 64,
    skip_correctness: bool = False,
    print_mask: bool = True,
    device: str = "cuda",
):
    assert score_mod is not None or mask_mod is not None, "Must provide a score_mod or mask_mod"
    if mask_mod is not None:
        block_mask = create_block_mask_cached(mask_mod, 1, 1, S, S, device=device)
    else:
        block_mask = None
    sdpa_mask_fn = mask_mod if mask_mod is not None else score_mod
    mask = create_mask(sdpa_mask_fn, 1, 1, S, S, device=device)

    qkv = [
        torch.randn(B, H, S, D, device=device, dtype=data_type, requires_grad=True)
        for _ in range(3)
    ]
    gradOut = torch.randn(B, H, S, D, device=device, dtype=torch.float16)

    causal_fa2 = lambda: F.scaled_dot_product_attention(*qkv, is_causal=True)
    sdpa_mask = lambda: F.scaled_dot_product_attention(*qkv, attn_mask=mask)
    flex_attention_call = lambda: flex_attention(*qkv, score_mod=score_mod, block_mask=block_mask)

    results = []
    if block_mask is not None:
        density = (100 - block_mask.sparsity()) / 100
    else:
        density = 1.0
    causal_fav2_flops = 0.5 * B * H * D * S * S
    flops = density * B * H * D * S * S

    # Forward pass
    causal_fa2_time = do_bench(causal_fa2)
    sdpa_mask_time = do_bench(sdpa_mask)
    flex_ms = do_bench(flex_attention_call)

    # Backward pass
    causal_fa2_out = causal_fa2()
    sdpa_mask_out = sdpa_mask()
    flex_out = flex_attention_call()

    causal_fa2_bw_time = do_bench(lambda: causal_fa2_out.backward(gradOut, retain_graph=True))
    sdpa_mask_bw_time = do_bench(lambda: sdpa_mask_out.backward(gradOut, retain_graph=True))
    flex_bw_ms = do_bench(lambda: flex_out.backward(gradOut, retain_graph=True))

    print_header(
        f"{score_mod.__name__ if score_mod is not None else mask_mod.__name__}".replace(
            "_", " "
        ).title()
    )
    # Inline correctness check
    if not skip_correctness:
        sdpa_mask_outs = []
        flex_outs = []

        for tensor in qkv:
            tensor.grad = None

        out1 = sdpa_mask()
        sdpa_mask_outs.append(out1)
        out1.backward(gradOut)
        sdpa_mask_outs += [tensor.grad for tensor in qkv]

        for tensor in qkv:
            tensor.grad = None

        out2 = flex_attention_call()
        flex_outs.append(out2)
        out2.backward(gradOut)
        flex_outs += [tensor.grad for tensor in qkv]
        for flex, sdpa_mask in zip(flex_outs, sdpa_mask_outs):
            torch.testing.assert_close(flex, sdpa_mask, atol=1e-1, rtol=1e-2)

        print("Correctness check passed ✅")
    # Usage in your results formatting:
    results = [
        [
            "causal FA2",
            f"{causal_fa2_time:.4f}",
            f"{calculate_tflops(causal_fav2_flops, causal_fa2_time, 4):.2f}",
            f"{causal_fa2_bw_time:.4f}",
            f"{calculate_tflops(causal_fav2_flops, causal_fa2_bw_time, 10):.2f}",
        ],
        [
            "F.sdpa + mask",
            f"{sdpa_mask_time:.4f}",
            f"{calculate_tflops(flops, sdpa_mask_time, 4):.2f}",
            f"{sdpa_mask_bw_time:.4f}",
            f"{calculate_tflops(flops, sdpa_mask_bw_time, 10):.2f}",
        ],
        [
            "flexattention",
            f"{flex_ms:.4f}",
            f"{calculate_tflops(flops, flex_ms, 4):.2f}",
            f"{flex_bw_ms:.4f}",
            f"{calculate_tflops(flops, flex_bw_ms, 10):.2f}",
        ],
    ]
    print(
        tabulate(
            results,
            headers=[
                "Operation",
                "FW Time (ms)",
                "FW FLOPS (TF/s)",
                "BW Time (ms)",
                "BW FLOPS (TF/s)",
            ],
            tablefmt="grid",
        )
    )
    if print_mask:
        print(f"\nBlock Mask:\n{block_mask}")


def main():
    # Test with a causal mask
    test_mask(mask_mod=causal_mask)
    # Correctness check here is simple and only works with mask_fns and not actual score_mods
    test_mask(score_mod=generate_alibi_bias(16), skip_correctness=True)


if __name__ == "__main__":
    main()
