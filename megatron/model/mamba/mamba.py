import math

import torch
import torch.nn as nn
import torch.nn.functional as F

try:
    from mamba_ssm.ops.selective_scan_interface import (
        selective_scan_ref,
        selective_scan_fn,
        mamba_inner_fn,
    )
    from causal_conv1d import causal_conv1d_fn
    import einops
except ModuleNotFoundError:
    pass

from megatron.model.norms import get_norm


# Mamba layer, without parallelism.
class MambaBlock(nn.Module):
    def __init__(
        self,
        neox_args,
        init_method,
        output_layer_init_method,
    ):
        super().__init__()

        self.neox_args = neox_args

        dtype = {
            "fp16": torch.float16,
            "bf16": torch.bfloat16,
            "fp32": torch.float32,
        }[neox_args.precision]
        self.precision = dtype
        factory_kwargs = {"device": torch.cuda.current_device(), "dtype": dtype}

        # set variables, mostly following mamba defaults
        self.d_model = neox_args.hidden_size
        self.d_state = 16  # state dimensions per channel
        self.d_conv = 4  # convolution width
        self.expand = 2  # linear projection expansion factors
        self.d_inner = int(self.expand * self.d_model)
        self.dt_rank = math.ceil(self.d_model / 16)  # rank of dt / Delta parameter
        self.dt_scale = 1.0

        self.dt_init = "random"
        self.dt_min, self.dt_max, self.dt_init_floor = 0.001, 0.1, 1e-4
        assert self.dt_init in ["constant", "random"]

        # up-projection.
        self.in_proj = nn.Linear(
            self.d_model,
            self.d_inner * 2,
            bias=neox_args.mamba_use_bias_in_linears,
            **factory_kwargs,
        )
        init_method(self.in_proj.weight)

        # convolution.
        self.conv1d = nn.Conv1d(
            in_channels=self.d_inner,
            out_channels=self.d_inner,
            bias=neox_args.mamba_use_bias_in_conv,
            kernel_size=self.d_conv,
            groups=self.d_inner,
            padding=self.d_conv - 1,
            **factory_kwargs,
        )
        # Conv bias sometimes in 32-bit erroneously, when holding other parameters in fp32.
        # Uncertain why
        self.conv1d.to(self.precision)

        self.act_fn = F.silu  # we do not allow for

        # x_proj corresponds to s_B(x), s_C(x), s_Delta(x)
        # in https://arxiv.org/pdf/2312.00752.pdf Algorithm 2
        # (computes data-dependent B, C, Delta/dt)
        self.x_proj = nn.Linear(
            self.d_inner, self.dt_rank + self.d_state * 2, bias=False, **factory_kwargs
        )
        init_method(self.x_proj.weight)

        # up-project dt / Delta from dt_rank to d_inner
        self.dt_proj = nn.Linear(
            self.dt_rank, self.d_inner, bias=True, **factory_kwargs
        )

        # special init for dt_proj
        dt_init_std = (self.dt_rank**-0.5) * self.dt_scale
        if self.dt_init == "constant":
            nn.init.constant_(self.dt_proj.weight, dt_init_std)
        elif self.dt_init == "random":
            nn.init.uniform_(self.dt_proj.weight, -dt_init_std, dt_init_std)
        else:
            raise NotImplementedError

        # more dt_proj init stuff. copied from https://github.com/state-spaces/mamba/blob/009bec5ee37f586844a3fc89c040a9c1a9d8badf/mamba_ssm/modules/mamba_simple.py#L91-L101
        dt = torch.exp(
            torch.rand(self.d_inner, **factory_kwargs)
            * (math.log(self.dt_max) - math.log(self.dt_min))
            + math.log(self.dt_min)
        ).clamp(min=self.dt_init_floor)
        # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
        inv_dt = dt + torch.log(-torch.expm1(-dt))
        with torch.no_grad():
            self.dt_proj.bias.copy_(inv_dt)

        # initialize A . uses S4D real initialization
        A = einops.repeat(
            torch.arange(
                1,
                self.d_state + 1,
                dtype=torch.float32,
                device=torch.cuda.current_device(),
            ),
            "n -> d n",
            d=self.d_inner,
        ).contiguous()
        A_log = torch.log(A).to(
            torch.float32
        )  # Keep in fp32, following https://github.com/state-spaces/mamba#precision and code comments
        self.A_log = nn.Parameter(A_log)
        self.A_log._no_weight_decay = (
            True  # setting this attribute turns off weight decay for this param
        )
        # setting this attribute prevents deeperspeed from casting this param to fp32
        # requires commit ... or later
        if self.neox_args.mamba_selective_fp32_params:
            self.A_log._deepspeed_no_cast = True

        # D parameter
        self.D = nn.Parameter(
            torch.ones(
                self.d_inner, device=torch.cuda.current_device(), dtype=torch.float32
            )
        ).to(
            torch.float32
        )  # Keep in fp32, following https://github.com/state-spaces/mamba#precision and code comments
        self.D._no_weight_decay = (
            True  # setting this attribute turns off weight decay for this param
        )
        # setting this attribute prevents deeperspeed from casting this param to fp32
        # requires commit ... or later
        if self.neox_args.mamba_selective_fp32_params:
            self.D._deepspeed_no_cast = True

        # out down-projection
        self.out_proj = nn.Linear(
            self.d_inner,
            self.d_model,
            bias=neox_args.mamba_use_bias_in_linears,
            **factory_kwargs,
        )
        output_layer_init_method(self.out_proj.weight)

    def selective_scan(
        self,
        x,
        dt,
        A,
        B,
        C,
        D,
        z=None,
        delta_bias=None,
        delta_softplus=True,
    ):

        if not self.neox_args.mamba_selective_scan_fusion:
            y = selective_scan_ref(
                u=x,
                delta=dt,
                A=A,
                B=B,
                C=C,
                D=D,
                z=z,
                delta_bias=delta_bias,
                delta_softplus=delta_softplus,
                return_last_state=False,
            )
        else:
            y = selective_scan_fn(
                x,
                dt,
                A,
                B,
                C,
                D=D,
                z=z,
                delta_bias=delta_bias,
                delta_softplus=delta_softplus,
                return_last_state=False,
            )

        return y

    def forward(self, hidden_states):
        """ """
        # TODO: support inference natively in neox.
        # For now, we only handle training (parallel scan).
        assert self.training, "Mamba in NeoX does not support inference!"

        # hidden_states: [sq, b, h]
        seqlen, batch, dim = hidden_states.shape

        # first up: perform in_proj
        xz = einops.rearrange(
            self.in_proj.weight @ einops.rearrange(hidden_states, "l b d -> d (b l)"),
            "d (b l) -> b d l",
            l=seqlen,
        )

        if self.in_proj.bias is not None:
            xz = xz + einops.rearrange(self.in_proj.bias.to(dtype=xz.dtype), "d -> d 1")

        A = -torch.exp(self.A_log.float())  # (d_inner, d_state)

        if self.neox_args.mamba_inner_func_fusion:
            # =================
            # Fused mamba inner
            # =================

            # mamba provides a mamba_inner fn that computes the entire (post-in_proj) Mamba block.
            # we want to use it if we can, as it saves memory and provides speedups.
            # equivalent to use_fast_path=True in state-spaces/mamba.
            out = mamba_inner_fn(
                xz,
                self.conv1d.weight,
                # for some bizarre reason this becomes fp32 sometime after init, when A and D held in fp32.
                # cast it manually if the bias exists
                self.conv1d.bias.to(self.precision)
                if self.conv1d.bias is not None
                else self.conv1d.bias,
                self.x_proj.weight,
                self.dt_proj.weight,
                self.out_proj.weight,
                self.out_proj.bias,
                A,
                None,  # B is input-dependent, will compute from x_proj
                None,  # C is input-dependent, will compute from x_proj
                self.D.float(),
                delta_bias=self.dt_proj.bias.float(),
                delta_softplus=True,
            )

            out = einops.rearrange(out, "b l h -> l b h")

            return out

        x, z = xz.chunk(2, dim=1)

        # ===========
        # Convolution
        # ===========

        if not self.neox_args.mamba_causal_conv_fusion:
            self.conv1d.to(self.precision)  # required if keeping fp32 A_log, D
            x = self.act_fn(self.conv1d(x)[..., :seqlen])
        else:
            # Note: this requires silu as activation.
            x = causal_conv1d_fn(
                x=x,
                weight=einops.rearrange(self.conv1d.weight, "d 1 w -> d w"),
                bias=self.conv1d.bias.to(self.precision)
                if self.conv1d.bias is not None
                else self.conv1d.bias,
                activation="silu",
            )

        # ==============
        # SSM (S6) layer
        # ==============

        # project: perform s_B, s_C, s_Delta projections
        x_dbl = self.x_proj(einops.rearrange(x, "b d l -> (b l) d"))
        # split into component dt, B, C
        dt, B, C = torch.split(
            x_dbl, [self.dt_rank, self.d_state, self.d_state], dim=-1
        )

        # up-project Delta / dt
        dt = self.dt_proj.weight @ dt.t()
        dt = einops.rearrange(dt, "d (b l) -> b d l", l=seqlen)

        # rearrange B, C
        B = einops.rearrange(B, "(b l) d_state -> b d_state l", l=seqlen).contiguous()
        C = einops.rearrange(C, "(b l) d_state -> b d_state l", l=seqlen).contiguous()

        # perform selective scan.
        y = self.selective_scan(
            x,
            dt,
            A,
            B,
            C,
            self.D.float(),
            z=z,
            delta_bias=self.dt_proj.bias.float(),
            delta_softplus=True,
        )

        # ===============
        # Down-Projection
        # ===============
        y = einops.rearrange(y, "b d l -> b l d")

        out = self.out_proj(y)

        out = einops.rearrange(out, "b l h -> l b h")

        return out


class MambaResidualLayer(nn.Module):
    """
    Pre-norm Mamba Block with residual connection. No parallelism yet supported.
    """

    def __init__(
        self,
        neox_args,
        init_method,
        output_layer_init_method,
        layer_number,
    ):
        super().__init__()
        # TODO: allow for residual in fp32 if it helps?
        self.layer_number = layer_number

        norm, eps = get_norm(neox_args)

        self.norm = norm(neox_args.hidden_size, eps=eps)

        self.mixer = MambaBlock(
            neox_args=neox_args,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
        )

    def forward(self, x, attention_mask=None, layer_past=None):

        # pseudocode:
        # x = x + mixer(norm(x))
        residual = x

        hidden_states = self.mixer(self.norm(x))

        return hidden_states + residual


class MambaResidualLayerPipe(MambaResidualLayer):
    """Extends MambaResidualLayer to forward attention_mask through the pipeline. DeepSpeed requires this."""

    def forward(self, args):
        assert (
            len(args) == 2
        ), "MambaResidualLayerPipe expects 2 arguments - hidden_states and attention_mask"
        hidden_states, attention_mask = args
        # we are returning just [hidden_states, mask]
        return super().forward(hidden_states, attention_mask), attention_mask
