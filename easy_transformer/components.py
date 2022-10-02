from mimetypes import init
from typing import Callable, Union, List, Tuple, Dict, Optional
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import einops
import logging

from functools import *

from easy_transformer.hook_points import HookPoint
from easy_transformer.utils import (
    gelu_new,
    solu,
)
from easy_transformer.EasyTransformerConfig import EasyTransformerConfig

from fancy_einsum import einsum

# Embed & Unembed
class Embed(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig]):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.W_E = nn.Parameter(torch.empty(self.cfg.d_vocab, self.cfg.d_model))

    def forward(self, tokens):
        # If A has shape [a, b] and B has shape [c, d], then A[:, B] has shape [a, c, d]
        # B acts as a tensor of indices into the second dimension (so >=0 and <b)
        return self.W_E[tokens, :] # Shape [batch pos d_model]


class Unembed(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig]):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.W_U = nn.Parameter(torch.empty(self.cfg.d_model, self.cfg.d_vocab))
        self.b_U = nn.Parameter(torch.zeros(self.cfg.d_vocab))

    def forward(self, residual):
        return (
            einsum("batch pos d_model, d_model vocab -> batch pos vocab", 
                   residual, self.W_U) + self.b_U
        )  # [batch, pos, d_vocab]


# Positional Embeddings
class PosEmbed(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig]):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.W_pos = nn.Parameter(torch.empty(self.cfg.n_ctx, self.cfg.d_model))

    def forward(self, tokens):
        # Tokens have shape [batch, pos]
        # Output shape [pos, d_model] - will be broadcast along batch dim
        tokens_length = tokens.size(-1)
        return self.W_pos[:tokens_length, :]  # [pos, d_model]


# LayerNormPre
# I fold the LayerNorm weights and biases into later weights and biases.
# This is just the 'center and normalise' part of LayerNorm
# Centering is equivalent to just deleting one direction of residual space,
# and is equivalent to centering the weight matrices of everything writing to the residual stream
# Normalising is a funkier non-linear operation, that projects the residual stream onto the unit hypersphere
class LayerNormPre(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig]):
        """LayerNormPre - the 'center and normalise' part of LayerNorm. Length is
        normally d_model, but is d_mlp for softmax. Not needed as a parameter. This
        should only be used in inference mode after folding in LayerNorm weights"""
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.eps = self.cfg.eps

        # Adds a hook point for the normalisation scale factor
        self.hook_scale = HookPoint()  # [batch, pos]
        self.hook_normalized = HookPoint()  # [batch, pos, length]

    def forward(self, x):
        x = x - x.mean(axis=-1, keepdim=True)  # [batch, pos, length]
        scale = self.hook_scale(
            (
                x.pow(2).mean(-1, keepdim=True)
                + self.eps
            ).sqrt()
        )  # [batch, pos, 1]
        return self.hook_normalized(x / scale)  # [batch, pos, length]


class LayerNorm(nn.Module):
    def __init__(
        self, cfg: Union[Dict, EasyTransformerConfig], length: Optional[int] = None
    ):

        """
        LayerNorm with optional length parameter

        length (Optional[int]): If the dimension of the LayerNorm. If not provided, assumed to be d_model
        """
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.eps = self.cfg.eps
        if length is None:
            self.length = self.cfg.d_model
        else:
            self.length = length

        self.w = nn.Parameter(torch.ones(self.length))
        self.b = nn.Parameter(torch.zeros(self.length))

        # Adds a hook point for the normalisation scale factor
        self.hook_scale = HookPoint()  # [batch, pos, 1]
        self.hook_normalized = HookPoint()  # [batch, pos, length]

    def forward(self, x):
        x = x - x.mean(axis=-1, keepdim=True)  # [batch, pos, length]
        scale = self.hook_scale(
            (
                x.pow(2).mean(-1, keepdim=True)
                + self.eps
            ).sqrt()
        )  # [batch, pos, 1]
        x = self.hook_normalized(x / scale)  # [batch, pos, length]
        return x * self.w + self.b


