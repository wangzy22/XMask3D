import logging
from typing import List, Optional, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as T
from ldm.models.diffusion.ddpm import LatentDiffusion as _LatentDiffusion
from ldm.modules.diffusionmodules.openaimodel import timestep_embedding
from ldm.modules.distributions.distributions import DiagonalGaussianDistribution
from ldm.util import instantiate_from_config
from omegaconf import OmegaConf
from timm.models.layers import trunc_normal_

from models.checkpoint.odise_checkpointer import LdmCheckpointer
from models.modeling.meta_arch.clip import ClipAdapter
from models.utils.file_io import PathManager

from ..diffusion import GaussianDiffusion, create_gaussian_diffusion
from ..preprocess import batched_input_to_device
from .helper import FeatureExtractor


def build_ldm_from_cfg(cfg_name) -> _LatentDiffusion:

    if cfg_name.startswith("v1"):
        url_prefix = "https://raw.githubusercontent.com/CompVis/stable-diffusion/main/configs/stable-diffusion/"
    elif cfg_name.startswith("v2"):
        url_prefix = "https://raw.githubusercontent.com/Stability-AI/stablediffusion/main/configs/stable-diffusion/"

    logging.getLogger(__name__).info(f"Loading LDM config from {cfg_name}")
    config = OmegaConf.load(PathManager.open(url_prefix + cfg_name))
    return instantiate_from_config(config.model)


class DisableLogger:
    "Disable HF logger"

    def __enter__(self):
        logging.disable(logging.CRITICAL)

    def __exit__(self, exit_type, exit_value, exit_traceback):
        logging.disable(logging.NOTSET)


def add_device_property(cls):
    class TempClass(cls):
        pass

    TempClass.device = property(lambda m: next(m.parameters()).device)

    return TempClass


