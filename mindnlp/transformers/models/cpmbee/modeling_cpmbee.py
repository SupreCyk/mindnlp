# coding=utf-8
# Copyright 2022 The OpenBMB Team The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# pylint: disable=missing-class-docstring
# pylint: disable=invalid-name
# pylint: disable=missing-function-docstring
# pylint: disable=unexpected-keyword-arg
# pylint: disable=arguments-renamed
# pylint: disable=unused-argument
# pylint: disable=unsubscriptable-object
""" MindSpore CpmBee model."""
import copy
import math
from collections import UserDict
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import mindspore
from mindspore import nn, ops, Parameter
from mindspore.common.initializer import initializer, Normal

from mindnlp.utils import logging
from mindnlp.modules.functional import embedding
from ...generation.beam_search import BeamHypotheses, BeamSearchScorer
from ...generation.streamers import BaseStreamer
from ...generation.utils import (
    GenerationConfig,
    LogitsProcessorList,
    StoppingCriteriaList,
    inspect,
    warnings,
)
from ...modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast, ModelOutput
from ...modeling_utils import PreTrainedModel
from .configuration_cpmbee import CpmBeeConfig
from .tokenization_cpmbee import CpmBeeTokenizer


logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "openbmb/cpm-bee-10b"
_CONFIG_FOR_DOC = "CpmBeeConfig"

CPMBEE_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "openbmb/cpm-bee-10b",
    "openbmb/cpm-bee-5b",
    "openbmb/cpm-bee-2b",
    "openbmb/cpm-bee-1b",
    # See all CPMBee models at https://huggingface.co/models?filter=cpmbee
]


class CpmBeeLinear(nn.Dense):
    def __init__(self, dim_in, dim_out, dtype):
        """
        Construct a linear for CPMBee. It contains a scale operation.
        """
        super().__init__(dim_in, dim_out, has_bias=False)
        self.dim_in = self.in_features = dim_in
        self.dim_out = self.out_features = dim_out

        self.weight = Parameter(ops.zeros((dim_out, dim_in), dtype=dtype))

    def construct(self, x: mindspore.Tensor):
        """
        Args:
            x (`mindspore.Tensor` of shape `(batch, seq_len, dim_in)`): The input of linear layer
        Returns:
            `mindspore.Tensor` of shape `(batch, seq_len, dim_out)`: The output of the linear transform y.
        """
        x = ops.dense(x, self.weight)
        x = x / math.sqrt(self.dim_in)
        return x


class CpmBeeLayerNorm(nn.Cell):
    """
    We use Root Mean Square (RMS) Layer Normalization, please see https://arxiv.org/abs/1910.07467 for details."
    """

    def __init__(self, config: CpmBeeConfig):
        super().__init__()

        self.eps = config.eps
        self.dim_norm = config.hidden_size
        self.weight = Parameter(ops.zeros(config.hidden_size, dtype=config.ms_dtype))

    def construct(self, hidden_states: mindspore.Tensor):
        """
        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        if hidden_states.shape[-1] != self.dim_norm:
            raise AssertionError("hidden_states.shape[-1] != self.dim_norm")
        old_dtype = hidden_states.dtype
        variance = hidden_states.to(mindspore.float32).pow(2).mean(axis=-1, keep_dims=True)
        hidden_states = (hidden_states * ops.rsqrt(variance + self.eps)).to(old_dtype) * self.weight
        return hidden_states


class CpmBeeAttention(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.dim_model = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.dim_head = config.dim_head

        self.project_q = CpmBeeLinear(self.dim_model, self.num_heads * self.dim_head, dtype=config.ms_dtype)
        self.project_k = CpmBeeLinear(self.dim_model, self.num_heads * self.dim_head, dtype=config.ms_dtype)
        self.project_v = CpmBeeLinear(self.dim_model, self.num_heads * self.dim_head, dtype=config.ms_dtype)

        self.attention_out = CpmBeeLinear(self.num_heads * self.dim_head, self.dim_model, dtype=config.ms_dtype)

        self.softmax = nn.Softmax(axis=-1)

        if config.dropout_p is not None:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_q: mindspore.Tensor,
        hidden_kv: mindspore.Tensor,
        attention_mask: mindspore.Tensor,
        position_bias: mindspore.Tensor,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_q (`mindspore.Tensor`):
                Input of transformer block(self-attention block). It can be the raw embedding of a batch of sequences.
            hidden_kv (`mindspore.Tensor` of shape `(batch, len_k, dim_model)`)):
                Tensor *key_value* and *query* of shape `(batch, len_k, dim_model)`
            attention_mask (`mindspore.Tensor` of shape `(batch, len_seq, len_seq)`):
                Avoid invalid areas to participate in the calculation of self-attention.
            position_bias (`mindspore.Tensor` of shape `(batch, len_seq, len_seq)`):
                Provide positional information to self-attention block.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple[mindspore.Tensor, mindspore.Tensor]`, *optional*):
                Cached past key and value projection states.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        batch_size = hidden_q.shape[0]
        len_q = hidden_q.shape[1]
        len_k = hidden_kv.shape[1]

        query = self.project_q(hidden_q)
        key = self.project_k(hidden_kv)
        value = self.project_v(hidden_kv)

        query = query.view(batch_size, len_q, self.num_heads, self.dim_head).permute(0, 2, 1, 3)
        key = key.view(batch_size, len_k, self.num_heads, self.dim_head).permute(0, 2, 1, 3)
        value = value.view(batch_size, len_k, self.num_heads, self.dim_head).permute(0, 2, 1, 3)

        if past_key_values is not None:
            key = ops.cat([past_key_values[0], key], axis=-2)
            value = ops.cat([past_key_values[1], value], axis=-2)
            len_k = key.shape[-2]

        # (batch_size, num_heads, len_q, dim_head) @ (batch_size, num_heads, dim_head, len_k) -> (batch_size, num_heads, len_q, len_k)
        score = ops.matmul(query, key.swapaxes(-1, -2)) / math.sqrt(self.dim_head)
        score = score + position_bias

        score = ops.masked_fill(
            score,
            attention_mask.view(batch_size, 1, len_q, len_k) == mindspore.tensor(False),
            ops.scalar_to_tensor(float("-inf"), dtype=score.dtype),
        )
        score = self.softmax(score)

        score = ops.masked_fill(
            score,
            attention_mask.view(batch_size, 1, len_q, len_k) == mindspore.tensor(False),
            ops.scalar_to_tensor(0, dtype=score.dtype),
        )
        if output_attentions:
            attn_weights = score
        else:
            attn_weights = None

        if self.dropout is not None:
            score = self.dropout(score)

        # (batch_size, num_heads, len_q, len_k) @ (batch_size, num_heads, len_k, dim_head) -> (batch_size, num_heads, len_q, dim_head)
        score = ops.matmul(score, value)

        score = score.view(batch_size, self.num_heads, len_q, self.dim_head).permute(0, 2, 1, 3)
        score = score.view(batch_size, len_q, self.num_heads * self.dim_head)

        score = self.attention_out(score)

        past_key_values = None
        if use_cache:
            past_key_values = (key, value)

        return score, attn_weights, past_key_values


class CpmBeeSelfAttentionBlock(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.layernorm_before_attention = CpmBeeLayerNorm(config)
        self.self_attention = CpmBeeAttention(config)
        if config.dropout_p:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: mindspore.Tensor,
        position_bias: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, len_seq, dim_model)`):
                Input of transformer block(self-attention block). It can be the raw embedding of a batch of sequences.
            attention_mask (`mindspore.Tensor` of shape `(batch, len_seq, len_seq)`):
                Avoid invalid areas to participate in the calculation of self-attention.
            position_bias (`mindspore.Tensor` of shape `(batch, len_seq, len_seq)`):
                Provide positional information to self-attention block.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple(mindspore.Tensor)`, *optional*):
                Cached past key and value projection states.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        outputs = self.layernorm_before_attention(hidden_states)
        outputs = self.self_attention(
            outputs, outputs, attention_mask, position_bias, output_attentions, past_key_values, use_cache
        )

        outputs, attn_weights, current_key_value = outputs

        if self.dropout is not None:
            outputs = self.dropout(outputs)
        hidden_states = (hidden_states + outputs) / 1.05

        return hidden_states, attn_weights, current_key_value


class CpmBeeDenseGatedACT(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.w_0 = CpmBeeLinear(config.hidden_size, config.dim_ff, dtype=config.ms_dtype)
        self.w_1 = CpmBeeLinear(config.hidden_size, config.dim_ff, dtype=config.ms_dtype)
        self.act = nn.GELU()

    def construct(self, hidden_states: mindspore.Tensor):
        """Transform an input tensor from one feature space to another via a nonlinear operation

        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        gate_score = self.act(self.w_0(hidden_states))
        hidden_states = self.w_1(hidden_states)

        hidden_states = gate_score * hidden_states
        return hidden_states


