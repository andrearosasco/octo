# adapted from https://github.com/google-research/vision_transformer/blob/main/vit_jax/models_vit.py
from typing import Callable, Optional

import flax.linen as nn
import jax.numpy as jnp

from orca.utils.typing import Array, Dtype, PRNGKey, Shape


class AddPositionEmbs(nn.Module):
    """Adds learned positional embeddings to the inputs.

    Attributes:
      posemb_init: positional embedding initializer.
    """

    posemb_init: Callable[[PRNGKey, Shape, Dtype], Array]

    @nn.compact
    def __call__(self, inputs):
        """Applies the AddPositionEmbs module.

        Args:
          inputs: Inputs to the layer.

        Returns:
          Output tensor with shape `(bs, timesteps, in_dim)`.
        """
        # inputs.shape is (batch_size, seq_len, emb_dim).
        assert inputs.ndim == 3, (
            "Number of dimensions should be 3," " but it is: %d" % inputs.ndim
        )
        pos_emb_shape = (1, inputs.shape[1], inputs.shape[2])
        pe = self.param("pos_embedding", self.posemb_init, pos_emb_shape)
        return inputs + pe


class MlpBlock(nn.Module):
    """Transformer MLP / feed-forward block."""

    mlp_dim: int
    dtype: Dtype = jnp.float32
    out_dim: Optional[int] = None
    dropout_rate: float = 0.1
    kernel_init: Callable[
        [PRNGKey, Shape, Dtype], Array
    ] = nn.initializers.xavier_uniform()
    bias_init: Callable[[PRNGKey, Shape, Dtype], Array] = nn.initializers.normal(
        stddev=1e-6
    )

    @nn.compact
    def __call__(self, inputs, *, deterministic):
        """Applies Transformer MlpBlock module."""
        actual_out_dim = inputs.shape[-1] if self.out_dim is None else self.out_dim
        x = nn.Dense(
            features=self.mlp_dim,
            dtype=self.dtype,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
        )(inputs)
        x = nn.gelu(x)
        x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=deterministic)
        output = nn.Dense(
            features=actual_out_dim,
            dtype=self.dtype,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
        )(x)
        output = nn.Dropout(rate=self.dropout_rate)(output, deterministic=deterministic)
        return output


class MAPHead(nn.Module):
    """Multihead Attention Pooling.

    From https://github.com/google-research/big_vision/blob/main/big_vision/models/vit.py
    """

    mlp_dim: Optional[int] = None  # Defaults to 4x input dim
    num_heads: int = 8
    num_readouts: int = 1

    @nn.compact
    def __call__(self, x, train=True):
        *batch_dims, l, d = x.shape
        x = x.reshape(-1, l, d)
        batch_size = x.shape[0]

        probe = self.param(
            "probe",
            nn.initializers.xavier_uniform(),
            (1, self.num_readouts, d),
            x.dtype,
        )
        probe = jnp.tile(probe, [batch_size, 1, 1])
        out = nn.MultiHeadDotProductAttention(
            num_heads=self.num_heads, kernel_init=nn.initializers.xavier_uniform()
        )(probe, x)

        # TODO: dropout on head?
        y = nn.LayerNorm()(out)

        out = out + MlpBlock(mlp_dim=nn.merge_param("mlp_dim", self.mlp_dim, 4 * d))(
            y, deterministic=not train
        )
        out = out.reshape(*batch_dims, self.num_readouts, d)
        return out


class Encoder1DBlock(nn.Module):
    """Transformer encoder layer.

    Attributes:
      inputs: input data.
      mlp_dim: dimension of the mlp on top of attention block.
      dtype: the dtype of the computation (default: float32).
      dropout_rate: dropout rate.
      attention_dropout_rate: dropout for attention heads.
      deterministic: bool, deterministic or not (to apply dropout).
      num_heads: Number of heads in nn.MultiHeadDotProductAttention
    """

    mlp_dim: int
    num_heads: int
    dtype: Dtype = jnp.float32
    dropout_rate: float = 0.1
    attention_dropout_rate: float = 0.1

    @nn.compact
    def __call__(self, inputs, attention_mask, *, deterministic):
        """Applies Encoder1DBlock module.

        Args:
          inputs: Inputs to the layer.
          deterministic: Dropout will not be applied when set to true.

        Returns:
          output after transformer encoder block.
        """

        # Attention block.
        assert inputs.ndim == 3, f"Expected (batch, seq, hidden) got {inputs.shape}"
        x = nn.LayerNorm(dtype=self.dtype)(inputs)
        x = nn.MultiHeadDotProductAttention(
            dtype=self.dtype,
            kernel_init=nn.initializers.xavier_uniform(),
            broadcast_dropout=False,
            deterministic=deterministic,
            dropout_rate=self.attention_dropout_rate,
            num_heads=self.num_heads,
        )(x, x, mask=attention_mask)
        x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=deterministic)
        x = x + inputs

        # MLP block.
        y = nn.LayerNorm(dtype=self.dtype)(x)
        y = MlpBlock(
            mlp_dim=self.mlp_dim, dtype=self.dtype, dropout_rate=self.dropout_rate
        )(y, deterministic=deterministic)

        return x + y


class Transformer(nn.Module):
    """Transformer Model Encoder for sequence to sequence translation.

    Attributes:
      num_layers: number of layers
      mlp_dim: dimension of the mlp on top of attention block
      num_heads: Number of heads in nn.MultiHeadDotProductAttention
      dropout_rate: dropout rate.
      attention_dropout_rate: dropout rate in self attention.
    """

    num_layers: int
    mlp_dim: int
    num_heads: int
    dropout_rate: float = 0.1
    attention_dropout_rate: float = 0.1
    add_position_embedding: bool = False

    @nn.compact
    def __call__(self, x, attention_mask, *, train):
        """Applies Transformer model on the inputs.

        Args:
          x: Inputs to the layer.
          train: Set to `True` when training.

        Returns:
          output of a transformer encoder.
        """
        assert x.ndim == 3  # (batch, len, emb)

        if self.add_position_embedding:
            x = AddPositionEmbs(
                posemb_init=nn.initializers.normal(stddev=0.02),  # from BERT.
                name="posembed_input",
            )(x)
            x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=not train)

        # Input Encoder
        for lyr in range(self.num_layers):
            x = Encoder1DBlock(
                mlp_dim=self.mlp_dim,
                dropout_rate=self.dropout_rate,
                attention_dropout_rate=self.attention_dropout_rate,
                name=f"encoderblock_{lyr}",
                num_heads=self.num_heads,
            )(x, attention_mask, deterministic=not train)
        encoded = nn.LayerNorm(name="encoder_norm")(x)

        return encoded
