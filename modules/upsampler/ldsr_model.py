import os
import sys
import time
import warnings
import traceback
import gc

import torch
import torchvision
import numpy as np
from PIL import Image
from einops import rearrange, repeat
from omegaconf import OmegaConf
from basicsr.utils.download_util import load_file_from_url

from modules.cmd_opts import cmd_opts
from modules.upsampler.upscaler import Upscaler, UpscalerData

from ldm.models.diffusion.ddim import DDIMSampler
from ldm.util import instantiate_from_config, ismap

warnings.filterwarnings("ignore", category=UserWarning)


# Create LDSR Class
class LDSR:
    def load_model_from_config(self, half_attention):
        print(f"Loading model from {self.modelPath}")
        pl_sd = torch.load(self.modelPath, map_location="cpu")
        sd = pl_sd["state_dict"]
        config = OmegaConf.load(self.yamlPath)
        model = instantiate_from_config(config.model)
        model.load_state_dict(sd, strict=False)
        model.cuda()
        if half_attention:
            model = model.half()

        model.eval()
        return {"model": model}

    def __init__(self, model_path, yaml_path):
        self.modelPath = model_path
        self.yamlPath = yaml_path

    @staticmethod
    def run(model, selected_path, custom_steps, eta):
        example = get_cond(selected_path)

        n_runs = 1
        guider = None
        ckwargs = None
        ddim_use_x0_pred = False
        temperature = 1.
        eta = eta
        custom_shape = None

        height, width = example["image"].shape[1:3]
        split_input = height >= 128 and width >= 128

        if split_input:
            ks = 128
            stride = 64
            vqf = 4  #
            model.split_input_params = {"ks": (ks, ks), "stride": (stride, stride),
                                        "vqf": vqf,
                                        "patch_distributed_vq": True,
                                        "tie_braker": False,
                                        "clip_max_weight": 0.5,
                                        "clip_min_weight": 0.01,
                                        "clip_max_tie_weight": 0.5,
                                        "clip_min_tie_weight": 0.01}
        else:
            if hasattr(model, "split_input_params"):
                delattr(model, "split_input_params")

        x_t = None
        logs = None
        for n in range(n_runs):
            if custom_shape is not None:
                x_t = torch.randn(1, custom_shape[1], custom_shape[2], custom_shape[3]).to(model.device)
                x_t = repeat(x_t, '1 c h w -> b c h w', b=custom_shape[0])

            logs = make_convolutional_sample(example, model,
                                             custom_steps=custom_steps,
                                             eta=eta, quantize_x0=False,
                                             custom_shape=custom_shape,
                                             temperature=temperature, noise_dropout=0.,
                                             corrector=guider, corrector_kwargs=ckwargs, x_T=x_t,
                                             ddim_use_x0_pred=ddim_use_x0_pred
                                             )
        return logs

    def super_resolution(self, image, steps=100, target_scale=2, half_attention=False):
        model = self.load_model_from_config(half_attention)

        # Run settings
        diffusion_steps = int(steps)
        eta = 1.0

        down_sample_method = 'Lanczos'

        gc.collect()
        torch.cuda.empty_cache()

        im_og = image
        width_og, height_og = im_og.size
        # If we can adjust the max upscale size, then the 4 below should be our variable
        down_sample_rate = target_scale / 4
        wd = width_og * down_sample_rate
        hd = height_og * down_sample_rate
        width_downsampled_pre = int(wd)
        height_downsampled_pre = int(hd)

        if down_sample_rate != 1:
            print(
                f'Downsampling from [{width_og}, {height_og}] to [{width_downsampled_pre}, {height_downsampled_pre}]')
            im_og = im_og.resize((width_downsampled_pre, height_downsampled_pre), Image.LANCZOS)
        else:
            print(f"Down sample rate is 1 from {target_scale} / 4 (Not downsampling)")
        logs = self.run(model["model"], im_og, diffusion_steps, eta)

        sample = logs["sample"]
        sample = sample.detach().cpu()
        sample = torch.clamp(sample, -1., 1.)
        sample = (sample + 1.) / 2. * 255
        sample = sample.numpy().astype(np.uint8)
        sample = np.transpose(sample, (0, 2, 3, 1))
        a = Image.fromarray(sample[0])

        del model
        gc.collect()
        torch.cuda.empty_cache()
        return a


def get_cond(selected_path):
    example = dict()
    up_f = 4
    c = selected_path.convert('RGB')
    c = torch.unsqueeze(torchvision.transforms.ToTensor()(c), 0)
    c_up = torchvision.transforms.functional.resize(c, size=[up_f * c.shape[2], up_f * c.shape[3]],
                                                    antialias=True)
    c_up = rearrange(c_up, '1 c h w -> 1 h w c')
    c = rearrange(c, '1 c h w -> 1 h w c')
    c = 2. * c - 1.

    c = c.to(torch.device("cuda"))
    example["LR_image"] = c
    example["image"] = c_up

    return example