class CpmBeeFeedForward(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.w_in = CpmBeeDenseGatedACT(config)
        if config.dropout_p is not None:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

        self.w_out = CpmBeeLinear(config.dim_ff, config.hidden_size, dtype=config.ms_dtype)

    def construct(self, hidden_states: mindspore.Tensor):
        """
        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        hidden_states = self.w_in(hidden_states)

        if self.dropout is not None:
            hidden_states = self.dropout(hidden_states)

        hidden_states = self.w_out(hidden_states)

        return hidden_states


class CpmBeeFFNBlock(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.layernorm_before_ffn = CpmBeeLayerNorm(config)
        self.ffn = CpmBeeFeedForward(config)
        if config.dropout_p:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_states: mindspore.Tensor,
    ):
        """
        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, len_seq, dim_model)`):
                Hidden states before feed forward layer.
        """
        ln_outputs = self.layernorm_before_ffn(hidden_states)
        outputs = self.ffn(ln_outputs)
        if self.dropout is not None:
            outputs = self.dropout(outputs)
        hidden_states = (hidden_states + outputs) / 1.05
        return hidden_states


class CpmBeeTransformerBlock(nn.Cell):
    def __init__(self, config: CpmBeeConfig, mask_att: bool = False, mask_ffn: bool = False):
        super().__init__()
        self.mask_att = mask_att
        self.mask_ffn = mask_ffn

        if not self.mask_att:
            self.self_att = CpmBeeSelfAttentionBlock(config)
        if not self.mask_ffn:
            self.ffn = CpmBeeFFNBlock(config)

    def construct(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: mindspore.Tensor,
        position_bias: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`mindspore.Tensor`):
                Input to the layer of shape `(batch, seq_len, dim_model)`
            attention_mask (`mindspore.Tensor`):
                Avoid invalid areas to participate in the calculation of shape `(batch, seq_len, seq_len)`
            position_bias (`mindspore.Tensor`):
                Provides position information to attention mechanism of shape `(num_heads, seq_len, seq_len)`
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple[mindspore.Tensor, mindspore.Tensor])`, *optional*):
                Cached past key and value projection states
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        if not self.mask_att:
            hidden_states = self.self_att(
                hidden_states,
                attention_mask=attention_mask,
                position_bias=position_bias,
                output_attentions=output_attentions,
                past_key_values=past_key_values,
                use_cache=use_cache,
            )

            hidden_states, attn_weights, current_key_value = hidden_states
        else:
            attn_weights, current_key_value = None, (None, None)

        if not self.mask_ffn:
            hidden_states = self.ffn(hidden_states)

        return hidden_states, attn_weights, current_key_value