# Attention
class Attention(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig], attn_type="global"):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.W_Q = nn.Parameter(
            torch.empty(self.cfg.n_heads, self.cfg.d_model, self.cfg.d_head)
        )
        self.W_K = nn.Parameter(
            torch.empty(self.cfg.n_heads, self.cfg.d_model, self.cfg.d_head)
        )
        self.W_V = nn.Parameter(
            torch.empty(self.cfg.n_heads, self.cfg.d_model, self.cfg.d_head)
        )
        self.W_O = nn.Parameter(
            torch.empty(self.cfg.n_heads, self.cfg.d_head, self.cfg.d_model)
        )
        self.b_Q = nn.Parameter(torch.zeros(self.cfg.n_heads, self.cfg.d_head))
        self.b_K = nn.Parameter(torch.zeros(self.cfg.n_heads, self.cfg.d_head))
        self.b_V = nn.Parameter(torch.zeros(self.cfg.n_heads, self.cfg.d_head))
        self.b_O = nn.Parameter(torch.zeros(self.cfg.d_model))

        self.attn_type = attn_type
        # Create a query_pos x key_pos mask, with True iff that query position
        # can attend to that key position
        causal_mask = torch.tril(torch.ones((self.cfg.n_ctx, self.cfg.n_ctx)).bool())
        if self.attn_type == "global":
            # For global attention, this is a lower triangular matrix - key <= query
            self.register_buffer("mask", causal_mask)
        elif self.attn_type == "local":
            # For local, this is banded, query - window_size < key <= query
            assert isinstance(self.cfg.window_size, int)
            self.register_buffer(
                "mask", torch.triu(causal_mask, 1 - self.cfg.window_size)
            )
        else:
            raise ValueError(f"Invalid attention type: {self.attn_type}")

        self.register_buffer("IGNORE", torch.tensor(-1e5))

        if self.cfg.use_attn_scale:
            self.attn_scale = np.sqrt(self.cfg.d_head)
        else:
            self.attn_scale = 1.0

        self.hook_k = HookPoint()  # [batch, pos, head_index, d_head]
        self.hook_q = HookPoint()  # [batch, pos, head_index, d_head]
        self.hook_v = HookPoint()  # [batch, pos, head_index, d_head]
        self.hook_z = HookPoint()  # [batch, pos, head_index, d_head]
        self.hook_attn_scores = HookPoint()  # [batch, head_index, query_pos, key_pos]
        self.hook_attn = HookPoint()  # [batch, head_index, query_pos, key_pos]
        self.hook_result = HookPoint()  # [batch, head_index, head_index, d_model]

    def forward(self, x):
        q = self.hook_q(
            einsum("batch pos d_model, head_index d_model d_head \
                -> batch pos head_index d_head", 
                        x, self.W_Q) + self.b_Q
        )  # [batch, pos, head_index, d_head]
        k = self.hook_k(
            einsum("batch pos d_model, head_index d_model d_head \
                -> batch pos head_index d_head", 
                        x, self.W_K) + self.b_K
        )  # [batch, pos, head_index, d_head]
        v = self.hook_v(
            einsum("batch pos d_model, head_index d_model d_head \
                -> batch pos head_index d_head", 
                        x, self.W_V) + self.b_V
        )  # [batch, pos, head_index, d_head]
        attn_scores = (
            einsum("batch query_pos head_index d_head, \
                batch key_pos head_index d_head \
                -> batch head_index query_pos key_pos", 
                   q, k) / self.attn_scale
        )  # [batch, head_index, query_pos, key_pos]
        if self.cfg.attention_dir == 'causal':
            # If causal attention, we mask it to only attend backwards. If bidirectional, we don't mask.
            attn_scores = self.causal_mask(attn_scores) # [batch, head_index, query_pos, key_pos]
        attn_matrix = self.hook_attn(
            F.softmax(attn_scores, dim=-1)
        )  # [batch, head_index, query_pos, key_pos]
        z = self.hook_z(
            einsum("batch key_pos head_index d_head, \
                batch head_index query_pos key_pos -> \
                batch query_pos head_index d_head", 
                v, attn_matrix)
        )  # [batch, pos, head_index, d_head]
        if self.cfg.use_attn_result:
            result = self.hook_result(
                einsum("batch pos head_index d_head, \
                        head_index d_head d_model -> \
                        batch pos head_index d_model", 
                       z, 
                       self.W_O)
            )  # [batch, pos, head_index, d_model]
            out = (
                einops.reduce(
                    result, "batch position index model->batch position model", "sum"
                )
                + self.b_O
            )  # [batch, pos, d_model]
        else:
            out = (
                    einsum("batch pos head_index d_head, \
                        head_index d_head d_model -> \
                        batch pos d_model", 
                        z, 
                        self.W_O)
                ) + self.b_O  # [batch, pos, d_model]
        return out

    def causal_mask(self, attn_scores):
        return torch.where(
            self.mask[: attn_scores.size(-2), : attn_scores.size(-1)],
            attn_scores,
            self.IGNORE,
        )