@torch.no_grad()
def convsample_ddim(model, cond, steps, shape, eta=1.0, callback=None, normals_sequence=None,
                    mask=None, x0=None, quantize_x0=False, temperature=1., score_corrector=None,
                    corrector_kwargs=None, x_t=None
                    ):
    ddim = DDIMSampler(model)
    bs = shape[0]
    shape = shape[1:]
    print(f"Sampling with eta = {eta}; steps: {steps}")
    samples, intermediates = ddim.sample(steps, batch_size=bs, shape=shape, conditioning=cond, callback=callback,
                                         normals_sequence=normals_sequence, quantize_x0=quantize_x0, eta=eta,
                                         mask=mask, x0=x0, temperature=temperature, verbose=False,
                                         score_corrector=score_corrector,
                                         corrector_kwargs=corrector_kwargs, x_t=x_t)

    return samples, intermediates


@torch.no_grad()
def make_convolutional_sample(batch, model, custom_steps=None, eta=1.0, quantize_x0=False, custom_shape=None, temperature=1., noise_dropout=0., corrector=None,
                              corrector_kwargs=None, x_T=None, ddim_use_x0_pred=False):
    log = dict()

    z, c, x, xrec, xc = model.get_input(batch, model.first_stage_key,
                                        return_first_stage_outputs=True,
                                        force_c_encode=not (hasattr(model, 'split_input_params')
                                                            and model.cond_stage_key == 'coordinates_bbox'),
                                        return_original_cond=True)

    if custom_shape is not None:
        z = torch.randn(custom_shape)
        print(f"Generating {custom_shape[0]} samples of shape {custom_shape[1:]}")

    z0 = None

    log["input"] = x
    log["reconstruction"] = xrec

    if ismap(xc):
        log["original_conditioning"] = model.to_rgb(xc)
        if hasattr(model, 'cond_stage_key'):
            log[model.cond_stage_key] = model.to_rgb(xc)

    else:
        log["original_conditioning"] = xc if xc is not None else torch.zeros_like(x)
        if model.cond_stage_model:
            log[model.cond_stage_key] = xc if xc is not None else torch.zeros_like(x)
            if model.cond_stage_key == 'class_label':
                log[model.cond_stage_key] = xc[model.cond_stage_key]

    with model.ema_scope("Plotting"):
        t0 = time.time()

        sample, intermediates = convsample_ddim(model, c, steps=custom_steps, shape=z.shape,
                                                eta=eta,
                                                quantize_x0=quantize_x0, mask=None, x0=z0,
                                                temperature=temperature, score_corrector=corrector, corrector_kwargs=corrector_kwargs,
                                                x_t=x_T)
        t1 = time.time()

        if ddim_use_x0_pred:
            sample = intermediates['pred_x0'][-1]

    x_sample = model.decode_first_stage(sample)

    try:
        x_sample_noquant = model.decode_first_stage(sample, force_not_quantize=True)
        log["sample_noquant"] = x_sample_noquant
        log["sample_diff"] = torch.abs(x_sample_noquant - x_sample)
    except:
        pass

    log["sample"] = x_sample
    log["time"] = t1 - t0

    return log


class UpscalerLDSR(Upscaler):
    def __init__(self, user_path):
        self.name = "LDSR"
        self.user_path = user_path
        self.model_url = "https://heibox.uni-heidelberg.de/f/578df07c8fc04ffbadf3/?dl=1"
        self.yaml_url = "https://heibox.uni-heidelberg.de/f/31a76b13ea27482981b4/?dl=1"
        super().__init__()
        scaler_data = UpscalerData("LDSR", None, self)
        self.scalers = [scaler_data]

    def load_model(self, path: str):
        # Remove incorrect project.yaml file if too big
        yaml_path = os.path.join(self.model_path, "project.yaml")
        old_model_path = os.path.join(self.model_path, "model.pth")
        new_model_path = os.path.join(self.model_path, "model.ckpt")
        if os.path.exists(yaml_path):
            statinfo = os.stat(yaml_path)
            if statinfo.st_size >= 10485760:
                print("Removing invalid LDSR YAML file.")
                os.remove(yaml_path)
        if os.path.exists(old_model_path):
            print("Renaming model from model.pth to model.ckpt")
            os.rename(old_model_path, new_model_path)
        model = load_file_from_url(url=self.model_url, model_dir=self.model_path,
                                   file_name="model.ckpt", progress=True)
        yaml = load_file_from_url(url=self.yaml_url, model_dir=self.model_path,
                                  file_name="project.yaml", progress=True)

        try:
            return LDSR(model, yaml)

        except Exception:
            print("Error importing LDSR:", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
        return None

    def do_upscale(self, img, path):
        ldsr = self.load_model(path)
        if ldsr is None:
            print("NO LDSR!")
            return img
        ddim_steps = cmd_opts.ldsr_steps
        return ldsr.super_resolution(img, ddim_steps, self.scale)
