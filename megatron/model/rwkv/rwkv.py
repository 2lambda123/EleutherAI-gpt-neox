########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import os, math, gc, importlib
import torch
# torch._C._jit_set_profiling_executor(True)
# torch._C._jit_set_profiling_mode(True)
import torch.nn as nn
from torch.nn import functional as F
from rwkv.cuda import rwkv_cuda

########################################################################################################
# CUDA Kernel
########################################################################################################

from torch.utils.cpp_extension import load

HEAD_SIZE = 64

#wkv_cuda = load(name="wkv6", sources=["/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_op.cpp", f"/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_cuda.cu"],
#                verbose=True, extra_cuda_cflags=["-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3", "--extra-device-vectorization", f"-D_N_={HEAD_SIZE}", f"-D_T_={512}"])

class WKV(torch.autograd.Function):
    @staticmethod
    def forward(WKV, ctx, B, T, C, H, r, k, v, w, u):
        with torch.no_grad():
            assert r.dtype == torch.bfloat16
            assert k.dtype == torch.bfloat16
            assert v.dtype == torch.bfloat16
            assert w.dtype == torch.bfloat16
            assert u.dtype == torch.bfloat16
            assert HEAD_SIZE == C // H
            ctx.B = B
            ctx.T = T
            ctx.C = C
            ctx.H = H
            assert r.is_contiguous()
            assert k.is_contiguous()
            assert v.is_contiguous()
            assert w.is_contiguous()
            assert u.is_contiguous()
            ew = (-torch.exp(w.float())).contiguous()
            ctx.save_for_backward(r, k, v, ew, u)
            y = torch.empty((B, T, C), device=r.device, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            wkv_cuda.forward(B, T, C, H, r, k, v, ew, u, y)
            return y

    @staticmethod
    def backward(ctx, gy):
        with torch.no_grad():
            assert gy.dtype == torch.bfloat16
            B = ctx.B
            T = ctx.T
            C = ctx.C
            H = ctx.H
            assert gy.is_contiguous()
            r, k, v, ew, u = ctx.saved_tensors
            gr = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            gk = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            gv = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            gw = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            gu = torch.empty((B, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format)#.uniform_(-100, 100)
            wkv_cuda.backward(B, T, C, H, r, k, v, ew, u, gy, gr, gk, gv, gw, gu)
            gu = torch.sum(gu, 0).view(H, C//H)
            return (None, None, None, None, gr, gk, gv, gw, gu)

def RUN_CUDA_RWKV(WKV, B, T, C, H, r, k, v, w, u):
    return WKV.apply(B, T, C, H, r, k, v, w, u)

# RWKV6 time mix
class RWKV_TimeMix(nn.Module):
    def __init__(self, neox_args, layer_number):
        super().__init__()
        self.neox_args = neox_args
        self.layer_number = layer_number

        self.head_size = 64
        self.num_attention_heads = neox_args.dim_att // self.head_size
        assert neox_args.dim_att % self.num_attention_heads == 0
        self.wkv_cuda = load(name="wkv6", sources=["/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_op.cpp",
                                              f"/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_cuda.cu"],
                                              verbose=True,
                                              extra_cuda_cflags=["-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3",
                                                 "--extra-device-vectorization",
                                                 f"-D_N_={self.neox_args.head_size}", f"-D_T_={self.neox_args.seq_length}"])


        with torch.no_grad():
            ratio_0_to_1 = layer_number / (neox_args.num_layers - 1)  # 0 to 1
            ratio_1_to_almost0 = 1.0 - (layer_number / neox_args.num_layers)  # 1 to ~0
            ddd = torch.ones(1, 1, neox_args.hidden_size)
            for i in range(neox_args.hidden_size):
                ddd[0, 0, i] = i / neox_args.hidden_size

            # fancy time_mix
            self.time_maa_x = nn.Parameter(1.0 - torch.pow(ddd, ratio_1_to_almost0))
            self.time_maa_w = nn.Parameter(1.0 - torch.pow(ddd, ratio_1_to_almost0))
            self.time_maa_k = nn.Parameter(1.0 - torch.pow(ddd, ratio_1_to_almost0))
            self.time_maa_v = nn.Parameter(1.0 - (torch.pow(ddd, ratio_1_to_almost0) + 0.3 * ratio_0_to_1))
            self.time_maa_r = nn.Parameter(1.0 - torch.pow(ddd, 0.5 * ratio_1_to_almost0))
            self.time_maa_g = nn.Parameter(1.0 - torch.pow(ddd, 0.5 * ratio_1_to_almost0))

            TIME_MIX_EXTRA_DIM = 32 # generate TIME_MIX for w,k,v,r,g
            self.time_maa_w1 = nn.Parameter(torch.zeros(neox_args.hidden_size, TIME_MIX_EXTRA_DIM*5).uniform_(-1e-4, 1e-4))
            self.time_maa_w2 = nn.Parameter(torch.zeros(5, TIME_MIX_EXTRA_DIM, neox_args.hidden_size).uniform_(-1e-4, 1e-4))

            # fancy time_decay
            decay_speed = torch.ones(neox_args.dim_att)
            for n in range(neox_args.dim_att):
                decay_speed[n] = -6 + 5 * (n / (neox_args.dim_att - 1)) ** (0.7 + 1.3 * ratio_0_to_1)
            self.time_decay = nn.Parameter(decay_speed.reshape(1,1,neox_args.dim_att))

            TIME_DECAY_EXTRA_DIM = 64
            self.time_decay_w1 = nn.Parameter(torch.zeros(neox_args.hidden_size, TIME_DECAY_EXTRA_DIM).uniform_(-1e-4, 1e-4))
            self.time_decay_w2 = nn.Parameter(torch.zeros(TIME_DECAY_EXTRA_DIM, neox_args.dim_att).uniform_(-1e-4, 1e-4))

            tmp = torch.zeros(neox_args.dim_att)
            for n in range(neox_args.dim_att):
                zigzag = ((n + 1) % 3 - 1) * 0.1
                tmp[n] = ratio_0_to_1 * (1 - (n / (neox_args.dim_att - 1))) + zigzag

            self.time_faaaa = nn.Parameter(tmp.reshape(self.num_attention_heads, self.head_size))

        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))
        self.receptance = nn.Linear(neox_args.hidden_size, neox_args.dim_att, bias=False)
        self.key = nn.Linear(neox_args.hidden_size, neox_args.dim_att, bias=False)

        self.value = nn.Linear(neox_args.hidden_size, neox_args.dim_att, bias=False)
        self.output = nn.Linear(neox_args.dim_att, neox_args.hidden_size, bias=False)
        self.gate = nn.Linear(neox_args.hidden_size, neox_args.dim_att, bias=False)
        self.ln_x = nn.GroupNorm(self.num_attention_heads, neox_args.dim_att, eps=(1e-5)*(8**2))

    #@torch.jit.script
    def jit_func(self, x):
        B, T, C = x.size()

        xx = self.time_shift(x) - x

        xxx = x + xx * self.time_maa_x
        xxx = torch.tanh(xxx @ self.time_maa_w1).view(B*T, 5, -1).transpose(0, 1)
        xxx = torch.bmm(xxx, self.time_maa_w2).view(5, B, T, -1)
        mw, mk, mv, mr, mg = xxx.unbind(dim=0)

        xw = x + xx * (self.time_maa_w + mw)
        xk = x + xx * (self.time_maa_k + mk)
        xv = x + xx * (self.time_maa_v + mv)
        xr = x + xx * (self.time_maa_r + mr)
        xg = x + xx * (self.time_maa_g + mg)

        r = self.receptance(xr)
        k = self.key(xk)
        v = self.value(xv)
        g = F.silu(self.gate(xg))

        ww = torch.tanh(xw @ self.time_decay_w1) @ self.time_decay_w2
        w = self.time_decay + ww

        return r, k, v, g, w

    #@torch.jit.script
    def jit_func_2(self, x, g):
        B, T, C = x.size()
        x = x.view(B * T, C)

        x = self.ln_x(x).view(B, T, C)
        x = self.output(x * g)
        return x

    def forward(self, x):
        B, T, C = x.size()
        H = self.num_attention_heads

        r, k, v, g, w = self.jit_func(x)
        x = RUN_CUDA_RWKV(self.WKV, B, T, C, H, r, k, v, w, u=self.time_faaaa)

        return self.jit_func_2(x, g)

########################################################################################################

class RWKV_ChannelMix(nn.Module):
    def __init__(self, neox_args, layer_number):
        super().__init__()
        self.neox_args = neox_args
        self.layer_number = layer_number
        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))

        with torch.no_grad():  # fancy init of time_mix
            ratio_1_to_almost0 = 1.0 - (layer_number / neox_args.num_layers)  # 1 to ~0
            ddd = torch.ones(1, 1, neox_args.hidden_size)
            for i in range(neox_args.hidden_size):
                ddd[0, 0, i] = i / neox_args.hidden_size
            self.time_maa_k = nn.Parameter(1.0 - torch.pow(ddd, ratio_1_to_almost0))
            self.time_maa_r = nn.Parameter(1.0 - torch.pow(ddd, ratio_1_to_almost0))

        self.key = nn.Linear(neox_args.hidden_size, neox_args.dim_ffn, bias=False)
        self.receptance = nn.Linear(neox_args.hidden_size, neox_args.hidden_size, bias=False)
        self.value = nn.Linear(neox_args.dim_ffn, neox_args.hidden_size, bias=False)

    #@torch.jit.script
    def forward(self, x):
        xx = self.time_shift(x) - x
        xk = x + xx * self.time_maa_k
        xr = x + xx * self.time_maa_r

        k = self.key(xk)
        k = torch.relu(k) ** 2
        kv = self.value(k)
        return torch.sigmoid(self.receptance(xr)) * kv