class LatentDiffusion(nn.Module):

    LDM_CONFIGS = {
        "sd://v1-3": ("v1-inference.yaml", (512, 512), (64, 64)),
        "sd://v1-4": ("v1-inference.yaml", (512, 512), (64, 64)),
        "sd://v1-5": ("v1-inference.yaml", (512, 512), (64, 64)),
        "sd://v2-0-base": ("v2-inference.yaml", (512, 512), (64, 64)),
        "sd://v2-0-v": ("v2-inference.yaml", (768, 768), (96, 96)),
        "sd://v2-1-base": ("v2-inference.yaml", (512, 512), (64, 64)),
        "sd://v2-1-v": ("v2-inference.yaml", (768, 768), (96, 96)),
    }

    def __init__(
        self,
        diffusion: Optional[GaussianDiffusion] = None,
        guidance_scale: float = 7.5,
        pixel_mean: Tuple[float] = (0.5, 0.5, 0.5),
        pixel_std: Tuple[float] = (0.5, 0.5, 0.5),
        init_checkpoint="sd://v1-3",
    ):

        super().__init__()
        self._logger = logging.getLogger(__name__)

        ldm_cfg, image_size, latent_image_size = self.LDM_CONFIGS[init_checkpoint]

        with DisableLogger():
            self.ldm: _LatentDiffusion = build_ldm_from_cfg(ldm_cfg)

        self.ldm.cond_stage_model.__class__ = add_device_property(
            self.ldm.cond_stage_model.__class__
        )

        self.init_checkpoint = init_checkpoint
        self.load_pretrain()

        self.image_size = image_size
        self.latent_image_size = latent_image_size

        self.latent_dim = self.ldm.channels
        assert self.latent_dim == self.ldm.first_stage_model.embed_dim
        if diffusion is None:
            diffusion = create_gaussian_diffusion(
                steps=1000,
                learn_sigma=False,
                noise_schedule="ldm_linear",
            )
        self.diffusion = diffusion

        self.guidance_scale = guidance_scale

        self.register_buffer("uncond_inputs", self.embed_text([""]))

        self.register_buffer(
            "pixel_mean", torch.tensor(pixel_mean).view(-1, 1, 1), False
        )
        self.register_buffer("pixel_std", torch.tensor(pixel_std).view(-1, 1, 1), False)

    def load_pretrain(self):
        self.init_checkpoint = "sd_model/sd-v1-3.ckpt"
        LdmCheckpointer(self.ldm).load(self.init_checkpoint)

    @property
    def device(self):
        return self.pixel_mean.device

    def forward(self, batched_inputs):

        batched_inputs = batched_input_to_device(
            batched_inputs, next(self.parameters()).device
        )

        if self.training:
            return self.forward_train(batched_inputs)
        else:
            return self.forward_test(batched_inputs)

    def forward_train(self, batched_inputs):
        raise NotImplementedError

    def apply_model_with_guidence(self, x_noisy, t, cond):

        half = x_noisy[: len(x_noisy) // 2]
        combined = torch.cat([half, half], dim=0)
        model_out = self.ldm.apply_model(combined, t, cond)
        eps, rest = model_out[:, : self.latent_dim], model_out[:, self.latent_dim :]
        cond_eps, uncond_eps = torch.split(eps, len(eps) // 2, dim=0)
        half_eps = uncond_eps + self.guidance_scale * (cond_eps - uncond_eps)
        eps = torch.cat([half_eps, half_eps], dim=0)
        return torch.cat([eps, rest], dim=1)

    def embed_text(self, text):
        return self.ldm.get_learned_conditioning(text)

    @property
    def encoder(self):
        return self.ldm.first_stage_model.encoder

    @property
    def unet(self):
        return self.ldm.model.diffusion_model

    @property
    def decoder(self):
        return self.ldm.first_stage_model.decoder

    @torch.no_grad()
    def encode_to_latent(self, input_image):
        encoder_posterior = self.ldm.encode_first_stage(input_image)

        latent_image = self.ldm.get_first_stage_encoding(encoder_posterior.mean)

        return latent_image

    @torch.no_grad()
    def decode_from_latent(self, latent_image):
        return self.ldm.decode_first_stage(latent_image)

    def forward_test(self, batched_inputs):

        caption = batched_inputs["caption"]
        batch_size = len(caption)

        cond_inputs = self.embed_text(caption)

        if self.guidance_scale != 1.0:
            uncond_inputs = self.uncond_inputs.expand_as(cond_inputs)
        else:
            uncond_inputs = None

        if uncond_inputs is None:
            latent_samples = self.diffusion.ddim_sample_loop(
                model=self.ldm.apply_model,
                shape=(batch_size, self.latent_dim, *self.latent_image_size),
                device=self.device,
                clip_denoised=False,
                model_kwargs={"cond": cond_inputs},
            )
        else:

            latent_samples = self.diffusion.ddim_sample_loop(
                model=self.apply_model_with_guidence,
                shape=(batch_size * 2, self.latent_dim, *self.latent_image_size),
                device=self.device,
                clip_denoised=False,
                model_kwargs={"cond": torch.cat([cond_inputs, uncond_inputs], dim=0)},
            )[:batch_size]

        decoded_samples = self.ldm.decode_first_stage(latent_samples)
        out_samples = decoded_samples * self.pixel_std + self.pixel_mean
        out_samples = out_samples.clamp(0.0, 1.0)

        return out_samples


class LdmExtractor(FeatureExtractor):
    def __init__(
        self,
        ldm: Optional[LatentDiffusion] = None,
        encoder_block_indices: Tuple[int, ...] = (5, 7),
        unet_block_indices: Tuple[int, ...] = (2, 5, 8, 11),
        decoder_block_indices: Tuple[int, ...] = (2, 5),
        steps: Tuple[int, ...] = (0,),
        share_noise: bool = True,
        enable_resize: bool = False,
    ):

        super().__init__()

        self.encoder_block_indices = encoder_block_indices
        self.unet_block_indices = unet_block_indices
        self.decoder_block_indices = decoder_block_indices

        self.steps = steps

        if ldm is not None:
            self.ldm = ldm
        else:
            self.ldm = LatentDiffusion()
        if enable_resize:
            self.image_preprocess = T.Resize(
                size=self.ldm.image_size, interpolation=T.InterpolationMode.BICUBIC
            )
        else:
            self.image_preprocess = None

        if share_noise:

            rng = torch.Generator().manual_seed(42)
            self.register_buffer(
                "shared_noise",
                torch.randn(
                    1, self.ldm.latent_dim, *self.ldm.latent_image_size, generator=rng
                ),
            )
        else:
            self.shared_noise = None

        self.reset_dim_stride()
        self._freeze()

    def reset_dim_stride(self):

        all_encoder_blocks = []
        for i_level in range(self.ldm.encoder.num_resolutions):
            for i_block in range(self.ldm.encoder.num_res_blocks):
                all_encoder_blocks.append(self.ldm.encoder.down[i_level].block[i_block])

        encoder_dims = []
        encoder_strides = []
        encoder_blocks = []
        for idx in self.encoder_block_indices:
            encoder_dims.append(all_encoder_blocks[idx].in_channels)
            group_size = 2
            encoder_strides.append(2 ** ((idx + group_size) // group_size - 1))
            encoder_blocks.append(all_encoder_blocks[idx])

        assert set(self.unet_block_indices).issubset(
            set(range(len(self.ldm.unet.output_blocks)))
        )
        unet_dims = []
        unet_strides = []
        unet_blocks = []
        for idx, block in enumerate(self.ldm.unet.output_blocks):
            if idx in self.unet_block_indices:

                unet_dims.append(block[0].channels)

                group_size = 3
                unet_strides.append(64 // (2 ** ((idx + group_size) // group_size - 1)))
                unet_blocks.append(block)

        all_decoder_blocks = []

        for i_level in reversed(range(self.ldm.decoder.num_resolutions)):
            for i_block in range(self.ldm.decoder.num_res_blocks + 1):
                all_decoder_blocks.append(self.ldm.decoder.up[i_level].block[i_block])

        decoder_dims = []
        decoder_strides = []
        decoder_blocks = []
        for idx in self.decoder_block_indices:
            decoder_dims.append(all_decoder_blocks[idx].in_channels)
            group_size = 3
            decoder_strides.append(8 // (2 ** ((idx + group_size) // group_size - 1)))
            decoder_blocks.append(all_decoder_blocks[idx])

        feature_dims = encoder_dims + unet_dims * len(self.steps) + decoder_dims
        feature_strides = (
            encoder_strides + unet_strides * len(self.steps) + decoder_strides
        )

        self.encoder_blocks = encoder_blocks
        self.unet_blocks = unet_blocks
        self.decoder_blocks = decoder_blocks

        return feature_dims, feature_strides

    @property
    def feature_size(self):
        return self.ldm.image_size

    @property
    def feature_dims(self):
        return self.reset_dim_stride()[0]

    @property
    def feature_strides(self):
        return self.reset_dim_stride()[1]

    @property
    def num_groups(self) -> int:

        num_groups = len(self.encoder_block_indices)
        num_groups += len(self.unet_block_indices)
        num_groups += len(self.decoder_block_indices)
        return num_groups

    @property
    def grouped_indices(self):

        ret = []

        for i in range(len(self.encoder_block_indices)):
            ret.append([i])

        offset = len(self.encoder_block_indices)

        for i in range(len(self.unet_block_indices)):
            cur_indices = []
            for t in range(len(self.steps)):
                cur_indices.append(i + t * len(self.unet_block_indices) + offset)
            ret.append(cur_indices)

        offset += len(self.steps) * len(self.unet_block_indices)

        for i in range(len(self.decoder_block_indices)):
            ret.append([i + offset])
        return ret

    @property
    def pixel_mean(self):
        return self.ldm.pixel_mean

    @property
    def pixel_std(self):
        return self.ldm.pixel_std

    @property
    def device(self):
        return self.ldm.device

    @torch.no_grad()
    def build_text_embed(self, text: List[List[str]], batch_size=64, flatten=True):
        if isinstance(text, str):
            text = [text]
        if isinstance(text[0], str):
            text = [[t] for t in text]

        assert isinstance(text[0], list)

        flatten_text = [t for sublist in text for t in sublist]

        text_embed_list = []

        for i in range(0, len(flatten_text), batch_size):
            cur_text = flatten_text[i : i + batch_size]
            text_embed = self.ldm.embed_text(cur_text)
            text_embed_list.append(text_embed)

        return torch.concat(text_embed_list, dim=0)

    def encoder_forward(self, x):
        encoder = self.ldm.encoder
        ret_features = []

        temb = None

        hs = [encoder.conv_in(x)]
        for i_level in range(encoder.num_resolutions):
            for i_block in range(encoder.num_res_blocks):

                if encoder.down[i_level].block[i_block] in self.encoder_blocks:
                    ret_features.append(hs[-1].contiguous())

                h = encoder.down[i_level].block[i_block](hs[-1], temb)
                if len(encoder.down[i_level].attn) > 0:
                    h = encoder.down[i_level].attn[i_block](h)
                hs.append(h)
            if i_level != encoder.num_resolutions - 1:
                hs.append(encoder.down[i_level].downsample(hs[-1]))

        h = hs[-1]
        h = encoder.mid.block_1(h, temb)
        h = encoder.mid.attn_1(h)
        h = encoder.mid.block_2(h, temb)

        h = encoder.norm_out(h)
        h = h * torch.sigmoid(h)
        h = encoder.conv_out(h)
        return h, ret_features

    def encode_to_latent(self, image: torch.Tensor):
        h, ret_features = self.encoder_forward(image)
        moments = self.ldm.ldm.first_stage_model.quant_conv(h)
        posterior = DiagonalGaussianDistribution(moments)

        latent_image = self.ldm.ldm.scale_factor * posterior.mean

        return latent_image, ret_features

    def unet_forward(self, x, timesteps, context, cond_emb=None):
        unet = self.ldm.unet
        ret_features = []

        hs = []
        t_emb = timestep_embedding(timesteps, unet.model_channels, repeat_only=False)
        emb = unet.time_embed(t_emb)
        if cond_emb is not None:
            emb += cond_emb

        h = x
        for module in unet.input_blocks:
            h = module(h, emb, context)
            hs.append(h)
        h = unet.middle_block(h, emb, context)
        for module in unet.output_blocks:
            h = torch.cat([h, hs.pop()], dim=1)
            if module in self.unet_blocks:
                ret_features.append(h.contiguous())
            h = module(h, emb, context)

        return unet.out(h), ret_features

    def decoder_forward(self, z):
        decoder = self.ldm.decoder
        ret_features = []

        decoder.last_z_shape = z.shape

        temb = None

        h = decoder.conv_in(z)

        h = decoder.mid.block_1(h, temb)
        h = decoder.mid.attn_1(h)
        h = decoder.mid.block_2(h, temb)

        for i_level in reversed(range(decoder.num_resolutions)):
            for i_block in range(decoder.num_res_blocks + 1):

                if decoder.up[i_level].block[i_block] in self.decoder_blocks:
                    ret_features.append(h.contiguous())

                h = decoder.up[i_level].block[i_block](h, temb)
                if len(decoder.up[i_level].attn) > 0:
                    h = decoder.up[i_level].attn[i_block](h)
            if i_level != 0:
                h = decoder.up[i_level].upsample(h)

        if decoder.give_pre_end:
            return h

        h = decoder.norm_out(h)
        h = h * torch.sigmoid(h)
        h = decoder.conv_out(h)
        if decoder.tanh_out:
            h = torch.tanh(h)
        return h, ret_features

    def decode_to_image(self, z):
        z = 1.0 / self.ldm.ldm.scale_factor * z

        z = self.ldm.ldm.first_stage_model.post_quant_conv(z)
        dec, ret_features = self.decoder_forward(z)

        return dec, ret_features

    def forward(self, batched_inputs):

        features = []

        image = batched_inputs["img"]
        batch_size = image.shape[0]

        if self.image_preprocess is None:
            normalized_image = (image - self.pixel_mean) / self.pixel_std
        else:
            normalized_image = self.image_preprocess(
                (image - self.pixel_mean) / self.pixel_std
            )

        if "caption" in batched_inputs:
            captions = batched_inputs["caption"]
        else:
            captions = [""] * batch_size

        latent_image, encoder_features = self.encode_to_latent(normalized_image)
        cond_inputs = batched_inputs.get("cond_inputs", self.ldm.embed_text(captions))

        unet_features = []
        for i, t in enumerate(self.steps):

            if "cond_emb" in batched_inputs:
                cond_emb = batched_inputs["cond_emb"][:, i]
            else:
                cond_emb = None

            if t < 0:
                noisy_latent_image = latent_image

                t = torch.tensor([0], device=self.device).expand(batch_size)
            else:
                t = torch.tensor([t], device=self.device).expand(batch_size)
                if self.shared_noise is not None:
                    if self.shared_noise.shape[2:] != latent_image.shape[2:]:
                        assert self.image_preprocess is None
                        shared_noise = F.interpolate(
                            self.shared_noise,
                            size=latent_image.shape[2:],
                            mode="bicubic",
                            align_corners=False,
                        )
                    else:
                        shared_noise = self.shared_noise
                    noise = shared_noise.expand_as(latent_image)
                else:
                    noise = None

                noisy_latent_image = self.ldm.diffusion.q_sample(latent_image, t, noise)

            _, cond_unet_features = self.unet_forward(
                noisy_latent_image, t, cond_inputs, cond_emb=cond_emb
            )
            unet_features.extend(cond_unet_features)

        _, decoder_features = self.decode_to_image(latent_image)

        features = [*encoder_features, *unet_features, *decoder_features]

        assert len(features) == len(
            self.feature_dims
        ), f"{len(features)} != {len(self.feature_dims)}"

        for indices in self.grouped_indices:
            for idx in indices:
                if self.image_preprocess is not None:
                    continue
                assert (
                    image.shape[-2] // self.feature_strides[idx]
                    == features[idx].shape[-2]
                )
                assert (
                    image.shape[-1] // self.feature_strides[idx]
                    == features[idx].shape[-1]
                )

        return features


class PositionalLinear(nn.Module):
    def __init__(self, in_features, out_features, seq_len=77, bias=True):
        super().__init__()

        self.linear = nn.Linear(in_features, out_features, bias=bias)
        self.positional_embedding = nn.Parameter(torch.zeros(1, seq_len, out_features))
        trunc_normal_(self.positional_embedding, std=0.02)

    def forward(self, x):

        x = self.linear(x)
        if x.dim() == 2:
            x = x.unsqueeze(1) + self.positional_embedding

        return x


class LdmImplicitCaptionerExtractor(nn.Module):
    def __init__(
        self,
        learnable_time_embed=True,
        num_timesteps=1,
        dim_latent=768,
        clip=None,
        **kwargs,
    ):
        super().__init__()
        self.clip = clip
        self.ldm_extractor = LdmExtractor(**kwargs)

        self.text_embed_shape = self.ldm_extractor.ldm.embed_text([""]).shape[1:]

        self.clip_project = PositionalLinear(
            dim_latent, self.text_embed_shape[1], self.text_embed_shape[0]
        )
        self.alpha_cond = nn.Parameter(
            torch.zeros_like(self.ldm_extractor.ldm.uncond_inputs)
        )

        self.learnable_time_embed = learnable_time_embed

        if self.learnable_time_embed:

            self.time_embed_project = PositionalLinear(
                dim_latent,
                self.ldm_extractor.ldm.unet.time_embed[-1].out_features,
                num_timesteps,
            )
            self.alpha_cond_time_embed = nn.Parameter(
                torch.zeros(self.ldm_extractor.ldm.unet.time_embed[-1].out_features)
            )

    @property
    def feature_size(self):
        return self.ldm_extractor.feature_size

    @property
    def feature_dims(self):
        return self.ldm_extractor.feature_dims

    @property
    def feature_strides(self):
        return self.ldm_extractor.feature_strides

    @property
    def num_groups(self) -> int:

        return self.ldm_extractor.num_groups

    @property
    def grouped_indices(self):

        return self.ldm_extractor.grouped_indices

    def extra_repr(self):
        return f"learnable_time_embed={self.learnable_time_embed}"

    def forward(self, batched_inputs, prefix):

        image = batched_inputs["img"]

        if self.clip != None:
            prefix = self.clip.embed_image(image).image_embed

        prefix_embed = self.clip_project(prefix)

        batched_inputs["cond_inputs"] = (
            self.ldm_extractor.ldm.uncond_inputs
            + torch.tanh(self.alpha_cond) * prefix_embed
        )

        if self.learnable_time_embed:
            batched_inputs["cond_emb"] = torch.tanh(
                self.alpha_cond_time_embed
            ) * self.time_embed_project(prefix)

        self.set_requires_grad(self.training)

        return self.ldm_extractor(batched_inputs)

    def set_requires_grad(self, requires_grad):
        for p in self.ldm_extractor.ldm.ldm.model.parameters():
            p.requires_grad = requires_grad