class CpmBeeEncoder(nn.Cell):
    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        self.num_layers = config.num_hidden_layers
        if config.mask_modules is not None:
            assert len(config.mask_modules) == self.num_layers, "The total number of masks should equal to num_layers"
            for mask_module in config.mask_modules:
                assert len(mask_module) == 2, "For encoder, each mask should be (mask_att, mask_ffn)"
        else:
            config.mask_modules = [(False, False)] * self.num_layers

        self.layers = nn.CellList(
            [
                CpmBeeTransformerBlock(
                    config, mask_att=config.mask_modules[ith][0], mask_ffn=config.mask_modules[ith][1]
                )
                for ith in range(self.num_layers)
            ]
        )

        self.output_layernorm = CpmBeeLayerNorm(config)

    def construct(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: mindspore.Tensor,
        position_bias: mindspore.Tensor,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[Tuple[mindspore.Tensor, mindspore.Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`mindspore.Tensor`):
                Input to the layer of shape `(batch, seq_len, dim_model)`
            attention_mask (`mindspore.Tensor`):
                Avoid invalid areas to participate in the calculation of shape `(batch, seq_len, seq_len)`
            position_bias (`mindspore.Tensor`):
                Provides position information to attention mechanism of shape `(num_heads, seq_len, seq_len)`
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers.
            past_key_values (`Tuple[mindspore.Tensor, mindspore.Tensor])`, *optional*):
                Cached past key and value projection states
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        current_key_values = () if use_cache else None

        for i, layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            layer_outputs = layer(
                hidden_states,
                attention_mask,
                position_bias,
                output_attentions=output_attentions,
                past_key_values=past_key_values[i] if past_key_values else None,
                use_cache=use_cache,
            )
            hidden_states, attn_weights, current_key_value = layer_outputs
            if output_attentions:
                all_self_attns += (attn_weights,)
            if current_key_values is not None:
                current_key_values = current_key_values + (current_key_value,)

        hidden_states = self.output_layernorm(hidden_states)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        return hidden_states, current_key_values, all_hidden_states, all_self_attns


class CpmBeeBucketPositionBias(nn.Cell):
    def __init__(self, config: CpmBeeConfig) -> None:
        super().__init__()

        self.num_heads = config.num_attention_heads
        self.num_buckets = config.position_bias_num_buckets
        self.num_segment_bucket = config.position_bias_num_segment_buckets
        self.max_distance = config.position_bias_max_distance

        self.relative_attention_bias = Parameter(
            ops.zeros(
                config.position_bias_num_buckets + config.position_bias_num_segment_buckets,
                config.num_attention_heads,
                dtype=config.ms_dtype,
            ),
        )

    def construct(self, query_pos: mindspore.Tensor, key_pos: mindspore.Tensor, rel_buckets: mindspore.Tensor):
        batch = key_pos.shape[0]
        keylen = key_pos.shape[1]
        querylen = query_pos.shape[1]

        if key_pos.shape[0] != query_pos.shape[0]:
            raise AssertionError(
                f"key_pos.shape[0] should be equal to query_pos.shape[0], but got {key_pos.shape[0]} and {query_pos.shape[0]}!"
            )
        if rel_buckets.shape[0] != batch:
            raise AssertionError(
                f"rel_buckets.shape[0] should be equal to batch, but got {rel_buckets.shape[0]} and {batch}!"
            )
        if rel_buckets.shape[1] != querylen:
            raise AssertionError(
                f"rel_buckets.shape[1] should be equal to querylen, but got {rel_buckets.shape[1]} and {querylen}!"
            )
        if rel_buckets.shape[2] != keylen:
            raise AssertionError(
                f"rel_buckets.shape[2] should be equal to keylen, but got {rel_buckets.shape[2]} and {keylen}!"
            )

        relative_position_bucket = rel_buckets - 1 + self.num_buckets

        inner_segment_bucket = self._position_bucket(
            key_pos[..., None, :] - query_pos[..., :, None],
            num_buckets=self.num_buckets,
            max_distance=self.max_distance,
        )
        relative_position_bucket = ops.where(
            rel_buckets == 0,
            inner_segment_bucket,
            relative_position_bucket,
        )

        embeds = embedding(relative_position_bucket, self.relative_attention_bias)
        embeds = embeds.permute(0, 3, 1, 2)
        return embeds

    def _position_bucket(self, relative_position, num_buckets=32, max_distance=128):
        relative_buckets = 0
        num_buckets //= 2
        relative_buckets = (relative_position > 0).to(mindspore.int32) * num_buckets
        relative_position = ops.abs(relative_position)
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact
        relative_postion_if_large = max_exact + (
            ops.log(relative_position.float() / max_exact)
            / math.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        ).to(mindspore.int32)
        relative_postion_if_large = ops.minimum(
            relative_postion_if_large,
            ops.full_like(relative_postion_if_large, num_buckets - 1),
        )
        relative_buckets += ops.where(is_small, relative_position.to(mindspore.int32), relative_postion_if_large)
        return relative_buckets


# Copied from transformers.models.bert.modeling_bert.BertOutput with Bert->CPMBee
class CpmBeeOutput(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.intermediate_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(p=config.hidden_dropout_prob)

    def construct(self, hidden_states: mindspore.Tensor, input_tensor: mindspore.Tensor) -> mindspore.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class CpmBeeRotaryEmbedding(nn.Cell):
    """
    RotaryEmbedding embeds the unk token and special token. It will embeds the "...<mask>...<mask>...<unk>...<unk>..."
    to "...<mask_0>...<mask_1>...<unk_0>...<unk_1>..."" to help model to specify different special tokens and unk
    tokens.
    """

    def __init__(self, config: CpmBeeConfig):
        super().__init__()
        inv_freq = 1.0 / (10000 ** (ops.arange(0, config.hidden_size, 2, dtype=mindspore.float32) / config.hidden_size))
        self.distance_scale = config.distance_scale
        self.dtype = config.ms_dtype
        self.inv_freq = inv_freq.to(config.ms_dtype)

    def construct(self, x: mindspore.Tensor, x_pos: mindspore.Tensor):
        inv_freq = self.inv_freq.to(dtype=x.dtype)

        x_pos = x_pos * self.distance_scale
        freqs = x_pos[..., None] * inv_freq[None, :]  # (..., dim/2)

        emb = ops.cat((freqs, freqs), axis=-1)  # (..., dim)
        emb_cos = emb.cos()  # (..., dim)
        emb_sin = emb.sin()  # (..., dim)

        rotate_x = ops.cat([-x[..., x.shape[-1] // 2 :], x[..., : x.shape[-1] // 2]], axis=-1)  # (..., dim)

        return x * emb_cos + rotate_x * emb_sin


class CpmBeeEmbeddingExt(nn.Embedding):
    """
    Contains a RotaryEmbedding.
    """

    def __init__(self, config: CpmBeeConfig):
        super().__init__(config.vocab_size, config.hidden_size, dtype=config.ms_dtype)
        self.dim_model = config.hidden_size
        self.rotary_emb = CpmBeeRotaryEmbedding(config)

    def construct(self, ids: mindspore.Tensor, ids_sub: mindspore.Tensor):
        embeds = super().construct(ids) / math.sqrt(self.dim_model)
        return self.rotary_emb(embeds, ids_sub)

    def projection(self, x: mindspore.Tensor, ext_table: Optional[mindspore.Tensor] = None):
        logits = ops.dense(x / math.sqrt(self.dim_model), self.weight)
        if ext_table is not None and 0 not in ext_table.shape:
            logits_ext = ops.dense(x, ext_table)
            logits = ops.cat([logits, logits_ext], axis=-1)
        return logits


class CpmBeePreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = CpmBeeConfig
    base_model_prefix = "cpmbee"
    supports_gradient_checkpointing = True

    def _init_weights(self, cell):
        """Initialize the weights"""
        std = self.config.init_std
        if isinstance(cell, nn.Dense):
            cell.weight.set_data(initializer(Normal(std), cell.weight.shape, cell.weight.dtype))
            if cell.bias is not None:
                cell.bias.set_data(initializer('zeros', cell.bias.shape, cell.bias.dtype))
        # still needed
        elif isinstance(cell, CpmBeeEmbeddingExt):
            cell.weight.set_data(initializer(Normal(std), cell.weight.shape, cell.weight.dtype))
        elif isinstance(cell, nn.LayerNorm):
            cell.weight.set_data(initializer('ones', cell.weight.shape, cell.weight.dtype))
            cell.bias.set_data(initializer('zeros', cell.bias.shape, cell.bias.dtype))
        elif isinstance(cell, CpmBeeLayerNorm):
            cell.weight.set_data(initializer('ones', cell.weight.shape, cell.weight.dtype))
        elif isinstance(cell, CpmBeeBucketPositionBias):
            cell.relative_attention_bias.set_data(initializer(
                Normal(std), cell.relative_attention_bias.shape, cell.relative_attention_bias.dtype))


class CpmBeeModel(CpmBeePreTrainedModel):
    def __init__(self, config: CpmBeeConfig):
        super().__init__(config)
        if config.half:
            config.ms_dtype = mindspore.float16
        else:
            config.ms_dtype = mindspore.float32
        self.encoder = CpmBeeEncoder(config)
        self.input_embedding = CpmBeeEmbeddingExt(config)
        self.position_bias = CpmBeeBucketPositionBias(config)
        self.vocab_size = config.vocab_size
        self.post_init()

    def get_input_embeddings(self):
        return self.input_embedding

    def set_input_embeddings(self, embeddings, **kwargs):
        self.input_embedding = embeddings

    def construct(
        self,
        input_ids: mindspore.Tensor,
        input_id_sub: Optional[mindspore.Tensor] = None,
        length: Optional[mindspore.Tensor] = None,
        context: Optional[mindspore.Tensor] = None,
        sample_ids: Optional[mindspore.Tensor] = None,
        num_segments: Optional[mindspore.Tensor] = None,
        segment: Optional[mindspore.Tensor] = None,
        segment_rel_offset: Optional[mindspore.Tensor] = None,
        segment_rel: Optional[mindspore.Tensor] = None,
        span: Optional[Dict] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[List] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        # dummy setting for common tests
        if input_id_sub is None:
            dtype = input_ids.dtype
            batch, seq_length = input_ids.shape
            segment = ops.where(input_ids != 0, mindspore.tensor(2), 0).to(dtype=dtype)
            context = ops.full((batch, seq_length), 1, dtype=dtype)
            position = ops.arange(seq_length, dtype=dtype).repeat(batch, 1)
            input_id_sub = ops.full((batch, seq_length), 0, dtype=dtype)
            segment_rel_offset = ops.full((batch, seq_length), 0, dtype=dtype)
            segment_rel = ops.full((batch, seq_length), 0, dtype=dtype)
            num_segments = ops.full((batch, seq_length), 0, dtype=dtype)
            sample_ids = ops.zeros_like(input_ids)

        batch = input_ids.shape[0]
        seqlen = input_ids.shape[1]

        # calc segment bucket
        segment_rel_2d = ops.masked_fill(
            segment[:, :, None] * num_segments[:, :, None]
            + segment[:, None, :]
            + segment_rel_offset[:, :, None],
            ~(
                (sample_ids[:, :, None] == sample_ids[:, None, :])
                & (span[:, None, :] == span[:, :, None])
            ),  # not in the same span or sample
            0,  # avoid torch.gather overflow
        ).view(batch, seqlen * seqlen)

        segment_bucket = ops.gather_elements(
            input=segment_rel,
            dim=1,
            index=segment_rel_2d.long(),
        ).view(batch, seqlen, seqlen)

        segment_bucket = segment_bucket.masked_fill(
            ~(
                (sample_ids[:, :, None] == sample_ids[:, None, :])
                & (span[:, None, :] == span[:, :, None])
            ),  # not in the same span or sample
            1,  # bucket is used for in-context samples
        )

        # directional mask
        directional_mask_2d = ops.arange(seqlen) <= ops.arange(
            seqlen
        ).view(-1, 1)
        # sample mask
        sample_mask_2d = (sample_ids[:, :, None] == 0) | (
            sample_ids[:, :, None] == sample_ids[:, None, :]
        )
        # context mask
        attention_mask = context[:, None, :] | (
            context[:, :, None].logical_not() & directional_mask_2d.view(1, seqlen, seqlen)
        )
        # span mask
        attention_mask = (
            attention_mask & sample_mask_2d & (span[:, None, :] == span[:, :, None])
        )
        # length mask
        mask_1d = (
            ops.arange(seqlen)[None, :].repeat(batch, 1) < length[:, None]
        )
        attention_mask = (
            mask_1d.view(batch, seqlen, 1) & mask_1d.view(batch, 1, seqlen) & attention_mask
        )
        position = ops.arange(seqlen).expand(batch, seqlen)

        hidden_states = self.input_embedding(input_ids, input_id_sub)
        position_bias = self.position_bias(position, position, segment_bucket)
        hidden_states, present_key_values, all_hidden_states, all_attentions = self.encoder(
            hidden_states,
            attention_mask,
            position_bias,
            output_attentions,
            output_hidden_states,
            past_key_values=None,
            use_cache=False
        )

        if not return_dict:
            return tuple(
                v for v in [hidden_states, present_key_values, all_hidden_states, all_attentions] if v is not None
            )

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=present_key_values,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
        )

    def inference(
        self,
        input_ids: mindspore.Tensor,
        input_id_sub: Optional[mindspore.Tensor] = None,
        position: Optional[mindspore.Tensor] = None,
        context: Optional[mindspore.Tensor] = None,
        sample_ids: Optional[mindspore.Tensor] = None,
        num_segments: Optional[mindspore.Tensor] = None,
        segment: Optional[mindspore.Tensor] = None,
        segment_rel_offset: Optional[mindspore.Tensor] = None,
        segment_rel: Optional[mindspore.Tensor] = None,
        past_states: Optional[Dict] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[List] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        # dummy setting for common tests
        if input_id_sub is None:
            dtype = input_ids.dtype
            batch, seq_length = input_ids.shape
            segment = ops.where(input_ids != 0, 2, 0).to(dtype=dtype)
            context = ops.full((batch, seq_length), 1, dtype=dtype)
            position = ops.arange(seq_length, dtype=dtype).repeat(batch, 1)
            input_id_sub = ops.full((batch, seq_length), 0, dtype=dtype)
            segment_rel_offset = ops.full((batch, seq_length), 0, dtype=dtype)
            segment_rel = ops.full((batch, seq_length), 0, dtype=dtype)
            num_segments = ops.full((batch, seq_length), 0, dtype=dtype)
            sample_ids = ops.zeros_like(input_ids)

        if past_states is None:
            present_position = position
            present_context = context
            present_sample_ids = sample_ids
            present_num_segments = num_segments
            present_segments = segment
            present_buffer = None
        else:
            present_position = ops.cat([past_states["buffer_position"], position], axis=-1)
            present_context = ops.cat([past_states["buffer_context"], context.astype(mindspore.int64)], axis=-1)
            present_sample_ids = ops.cat([past_states["buffer_sample_ids"], sample_ids], axis=-1)
            present_num_segments = ops.cat([past_states["buffer_num_segments"], num_segments], axis=-1)
            present_segments = ops.cat([past_states["buffer_segments"], segment], axis=-1)
            present_buffer = past_states["buffer"]

        batch = input_ids.shape[0]
        len_q = input_ids.shape[1]
        len_buffer = present_position.shape[1]

        segment_rel_2d = ops.masked_fill(
            segment[:, :, None] * num_segments[:, :, None]
            + present_segments[:, None, :]
            + segment_rel_offset[:, :, None],
            ~((sample_ids[:, :, None] == present_sample_ids[:, None, :])),  # not in the same sample
            0,  # avoid torch.gather overflow
        ).view(batch, len_q * len_buffer)

        segment_bucket = ops.gather_elements(
            input=segment_rel,
            dim=1,
            index=segment_rel_2d.long(),
        ).view(batch, len_q, len_buffer)

        segment_bucket = segment_bucket.masked_fill(
            ~((sample_ids[:, :, None] == present_sample_ids[:, None, :])),  # not in the same span or sample
            1,  # bucket is used for in-context samples
        )

        # directional mask
        directional_mask_2d = present_position[:, None, :] <= position[:, :, None]
        # sample mask
        sample_mask_2d = (sample_ids[:, :, None] == 0) | (sample_ids[:, :, None] == present_sample_ids[:, None, :])
        # context mask
        attention_mask = present_context[:, None, :] | (
            context[:, :, None].logical_not() & directional_mask_2d.view(batch, len_q, len_buffer)
        )
        # span mask
        attention_mask = attention_mask & sample_mask_2d
        # length mask
        mask_1d = present_num_segments != 0
        attention_mask = mask_1d.view(batch, 1, len_buffer) & attention_mask

        hidden_states = self.input_embedding(input_ids, input_id_sub)
        position_bias = self.position_bias(position, present_position, segment_bucket)
        hidden_states, present_key_values, all_hidden_states, all_attentions = self.encoder(
            hidden_states,
            attention_mask,
            position_bias,
            output_attentions,
            output_hidden_states,
            present_buffer,
            use_cache,
        )

        if not return_dict:
            return tuple(
                v for v in [hidden_states, present_key_values, all_hidden_states, all_attentions] if v is not None
            )

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=present_key_values,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
        )


class CpmBeeBeamHypotheses(BeamHypotheses):
    def add(self, hyp: List, sum_logprobs: float, beam_indices: Optional[mindspore.Tensor] = None):
        """
        Add a new hypothesis to the list.
        """
        score = sum_logprobs / (len(hyp) ** self.length_penalty)
        if len(self) < self.num_beams or score > self.worst_score:
            self.beams.append((score, hyp, beam_indices))
            if len(self) > self.num_beams:
                sorted_next_scores = sorted([(s, idx) for idx, (s, _, _) in enumerate(self.beams)])
                del self.beams[sorted_next_scores[0][1]]
                self.worst_score = sorted_next_scores[1][0]
            else:
                self.worst_score = min(score, self.worst_score)


class CpmBeeBeamSearchScorer(BeamSearchScorer):
    """
    Override BeamSearchScorer for CPMBee to support:
    1. Replace beam_tokens by beam_states, containing `idx`, `ans`, `nx_token_id`...
    2. The `process` will update the beam_states
    3. The `finalize` will just return the best hypotheses as a list.
    """

    def __init__(
        self,
        batch_size: int,
        num_beams: int,
        length_penalty: Optional[float] = 1.0,
        do_early_stopping: Optional[Union[bool, str]] = False,
        num_beam_hyps_to_keep: Optional[int] = 1,
        num_beam_groups: Optional[int] = 1,
        max_length: Optional[int] = None,
        **model_kwargs,
    ):
        super().__init__(batch_size, num_beams, length_penalty, do_early_stopping, num_beam_hyps_to_keep, num_beam_groups, max_length)
        self.num_beams = num_beams
        self.length_penalty = length_penalty
        self.do_early_stopping = do_early_stopping
        self.num_beam_hyps_to_keep = num_beam_hyps_to_keep
        self.num_beam_groups = num_beam_groups
        self.group_size = self.num_beams // self.num_beam_groups

        self._is_init = False
        self._beam_hyps = [
            CpmBeeBeamHypotheses(
                num_beams=self.num_beams,
                length_penalty=self.length_penalty,
                early_stopping=self.do_early_stopping,
                max_length=max_length,
            )
            for _ in range(batch_size)
        ]
        self._done = mindspore.tensor([False for _ in range(batch_size)], dtype=mindspore.bool_)

        self.beam_states = []
        for sent_id in range(batch_size):
            instance_beam_states = []

            for _ in range(self.num_beams):
                instance_beam_states.append(
                    {
                        "idx": 0,
                        "ans": [],
                        "nx_token_id": 6,
                        "nx_token_sub": 0,
                        "nx_segment_id": model_kwargs["other_info"][sent_id]["predict_segments"][0][0],
                        "nx_position": 0,
                    }
                )
            self.beam_states.append(instance_beam_states)

    def process(
        self,
        batch_size: int,
        cur_len: int,
        _next_scores: mindspore.Tensor,
        next_scores: mindspore.Tensor,
        next_tokens: mindspore.Tensor,
        vocab_size: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        bos_token_id: Optional[int] = None,
        eos_token_id: Optional[Union[int, List[int]]] = None,
        max_length: Optional[int] = None,
        ext_table_sub_cpu: Optional[mindspore.Tensor] = None,
        ext_table_ids_cpu: Optional[mindspore.Tensor] = None,
        **model_kwargs,
    ) -> Tuple[mindspore.Tensor]:
        next_beam_state = []
        for sent_id in range(batch_size):
            self._done[sent_id] = self._done[sent_id] or self._beam_hyps[sent_id].is_done(
                next_scores[sent_id].max().item(), cur_len
            )
            if self._done[sent_id]:
                next_beam_state.append(
                    [
                        (
                            {
                                "idx": 0,
                                "ans": [],
                                "nx_token_id": pad_token_id,
                                "nx_token_sub": 0,
                                "nx_segment_id": 0,
                                "nx_position": 0,
                            },
                            0,
                            0,
                        )
                    ]
                    * self.num_beams
                )
                continue

            next_instance_beam_states = []

            for idx, value in zip(next_tokens[sent_id], next_scores[sent_id]):
                beam_id = ops.div(idx, _next_scores.shape[-1], rounding_mode="floor").item()
                word_id = (idx % _next_scores.shape[-1]).item()

                curr_info = self.beam_states[sent_id][beam_id]
                if (
                    word_id == eos_token_id
                    and (curr_info["idx"] + 1 == len(model_kwargs["other_info"][sent_id]["predict_segments"]))
                ) or cur_len == max_length:
                    self._beam_hyps[sent_id].add(
                        self.beam_states[sent_id][beam_id]["ans"]
                        + [
                            (
                                word_id,
                                model_kwargs["other_info"][sent_id]["predict_segments"][curr_info["idx"]][1],
                            )
                        ],
                        value.item(),
                    )
                elif word_id == eos_token_id:
                    next_instance_beam_states.append(
                        (
                            {
                                "idx": curr_info["idx"] + 1,
                                "ans": curr_info["ans"]
                                + [
                                    (
                                        word_id,
                                        model_kwargs["other_info"][sent_id]["predict_segments"][curr_info["idx"]][1],
                                    )
                                ],
                                "nx_token_id": bos_token_id,
                                "nx_token_sub": 0,
                                "nx_segment_id": model_kwargs["other_info"][sent_id]["predict_segments"][
                                    curr_info["idx"] + 1
                                ][0],
                                "nx_position": 0,
                            },
                            value.item(),
                            sent_id * self.num_beams + beam_id,
                        )
                    )

                else:
                    raw_word_id = word_id
                    word_id_sub = 0
                    if word_id >= vocab_size:
                        word_id -= vocab_size
                        word_id_sub = int(ext_table_sub_cpu[word_id].item())
                        word_id = int(ext_table_ids_cpu[word_id].item())

                    next_instance_beam_states.append(
                        (
                            {
                                "idx": curr_info["idx"],
                                "ans": curr_info["ans"]
                                + [
                                    (
                                        raw_word_id,
                                        model_kwargs["other_info"][sent_id]["predict_segments"][curr_info["idx"]][1],
                                    )
                                ],
                                "nx_token_id": word_id,
                                "nx_token_sub": word_id_sub,
                                "nx_segment_id": curr_info["nx_segment_id"],
                                "nx_position": curr_info["nx_position"] + 1,
                            },
                            value.item(),
                            sent_id * self.num_beams + beam_id,
                        )
                    )

                if len(next_instance_beam_states) == self.num_beams:
                    break
            assert len(next_instance_beam_states) == 0 if cur_len == max_length else self.num_beams
            next_beam_state.append(next_instance_beam_states)

        if cur_len == max_length:
            return None

        beam_reorder_idx = []
        beam_new_scores = []
        beam_states = []
        for sent_id in range(batch_size):
            instance_beam_states = []
            for beam_id in range(self.num_beams):
                state, value, beam_idx = next_beam_state[sent_id][beam_id]
                beam_reorder_idx.append(beam_idx)
                beam_new_scores.append(value)
                instance_beam_states.append(state)
            beam_states.append(instance_beam_states)
        self.beam_states = beam_states

        return UserDict(
            {
                "next_beam_scores": mindspore.tensor(beam_new_scores).view(-1),
                "next_beam_states": beam_states,
                "next_beam_indices": mindspore.tensor(beam_reorder_idx, dtype=mindspore.int32).view(-1),
            }
        )

    def finalize(self) -> Tuple[mindspore.Tensor]:
        results = []
        for _, hypotheses in enumerate(self._beam_hyps):
            best_hyp = max(hypotheses.beams, key=lambda x: x[0])[1]
            results.append(best_hyp)
        return results

    @staticmethod
    def apply_repetition_penalty(
        logits,
        batch_size,
        num_beams,
        prev_output_tokens,
        repetition_penalty,
        start_idx=None,
        end_idx=None,
        window_size=None,
    ):
        # only conduct repetition penalty for the output
        assert repetition_penalty >= 1, "repetition penalty coefficient should >= 1"
        # repetition penalty (from CTRL paper https://arxiv.org/abs/1909.05858)
        for i in range(batch_size * num_beams):
            if start_idx is None or end_idx is None:
                output_tokens = prev_output_tokens[i].tolist()
            else:
                if end_idx >= start_idx:
                    if window_size:
                        output_tokens = prev_output_tokens[i][
                            max(start_idx, end_idx + 1 - window_size) : end_idx + 1
                        ].tolist()
                    else:
                        output_tokens = prev_output_tokens[i][start_idx : end_idx + 1].tolist()
                else:
                    output_tokens = []
            for previous_token in set(output_tokens):
                # if score < 0 then repetition penalty has to
                # multiplied to reduce the previous token probability
                if logits[i, previous_token] < 0:
                    logits[i, previous_token] *= repetition_penalty
                else:
                    logits[i, previous_token] /= repetition_penalty


class CpmBeeForCausalLM(CpmBeePreTrainedModel):
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config: CpmBeeConfig):
        super().__init__(config)
        self.cpmbee = CpmBeeModel(config)

        # lm_head.weight is tied to cpmbee.input_embedding.weight
        self.lm_head = nn.Dense(config.hidden_size, config.vocab_size, has_bias=False)
        self.post_init()

    def construct(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        input_id_sub: Optional[mindspore.Tensor] = None,
        length: Optional[mindspore.Tensor] = None,
        context: Optional[mindspore.Tensor] = None,
        sample_ids: Optional[mindspore.Tensor] = None,
        num_segments: Optional[mindspore.Tensor] = None,
        segment: Optional[mindspore.Tensor] = None,
        segment_rel_offset: Optional[mindspore.Tensor] = None,
        segment_rel: Optional[mindspore.Tensor] = None,
        span: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[List] = None,
        use_cache: Optional[bool] = None,
        labels: Optional[mindspore.Tensor] = None,
        return_dict: Optional[bool] = None,
        ext_table_ids: Optional[mindspore.Tensor] = None,  # (ext_table_size) int32
        ext_table_sub: Optional[mindspore.Tensor] = None,  # (ext_table_size) int32
        **kwargs,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            input_ids (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Indices of input sequence tokens in the vocabulary.

                Indices can be obtained using [`CPMBeeTokenizer`]. See [`PreTrainedTokenizer.encode`] and
                [`PreTrainedTokenizer.__call__`] for details.

                [What are input IDs?](../glossary#input-ids)
            input_id_sub (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Subscription of input sequence tokens in the vocabulary.

                Subscription of normal text will be zero while the special tokens of each group will be the 0, 1, 2,
                ... <ans_0>, <ans_1>, <ans_2> ... belongs to group <ans>. <mask_0>, <mask_1>, <mask_2> ... belongs to
                group <mask>.
            length (`mindspore.Tensor` of shape `(batch_size)`):
                The length of sequences in batch.
            context (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Whether this token id is context or not. If is context, the value is 1. If not, the value is 0. If a
                token id is context, it does not need to be predicted.
            sample_ids (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Give a sample id to every token id. The token ids with same sample ids belongs to the same sample.
            num_segments (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Total number of segments in the current input.
            segment (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Give a segment id to every token id. The token ids with same segment ids belongs to the same sample.

                Generally, a string key or value in input data will be a segment. For example, input {"input": "hello,
                ", "<ans>": ""}, the segments includes: "input", "hello, ", "<ans>" and "".
            segment_rel_offset (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                The offset of segment rel.
            segment_rel (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                The segment relevance. A relative implementation of measuring the importance of segments.
            span (`Dict[str, Union[mindspore.Tensor, List]]`):
                Span will record every input_ids shape.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers.
            past_key_values (`tuple(tuple(mindspore.Tensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
                A dummy arguments for CPMBee. The `past_states` contains pre-computed hidden-states (key and values in
                the self-attention blocks and in the cross-attention blocks) that can be used (see `past_key_values`
                input) and other history arguments to speed up sequential decoding.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            labels (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss.
            return_dict (`bool`, *optional*):
                Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
            ext_table_ids (`mindspore.Tensor`, *optional*):
                ext_table ids for embedding projection.
            ext_table_sub (`mindspore.Tensor`, *optional*):
                ext_table subscriptions for embedding projection.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        model_output = self.cpmbee(
            input_ids,
            input_id_sub,
            length,
            context,
            sample_ids,
            num_segments,
            segment,
            segment_rel_offset,
            segment_rel,
            span,
            output_attentions,
            output_hidden_states,
            past_key_values,
            use_cache,
            return_dict,
        )
        hidden_states = model_output.last_hidden_state if return_dict else model_output[0]

        if ext_table_ids is not None:
            ext_table = self.cpmbee.input_embedding(ext_table_ids, ext_table_sub)
        else:
            ext_table = None
        logits = self.cpmbee.input_embedding.projection(hidden_states, ext_table)

        loss = None
        if labels is not None:
            loss = ops.cross_entropy(logits.view(-1, logits.shape[-1]), labels.long().view(-1))

        if not return_dict:
            output = (logits,) + model_output[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=model_output.past_key_values,
            hidden_states=model_output.hidden_states,
            attentions=model_output.attentions,
        )

    def inference(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        input_id_sub: Optional[mindspore.Tensor] = None,
        position: Optional[mindspore.Tensor] = None,
        context: Optional[mindspore.Tensor] = None,
        sample_ids: Optional[mindspore.Tensor] = None,
        num_segments: Optional[mindspore.Tensor] = None,
        segment: Optional[mindspore.Tensor] = None,
        segment_rel_offset: Optional[mindspore.Tensor] = None,
        segment_rel: Optional[mindspore.Tensor] = None,
        past_states: Optional[Dict] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[List] = None,
        use_cache: Optional[bool] = None,
        labels: Optional[mindspore.Tensor] = None,
        return_dict: Optional[bool] = None,
        ext_table_ids: Optional[mindspore.Tensor] = None,  # (ext_table_size) int32
        ext_table_sub: Optional[mindspore.Tensor] = None,  # (ext_table_size) int32
        **kwargs,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            input_ids (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Indices of input sequence tokens in the vocabulary.

                Indices can be obtained using [`CPMBeeTokenizer`]. See [`PreTrainedTokenizer.encode`] and
                [`PreTrainedTokenizer.__call__`] for details.

                [What are input IDs?](../glossary#input-ids)
            input_id_sub (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Subscription of input sequence tokens in the vocabulary.

                Subscription of normal text will be zero while the special tokens of each group will be the 0, 1, 2,
                ... <ans_0>, <ans_1>, <ans_2> ... belongs to group <ans>. <mask_0>, <mask_1>, <mask_2> ... belongs to
                group <mask>.
            position (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                The position of input sequence tokens in the vocabulary for each segment. if segment1 is 0, 1, 2 and
                segment2 is 0, 1, 2, 3, the position will be 0, 1, 2, 0, 1, 2, 3
            context (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Whether this token id is context or not. If is context, the value is 1. If not, the value is 0. If a
                token id is context, it does not need to be predicted.
            sample_ids (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Give a sample id to every token id. The token ids with same sample ids belongs to the same sample.
            num_segments (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Total number of segments in the current input.
            segment (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                Give a segment id to every token id. The token ids with same segment ids belongs to the same sample.

                Generally, a string key or value in input data will be a segment. For example, input {"input": "hello,
                ", "<ans>": ""}, the segments includes: "input", "hello, ", "<ans>" and "".
            segment_rel_offset (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                The offset of segment rel.
            segment_rel (`mindspore.Tensor` of shape `(batch_size, seq_len)`):
                The segment relevance. A relative implementation of measuring the importance of segments.
            past_states (`Dict[str, Union[mindspore.Tensor, List]]`):
                Store the history information including position, context, sample_ids, num_segments, segment and
                past_key_values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers.
            past_key_values (`tuple(tuple(mindspore.Tensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
                A dummy arguments for CPMBee. The `past_states` contains pre-computed hidden-states (key and values in
                the self-attention blocks and in the cross-attention blocks) that can be used (see `past_key_values`
                input) and other history arguments to speed up sequential decoding.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            labels (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss.
            return_dict (`bool`, *optional*):
                Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
            ext_table_ids (`mindspore.Tensor`, *optional*):
                ext_table ids for embedding projection.
            ext_table_sub (`mindspore.Tensor`, *optional*):
                ext_table subscriptions for embedding projection.

        Example:

        Text Generation with CpmBeeForCausalLM.
        ```python
        >>> from transformers import CpmBeeTokenizer, CpmBeeForCausalLM

        >>> texts = {"input": "今天天气不错，", "<ans>": ""}
        >>> model = CpmBeeForCausalLM.from_pretrained("openbmb/cpm-bee-10b")
        >>> tokenizer = CPMBeeTokenizer.from_pretrained("openbmb/cpm-bee-10b")
        >>> output_texts = model.generate({"input": "今天天气不错，", "<ans>": ""}, tokenizer)
        >>> print(output_texts)
        {'input': '今天天气不错，', '<ans>': '适合睡觉。'}
        ```
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        model_output = self.cpmbee.inference(
            input_ids,
            input_id_sub,
            position,
            context,
            sample_ids,
            num_segments,
            segment,
            segment_rel_offset,
            segment_rel,
            past_states,
            output_attentions,
            output_hidden_states,
            past_key_values,
            use_cache,
            return_dict,
        )
        hidden_states = model_output.last_hidden_state if return_dict else model_output[0]

        if ext_table_ids is not None and 0 not in ext_table_ids.shape:
            ext_table = self.cpmbee.input_embedding(ext_table_ids, ext_table_sub)
        else:
            ext_table = None
        logits = self.cpmbee.input_embedding.projection(hidden_states, ext_table)

        loss = None
        if labels is not None:
            loss = ops.cross_entropy(logits.view(-1, logits.shape[-1]), labels.view(-1))

        if not return_dict:
            output = (logits,) + model_output[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=model_output.past_key_values,
            hidden_states=model_output.hidden_states,
            attentions=model_output.attentions,
        )

    def get_input_embeddings(self):
        return self.cpmbee.input_embedding

    def set_input_embeddings(self, embeddings):
        self.cpmbee.input_embedding = embeddings

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def prepare_inputs_for_generation(
        self,
        input_ids: mindspore.Tensor,
        batch_size: int,
        beam_scorer: CpmBeeBeamSearchScorer = None,
        input_id_subs: Optional[mindspore.Tensor] = None,
        input_pos: Optional[mindspore.Tensor] = None,
        segment_ids: Optional[mindspore.Tensor] = None,
        batch_ext_table_ids: Optional[mindspore.Tensor] = None,
        batch_ext_table_sub: Optional[mindspore.Tensor] = None,
        other_info: Optional[Dict] = None,
        **model_kwargs,
    ):
        """
        Choose the current input according to beam states.
        """
        # init preparation
        context = model_kwargs.get("context")
        sample_ids = model_kwargs.get("sample_ids")
        segment_rel_offset = model_kwargs.get("segment_rel_offset")
        num_segments = model_kwargs.get("num_segments")
        segment_rel = model_kwargs.get("segment_rel")
        past_states = model_kwargs.get("past_states", None)
        past_key_values = model_kwargs.get("past_key_values", None)
        _input_ids = input_ids

        # update input in generation
        if beam_scorer is not None:
            tmp_input = []
            tmp_input_sub = []
            tmp_position = []
            tmp_segment = []
            for sent_id in range(batch_size):
                for beam_id in range(beam_scorer.num_beams):
                    tmp_input.append(beam_scorer.beam_states[sent_id][beam_id]["nx_token_id"])
                    tmp_input_sub.append(beam_scorer.beam_states[sent_id][beam_id]["nx_token_sub"])
                    tmp_position.append(beam_scorer.beam_states[sent_id][beam_id]["nx_position"])
                    tmp_segment.append(beam_scorer.beam_states[sent_id][beam_id]["nx_segment_id"])

            model_kwargs["input_id_subs"] = input_id_subs = mindspore.tensor(
                tmp_input_sub, dtype=mindspore.int64
            ).view(batch_size * beam_scorer.num_beams, 1)
            model_kwargs["input_pos"] = input_pos = mindspore.tensor(
                tmp_position, dtype=mindspore.int64
            ).view(batch_size * beam_scorer.num_beams, 1)
            model_kwargs["segment_ids"] = segment_ids = mindspore.tensor(
                tmp_segment, dtype=mindspore.int64
            ).view(batch_size * beam_scorer.num_beams, 1)
            input_ids = ops.cat(
                [
                    input_ids,
                    mindspore.tensor(tmp_input, dtype=mindspore.int64).view(
                        batch_size * beam_scorer.num_beams, 1
                    ),
                ],
                axis=-1,
            )
            _input_ids = input_ids[:, -1:]

        return {
            "input_ids": _input_ids,
            "input_id_sub": input_id_subs,
            "position": input_pos,
            "context": context,
            "sample_ids": sample_ids,
            "segment_rel_offset": segment_rel_offset,
            "segment": segment_ids,
            "num_segments": num_segments,
            "segment_rel": segment_rel,
            "use_cache": True,
            "past_key_values": past_key_values,
            "ext_table_ids": batch_ext_table_ids,
            "ext_table_sub": batch_ext_table_sub,
            "past_states": past_states,
        }, input_ids

    def _update_model_kwargs_for_generation(
        self,
        outputs: ModelOutput,
        model_inputs=None,
        **model_kwargs,
    ) -> Dict[str, Any]:
        """
        Concatenate the history input and current input.
        """

        old_past_states = model_kwargs["past_states"]
        model_kwargs["past_states"] = {
            "buffer_position": ops.cat([old_past_states["buffer_position"], model_inputs["position"]], axis=-1),
            "buffer_context": ops.cat([old_past_states["buffer_context"], model_inputs["context"].astype(mindspore.int64)], axis=-1),
            "buffer_sample_ids": ops.cat([old_past_states["buffer_sample_ids"], model_inputs["sample_ids"]], axis=-1),
            "buffer_num_segments": ops.cat(
                [old_past_states["buffer_num_segments"], model_inputs["num_segments"]], axis=-1
            ),
            "buffer_segments": ops.cat([old_past_states["buffer_segments"], model_inputs["segment"]], axis=-1),
            "buffer": outputs.past_key_values,
        }

        return model_kwargs

    def _reorder_cache(self, past_key_values: Dict, beam_idx: mindspore.Tensor):
        beam_idx = beam_idx.tolist()
        for kw in past_key_values.keys():
            if kw == "buffer":
                buf_list = past_key_values[kw]
                nw_buf_list = []
                for buf in buf_list:
                    if buf == (None, None):
                        nw_buf_list.append((None, None))
                    else:
                        k_buf, v_buf = buf
                        nw_buf_list.append((k_buf[beam_idx, :], v_buf[beam_idx, :]))
                past_key_values[kw] = nw_buf_list
            else:
                past_key_values[kw] = past_key_values[kw][beam_idx, :]

        return past_key_values

    @staticmethod
    def _expand_inputs_for_generation(
        expand_size: int = 1,
        is_encoder_decoder: bool = False,
        input_ids: Optional[mindspore.Tensor] = None,
        **model_kwargs,
    ) -> Tuple[mindspore.Tensor, Dict[str, Any]]:
        """Expands tensors from [batch_size, ...] to [batch_size * expand_size, ...]"""

        # do not expand ext_table_ids and ext_table_sub
        def _expand_dict_for_generation(dict_to_expand):
            for key in dict_to_expand:
                if (
                    dict_to_expand[key] is not None
                    and isinstance(dict_to_expand[key], mindspore.Tensor)
                    and "ext_table" not in key
                ):
                    dict_to_expand[key] = dict_to_expand[key].repeat_interleave(expand_size, dim=0)
            return dict_to_expand

        if input_ids is not None:
            input_ids = input_ids.repeat_interleave(expand_size, dim=0)

        model_kwargs = _expand_dict_for_generation(model_kwargs)

        if is_encoder_decoder:
            if model_kwargs.get("encoder_outputs") is None:
                raise ValueError("If `is_encoder_decoder` is True, make sure that `encoder_outputs` is defined.")
            model_kwargs["encoder_outputs"] = _expand_dict_for_generation(model_kwargs["encoder_outputs"])

        return input_ids, model_kwargs

    def adjust_logits_during_generation(
        self,
        logits: mindspore.Tensor,
        batch_size: int,
        beam_size: int,
        vocab_size: int,
        ext_table_ids: mindspore.Tensor,
        **model_kwargs,
    ) -> mindspore.Tensor:
        """
        Implement in subclasses of [`PreTrainedModel`] for custom behavior to adjust the logits in the generate method.
        """
        for sent_id in range(batch_size):
            if 1 not in model_kwargs["other_info"][sent_id]["ext_table"]:
                # unk is not allowed, mask unk
                logits[sent_id * beam_size : (sent_id + 1) * beam_size, 1] = -10000
            ext_ids = set()
            for v in model_kwargs["other_info"][sent_id]["ext_table"].keys():
                ext_ids.add(v)
            for ext_id in range(vocab_size, vocab_size + ext_table_ids.shape[0]):
                if ext_id not in ext_ids:
                    logits[sent_id * beam_size : (sent_id + 1) * beam_size, ext_id] = -10000
        return logits

    def beam_search(
        self,
        input_ids: mindspore.Tensor,
        beam_scorer: CpmBeeBeamSearchScorer,
        repetition_penalty: Optional[float] = 1.0,
        logits_processor: Optional[LogitsProcessorList] = None,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[Union[int, List[int]]] = None,
        bos_token_id: Optional[Union[int, List[int]]] = None,
        vocab_size: Optional[int] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_scores: Optional[bool] = None,
        return_dict_in_generate: Optional[bool] = None,
        synced_gpus: bool = False,
        **model_kwargs,
    ) -> List:
        """
        Override the beam_search for CPMBee.
        """
        # init values
        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        pad_token_id = pad_token_id if pad_token_id is not None else self.generation_config.pad_token_id
        eos_token_id = eos_token_id if eos_token_id is not None else self.generation_config.eos_token_id
        bos_token_id = bos_token_id if bos_token_id is not None else self.generation_config.bos_token_id
        vocab_size = vocab_size if vocab_size is not None else self.generation_config.vocab_size
        max_length = max_length if max_length is not None else self.generation_config.max_new_tokens
        output_scores = output_scores if output_scores is not None else self.generation_config.output_scores
        output_attentions = (
            output_attentions if output_attentions is not None else self.generation_config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.generation_config.output_hidden_states
        )
        return_dict_in_generate = (
            return_dict_in_generate
            if return_dict_in_generate is not None
            else self.generation_config.return_dict_in_generate
        )

        batch_size = len(beam_scorer._beam_hyps)
        num_beams = beam_scorer.num_beams

        batch_beam_size, cur_len = input_ids.shape

        if num_beams * batch_size != batch_beam_size:
            raise ValueError(
                f"Batch dimension of `input_ids` should be {num_beams * batch_size}, but is {batch_beam_size}."
            )

        # init attention / hidden states / scores tuples
        scores = () if (return_dict_in_generate and output_scores) else None
        beam_indices = (
            tuple(() for _ in range(batch_beam_size)) if (return_dict_in_generate and output_scores) else None
        )
        decoder_attentions = () if (return_dict_in_generate and output_attentions) else None
        cross_attentions = () if (return_dict_in_generate and output_attentions) else None
        decoder_hidden_states = () if (return_dict_in_generate and output_hidden_states) else None

        # initialise score of first beam with 0 and the rest with -1e9. This makes sure that only tokens
        # of the first beam are considered to avoid sampling the exact same tokens across all beams.
        beam_scores = ops.zeros((batch_size, num_beams), dtype=mindspore.float32)
        beam_scores[:, 1:] = -1e9
        beam_scores = beam_scores.view((batch_size * num_beams,))

        # init inference
        model_inputs, input_ids = self.prepare_inputs_for_generation(input_ids, batch_size, **model_kwargs)
        pred_start_index = input_ids.shape[-1]
        outputs = self.inference(
            **model_inputs,
            return_dict=True,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )

        # update model_kwargs
        model_kwargs["past_states"] = {
            "buffer_position": model_inputs["position"],
            "buffer_context": model_inputs["context"],
            "buffer_sample_ids": model_inputs["sample_ids"],
            "buffer_num_segments": model_inputs["num_segments"],
            "buffer_segments": model_inputs["segment"],
            "buffer": outputs.past_key_values,
        }
        model_kwargs["context"] = ops.ones(batch_beam_size, dtype=mindspore.bool_).view(
            batch_beam_size, 1
        )
        model_kwargs["sample_ids"] = ops.zeros(batch_beam_size, dtype=mindspore.int64).view(
            batch_beam_size, 1
        )
        model_kwargs["num_segments"] = model_kwargs["num_segments"][:, -1:]
        model_kwargs["segment_rel_offset"] = model_kwargs["segment_rel_offset"][:, -1:]
        model_kwargs["past_key_values"] = outputs.past_key_values

        ext_table_ids_cpu = model_inputs["ext_table_ids"]
        ext_table_sub_cpu = model_inputs["ext_table_sub"]

        cur_len = 0
        while True:
            model_inputs, input_ids = self.prepare_inputs_for_generation(
                input_ids, batch_size, beam_scorer, **model_kwargs
            )

            outputs = self.inference(
                **model_inputs,
                return_dict=True,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
            )

            next_token_logits = outputs.logits[:, -1, :]

            if all(beam_scorer._done):
                break
            # hack: adjust tokens for Marian. For Marian we have to make sure that the `pad_token_id`
            # cannot be generated both before and after the `ops.log_softmax` operation.
            next_token_logits = self.adjust_logits_during_generation(
                next_token_logits, batch_size, num_beams, vocab_size, ext_table_ids_cpu, **model_kwargs
            )

            # repetition_penalty
            beam_scorer.apply_repetition_penalty(
                next_token_logits,
                batch_size,
                num_beams,
                input_ids,
                repetition_penalty,
                pred_start_index,
                input_ids.shape[-1] - 1,
                None,
            )

            _next_token_scores = ops.log_softmax(
                next_token_logits, axis=-1
            )  # (batch_size * num_beams, vocab_size)

            next_token_scores_processed = logits_processor(input_ids, _next_token_scores)
            # next_token_scores_processed = _next_token_scores
            next_token_scores = next_token_scores_processed + beam_scores[:, None].expand_as(_next_token_scores)

            # Store scores, attentions and hidden_states when required
            if return_dict_in_generate:
                if output_scores:
                    scores += (next_token_scores_processed,)
                if output_attentions:
                    decoder_attentions += (
                        (outputs.decoder_attentions,) if self.config.is_encoder_decoder else (outputs.attentions,)
                    )
                    if self.config.is_encoder_decoder:
                        cross_attentions += (outputs.cross_attentions,)

                if output_hidden_states:
                    decoder_hidden_states += (
                        (outputs.decoder_hidden_states,)
                        if self.config.is_encoder_decoder
                        else (outputs.hidden_states,)
                    )

            # reshape for beam search
            next_token_scores = next_token_scores.view(batch_size, -1)

            # Sample 2 next tokens for each beam (so we have some spare tokens and match output of beam search)
            next_token_scores, next_tokens = ops.topk(
                next_token_scores, 2 * num_beams, dim=1, largest=True, sorted=True
            )

            beam_outputs = beam_scorer.process(
                batch_size,
                cur_len,
                _next_token_scores,
                next_token_scores,
                next_tokens,
                vocab_size=vocab_size,
                pad_token_id=pad_token_id,
                bos_token_id=bos_token_id,
                eos_token_id=eos_token_id,
                max_length=max_length,
                ext_table_ids_cpu=ext_table_ids_cpu,
                ext_table_sub_cpu=ext_table_sub_cpu,
                **model_kwargs,
            )
            if beam_outputs is None:
                break
            beam_idx = beam_outputs["next_beam_indices"]
            beam_scores = beam_outputs["next_beam_scores"]

            input_ids = input_ids[beam_idx.tolist(), :]
            model_kwargs = self._update_model_kwargs_for_generation(outputs, model_inputs, **model_kwargs)
            if model_kwargs["past_states"] is not None:
                model_kwargs["past_states"] = self._reorder_cache(model_kwargs["past_states"], beam_idx)

            if return_dict_in_generate and output_scores:
                beam_indices = tuple((beam_indices[beam_idx[i]] + (beam_idx[i],) for i in range(len(beam_indices))))

            cur_len += 1

            if beam_scorer.is_done or cur_len == max_length + 1:
                if not synced_gpus:
                    break

        sequence_outputs = beam_scorer.finalize()

        return sequence_outputs

    def _generate(
        self,
        inputs: Optional[mindspore.Tensor] = None,
        generation_config: Optional[GenerationConfig] = None,
        repetition_penalty: Optional[float] = 1.0,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        prefix_allowed_tokens_fn: Optional[Callable[[int, mindspore.Tensor], List[int]]] = None,
        synced_gpus: Optional[bool] = None,
        streamer: Optional["BaseStreamer"] = None,
        **kwargs,
    ) -> List:
        r"""
        The generation of CPMBee.
        1. It will use beam search as generation strategy.
        2. It will use CpmBeeBeamSearchScorer as the beamsearch scorer.
        """
        # 1. Handle `generation_config` and kwargs that might update it, and validate the `.generate()` call
        self._validate_model_class()

        # priority: `generation_config` argument > `model.generation_config` (the default generation config)
        if generation_config is None:
            # legacy: users may modify the model configuration to control generation -- update the generation config
            # model attribute accordingly, if it was created from the model config
            if self.generation_config._from_model_config:
                new_generation_config = GenerationConfig.from_model_config(self.config)
                if new_generation_config != self.generation_config:
                    warnings.warn(
                        "You have modified the pretrained model configuration to control generation. This is a"
                        " deprecated strategy to control generation and will be removed soon, in a future version."
                        " Please use a generation configuration file (see"
                        " https://huggingface.co/docs/transformers/main_classes/text_generation)"
                    )
                    self.generation_config = new_generation_config
            generation_config = self.generation_config

        generation_config = copy.deepcopy(generation_config)
        model_kwargs = generation_config.update(**kwargs)  # All unused kwargs must be model kwargs
        generation_config.validate()
        self._validate_model_kwargs(model_kwargs.copy())

        # 2. Set generation parameters if not already defined
        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        stopping_criteria = stopping_criteria if stopping_criteria is not None else StoppingCriteriaList()

        if generation_config.pad_token_id is None and generation_config.eos_token_id is not None:
            if model_kwargs.get("attention_mask", None) is None:
                logger.warning(
                    "The attention mask and the pad token id were not set. As a consequence, you may observe "
                    "unexpected behavior. Please pass your input's `attention_mask` to obtain reliable results."
                )
            eos_token_id = generation_config.eos_token_id
            if isinstance(eos_token_id, list):
                eos_token_id = eos_token_id[0]
            logger.warning(f"Setting `pad_token_id` to `eos_token_id`:{eos_token_id} for open-end generation.")
            generation_config.pad_token_id = eos_token_id

        # 3. Define model inputs
        # inputs_tensor has to be defined
        # model_input_name is defined if model-specific keyword input is passed
        # otherwise model_input_name is None
        # all model-specific keyword inputs are removed from `model_kwargs`
        inputs_tensor, model_input_name, model_kwargs = self._prepare_model_inputs(
            inputs, generation_config.bos_token_id, model_kwargs
        )
        batch_size = inputs_tensor.shape[0]

        # 4. Define other model kwargs
        model_kwargs["output_attentions"] = generation_config.output_attentions
        model_kwargs["output_hidden_states"] = generation_config.output_hidden_states
        model_kwargs["use_cache"] = generation_config.use_cache

        accepts_attention_mask = "attention_mask" in set(inspect.signature(self.construct).parameters.keys())
        requires_attention_mask = "encoder_outputs" not in model_kwargs

        if model_kwargs.get("attention_mask", None) is None and requires_attention_mask and accepts_attention_mask:
            model_kwargs["attention_mask"] = self._prepare_attention_mask_for_generation(
                inputs_tensor, generation_config.pad_token_id, generation_config.eos_token_id
            )

        # decoder-only models should use left-padding for generation
        if not self.config.is_encoder_decoder:
            # If `input_ids` was given, check if the last id in any sequence is `pad_token_id`
            # Note: If using, `inputs_embeds` this check does not work, because we want to be more hands-off.
            if (
                generation_config.pad_token_id is not None
                and len(inputs_tensor.shape) == 2
                and ops.sum(inputs_tensor[:, -1] == generation_config.pad_token_id) > 0
            ):
                logger.warning(
                    "A decoder-only architecture is being used, but right-padding was detected! For correct "
                    "generation results, please set `padding_side='left'` when initializing the tokenizer."
                )

        # 5. Prepare `input_ids` which will be used for auto-regressive generation
        input_ids = inputs_tensor if model_input_name == "input_ids" else model_kwargs.pop("input_ids")

        if streamer is not None:
            streamer.put(input_ids)

        # 6. Prepare `max_length` depending on other stopping criteria.
        input_ids_seq_length = input_ids.shape[-1]
        has_default_max_length = kwargs.get("max_length") is None and generation_config.max_length is not None
        if has_default_max_length and generation_config.max_new_tokens is None:
            warnings.warn(
                f"Using `max_length`'s default ({generation_config.max_length}) to control the generation length. "
                "This behaviour is deprecated and will be removed from the config in v5 of Transformers -- we"
                " recommend using `max_new_tokens` to control the maximum length of the generation.",
                UserWarning,
            )
        elif generation_config.max_new_tokens is not None:
            if not has_default_max_length:
                logger.warning(
                    f"Both `max_new_tokens` (={generation_config.max_new_tokens}) and `max_length`(="
                    f"{generation_config.max_length}) seem to have been set. `max_new_tokens` will take precedence. "
                    "Please refer to the documentation for more information. "
                    "(https://huggingface.co/docs/transformers/main/en/main_classes/text_generation)"
                )
            generation_config.max_length = generation_config.max_new_tokens + input_ids_seq_length

        if generation_config.min_length is not None and generation_config.min_length > generation_config.max_length:
            raise ValueError(
                f"Unfeasible length constraints: the minimum length ({generation_config.min_length}) is larger than"
                f" the maximum length ({generation_config.max_length})"
            )
        if input_ids_seq_length >= generation_config.max_length:
            input_ids_string = "decoder_input_ids" if self.config.is_encoder_decoder else "input_ids"
            logger.warning(
                f"Input length of {input_ids_string} is {input_ids_seq_length}, but `max_length` is set to"
                f" {generation_config.max_length}. This can lead to unexpected behavior. You should consider"
                " increasing `max_new_tokens`."
            )

        if streamer is not None and (generation_config.num_beams > 1):
            raise ValueError(
                "`streamer` cannot be used with beam search (yet!). Make sure that `num_beams` is set to 1."
            )

        # 7. prepare distribution pre_processing samplers
        logits_processor = self._get_logits_processor(
            generation_config=generation_config,
            input_ids_seq_length=input_ids_seq_length,
            encoder_input_ids=inputs_tensor,
            prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
            logits_processor=logits_processor,
        )

        # 8. prepare beam search scorer
        beam_scorer = CpmBeeBeamSearchScorer(
            batch_size=batch_size,
            num_beams=generation_config.num_beams,
            length_penalty=generation_config.length_penalty,
            do_early_stopping=generation_config.early_stopping,
            num_beam_hyps_to_keep=generation_config.num_return_sequences,
            max_length=generation_config.max_new_tokens,
            **kwargs,
        )
        # 9. interleave input_ids with `num_beams` additional sequences per batch
        input_ids, model_kwargs = self._expand_inputs_for_generation(
            input_ids=input_ids,
            expand_size=generation_config.num_beams,
            is_encoder_decoder=self.config.is_encoder_decoder,
            **model_kwargs,
        )
        # 10. run beam search
        return self.beam_search(
            input_ids,
            beam_scorer,
            repetition_penalty=repetition_penalty,
            logits_processor=logits_processor,
            max_length=generation_config.max_new_tokens,
            pad_token_id=generation_config.pad_token_id,
            eos_token_id=generation_config.eos_token_id,
            vocab_size=kwargs.get("vocab_size", None),
            output_scores=generation_config.output_scores,
            return_dict_in_generate=generation_config.return_dict_in_generate,
            synced_gpus=synced_gpus,
            **model_kwargs,
        )

    def generate(
        self,
        data_list: Union[Dict, List[Dict]],
        tokenizer: CpmBeeTokenizer,
        **kwargs,
    ):
        """
        Override the generate for CPMBee. It will accept dict or list(dict) as input and returns dict or list(dict)
        with `<ans>` filled.

        Parameters:
            data_list (`dict` or `list(dict)`):
                The sequence used as a prompt for the generation or as model inputs to the encoder. If dict, data_list
                will be wrapped as a list.
            tokenizer: (`CpmBeeTokenizer`):
                The tokenizer.
            generation_config (`~generation.GenerationConfig`, *optional*):
                The generation configuration to be used as base parametrization for the generation call. `**kwargs`
                passed to generate matching the attributes of `generation_config` will override them. If
                `generation_config` is not provided, the default will be used, which had the following loading
                priority: 1) from the `generation_config.json` model file, if it exists; 2) from the model
                configuration. Please note that unspecified parameters will inherit [`~generation.GenerationConfig`]'s
                default values, whose documentation should be checked to parameterize generation.
        """
        if isinstance(data_list, dict):
            data_list = [data_list]
        input_encoded = tokenizer(data_list, return_tensors="ms", padding=True)
        input_encoded.update(kwargs)
        input_encoded["vocab_size"] = tokenizer.vocab_size

        decode_res = self._generate(**input_encoded)

        for sent_id, result in enumerate(decode_res):
            ans_result_map: Dict[int, List[int]] = {}
            for raw_word_id, ans_id in result:
                if ans_id not in ans_result_map:
                    ans_result_map[ans_id] = []
                ans_result_map[ans_id].append(raw_word_id)

            answer_placeholders = input_encoded["other_info"][sent_id]["answer_placeholders"]
            ext_table = input_encoded["other_info"][sent_id]["ext_table"]
            data = data_list[sent_id]
            for ans_id, token_ids in ans_result_map.items():
                if token_ids[-1] == tokenizer.eos_token_id:
                    token_ids = token_ids[:-1]
                text = tokenizer.decode(token_ids, ext_table)
                path = answer_placeholders[ans_id - 1]

                if len(path) > 0:
                    p = data["<ans>"]
                    for part in path[:-1]:
                        p = p[part]
                    p[path[-1]] = text
                else:
                    data["<ans>"] = text
            for ans_id in range(len(answer_placeholders)): # pylint: disable=consider-using-enumerate
                if (ans_id + 1) not in ans_result_map:
                    path = answer_placeholders[ans_id]
                    p = data["<ans>"]
                    for part in path[:-1]:
                        p = p[part]
                    p[path[-1]] = None
        return data_list

__all__ = __all__ = [
    "CPMBEE_PRETRAINED_MODEL_ARCHIVE_LIST",
    "CpmBeeForCausalLM",
    "CpmBeeModel",
    "CpmBeePreTrainedModel",
]