# MLP Layers
class MLP(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig]):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        self.W_in = nn.Parameter(torch.empty(self.cfg.d_model, self.cfg.d_mlp))
        self.b_in = nn.Parameter(torch.zeros(self.cfg.d_mlp))
        self.W_out = nn.Parameter(torch.empty(self.cfg.d_mlp, self.cfg.d_model))
        self.b_out = nn.Parameter(torch.zeros(self.cfg.d_model))

        self.hook_pre = HookPoint()  # [batch, pos, d_mlp]
        self.hook_post = HookPoint()  # [batch, pos, d_mlp]

        if self.cfg.act_fn == "relu":
            self.act_fn = F.relu
        elif self.cfg.act_fn == "gelu":
            self.act_fn = F.gelu
        elif self.cfg.act_fn == "silu":
            self.act_fn = F.silu
        elif self.cfg.act_fn == "gelu_new":
            self.act_fn = gelu_new
        elif self.cfg.act_fn == "solu_ln":
            self.act_fn = solu
            self.hook_post_ln = HookPoint()  # [batch, pos, d_mlp]
            self.ln = LayerNorm(self.cfg, self.cfg.d_mlp)
        else:
            raise ValueError(f"Invalid activation function name: {self.cfg.act_fn}")

    def forward(self, x):
        # Technically, all these einsums could be done with a single matmul, but this is more readable.
        pre_act = self.hook_pre(
            einsum("batch pos d_model, d_model d_mlp -> batch pos d_mlp", x, self.W_in) + self.b_in
        )  # [batch, pos, d_mlp]
        post_act = self.hook_post(self.act_fn(pre_act))  # [batch, pos, d_mlp]
        if self.cfg.act_fn.endswith("_ln"):
            post_act = self.hook_post_ln(self.ln(post_act))
        mlp_out = (
            einsum("batch pos d_mlp, d_mlp d_model -> batch pos d_model", post_act, self.W_out) + self.b_out
        )  # [batch, pos, d_model]
        return mlp_out


# Transformer Block
class TransformerBlock(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig], block_index):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        if self.cfg.normalization_type == "LN":
            self.ln1 = LayerNorm(cfg)
            self.ln2 = LayerNorm(cfg)
        elif self.cfg.normalization_type == "LNPre":
            # We've folded in LayerNorm weights, so just need the center + scale parts
            self.ln1 = LayerNormPre(cfg)
            self.ln2 = LayerNormPre(cfg)
        elif self.cfg.normalization_type is None:
            self.ln1 = nn.Identity()
            self.ln2 = nn.Identity()
        else:
            logging.warning(
                f"Invalid normalization_type passed in {self.cfg.normalization_type}"
            )

        if not self.cfg.use_local_attn:
            self.attn = Attention(cfg, "global")
        else:
            assert self.cfg.attn_types is not None
            attn_type = self.cfg.attn_types[block_index]
            self.attn = Attention(cfg, attn_type)
        self.mlp = MLP(cfg)

        self.hook_attn_out = HookPoint()  # [batch, pos, d_model]
        self.hook_mlp_out = HookPoint()  # [batch, pos, d_model]
        self.hook_resid_pre = HookPoint()  # [batch, pos, d_model]
        self.hook_resid_mid = HookPoint()  # [batch, pos, d_model]
        self.hook_resid_post = HookPoint()  # [batch, pos, d_model]

    def forward(self, x):
        resid_pre = self.hook_resid_pre(x)  # [batch, pos, d_model]
        normalized_resid_pre = self.ln1(resid_pre)
        attn_out = self.hook_attn_out(
            self.attn(normalized_resid_pre)
        )  # [batch, pos, d_model]
        resid_mid = self.hook_resid_mid(resid_pre + attn_out)  # [batch, pos, d_model]
        
        normalized_resid_mid = self.ln2(resid_mid)
        mlp_out = self.hook_mlp_out(
            self.mlp(normalized_resid_mid)
        )  # [batch, pos, d_model]
        resid_post = self.hook_resid_post(resid_mid + mlp_out)  # [batch, pos, d_model]
        return resid_post

class AttnOnlyBlock(nn.Module):
    def __init__(self, cfg: Union[Dict, EasyTransformerConfig], block_index):
        super().__init__()
        if isinstance(cfg, Dict):
            cfg = EasyTransformerConfig.from_dict(cfg)
        self.cfg = cfg
        if self.cfg.normalization_type == "LN":
            self.ln1 = LayerNorm(cfg)
        elif self.cfg.normalization_type == "LNPre":
            # We've folded in LayerNorm weights, so just need the center + scale parts
            self.ln1 = LayerNormPre(cfg)
        elif self.cfg.normalization_type is None:
            self.ln1 = nn.Identity()
        else:
            logging.warning(
                f"Invalid normalization_type passed in {self.cfg.normalization_type}"
            )

        if not self.cfg.use_local_attn:
            self.attn = Attention(cfg, "global")
        else:
            assert self.cfg.attn_types is not None
            attn_type = self.cfg.attn_types[block_index]
            self.attn = Attention(cfg, attn_type)

        self.hook_attn_out = HookPoint()  # [batch, pos, d_model]
        self.hook_resid_pre = HookPoint()  # [batch, pos, d_model]
        self.hook_resid_post = HookPoint()  # [batch, pos, d_model]

    def forward(self, x):
        resid_pre = self.hook_resid_pre(x)  # [batch, pos, d_model]
        normalized_resid_pre = self.ln1(resid_pre)
        attn_out = self.hook_attn_out(
            self.attn(normalized_resid_pre)
        )  # [batch, pos, d_model]
        resid_post = self.hook_resid_post(resid_pre + attn_out)  # [batch, pos, d_model]
        return resid_post