########################################################################################################

class MishGLU(nn.Module):
    def __init__(self, neox_args, layer_number):
        super().__init__()
        self.neox_args = neox_args
        self.layer_number = layer_number
        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))

        with torch.no_grad():
            ratio_1_to_almost0 = 1.0 - (layer_number / neox_args.num_layers)

            x = torch.ones(1, 1, neox_args.hidden_size)
            for i in range(neox_args.hidden_size):
                x[0, 0, i] = i / neox_args.hidden_size

            self.time_mix_k = nn.Parameter(torch.pow(x, ratio_1_to_almost0))
            self.time_mix_r = nn.Parameter(torch.pow(x, ratio_1_to_almost0))
            self.aa = nn.Linear(neox_args.hidden_size, neox_args.dim_ffn, bias=False)
            self.bb = nn.Linear(neox_args.hidden_size, neox_args.dim_ffn, bias=False)
            self.value = nn.Linear(neox_args.dim_ffn, neox_args.hidden_size, bias=False)

    #@torch.jit.script
    def forward(self, x):
        xx = self.time_shift(x)
        xa = x * self.time_mix_k + xx * (1 - self.time_mix_k)
        xb = x * self.time_mix_r + xx * (1 - self.time_mix_r)
        a = self.aa(xa)
        b = self.bb(xb)
        return self.value(a * F.mish(b))

########################################################################################################
# The RWKV Model with our blocks
########################################################################################################

class RWKVResidualLayer(nn.Module):
    def __init__(self, neox_args, layer_number):
        super().__init__()
        self.neox_args = neox_args
        self.layer_number = layer_number
        self.fp16 = neox_args.precision == "fp16"
        self.bf16 = neox_args.precision == "bfloat16"
        if not hasattr(neox_args, 'dim_att'):
            neox_args.dim_att = neox_args.hidden_size
        if not hasattr(neox_args, 'dim_ffn'):
            neox_args.dim_ffn = neox_args.hidden_size * 4
        assert neox_args.hidden_size % 32 == 0
        assert neox_args.dim_att % 32 == 0
        assert neox_args.dim_ffn % 32 == 0

        if neox_args.attention_dropout > 0:
            self.drop0 = nn.Dropout(p = neox_args.attention_dropout)

        self.ln1 = nn.LayerNorm(neox_args.hidden_size)
        self.ln2 = nn.LayerNorm(neox_args.hidden_size)

        if self.layer_number == 0 and self.neox_args.rwkv_pre_ffn > 0:
            self.ffnPre = RWKV_ChannelMix(neox_args, 0)
        else:
            self.att = RWKV_TimeMix(neox_args, layer_number)

        if neox_args.rwkv_mishglu:
            self.ffn = MishGLU(neox_args, layer_number)
        else:
            self.ffn = RWKV_ChannelMix(neox_args, layer_number)

        if neox_args.attention_dropout > 0:
            self.drop0 = nn.Dropout(p = neox_args.attention_dropout)
        if neox_args.hidden_dropout > 0:
            self.drop1 = nn.Dropout(p = neox_args.hidden_dropout)

        global wkv_cuda
        wkv_cuda = load(name="wkv6", sources=["/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_op.cpp", f"/weka/home-jacob/gpt-neox/megatron/model/rwkv/cuda/wkv6_cuda.cu"],
                        verbose=True, extra_cuda_cflags=["-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3", "--extra-device-vectorization", f"-D_N_={HEAD_SIZE}", f"-D_T_={512}"])

    def forward(self, x):
        neox_args = self.neox_args
        B, T, C = x.size()
        if self.layer_number == 0:
            x = self.ln1(x)
            #if neox_args.pos_emb > 0:
            #    pos_emb = (self.pos_emb_x + self.pos_emb_y).reshape(T+1, -1)[:-1,:]
            #    x = x + pos_emb

        if self.neox_args.attention_dropout == 0 and self.neox_args.hidden_dropout == 0:
            if self.layer_number == 0 and neox_args.rwkv_pre_ffn > 0:
                x = x + self.ffnPre(self.ln1(x))
            else:
                x = x + self.att(self.ln1(x))
            x = x + self.ffn(self.ln2(x))
        else:
            if self.layer_number == 0 and neox_args.rwkv_pre_ffn > 0:
                x = self.drop0(x + self.ffnPre(self.ln1(x)))
            else:
                if self.neox_args.attention_dropout > 0:
                    x = self.drop0(x + self.att(self.ln1(x)))
                x = self.drop0(x + self.att(self.ln1(x)))
            if self.neox_args.hidden_dropout > 0:
                x = self.drop1(x + self.ffn(self.ln2(x)))

        return x

class RWKVResidualLayerPipe(RWKVResidualLayer):
    def forward(self, args):
        assert len(args) == 2
        hidden_states,  mask = args
        neox_args = self.neox_args
        return super().forward(hidden_states), mask
