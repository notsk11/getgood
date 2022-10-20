import re
import os
import sys
import traceback
from collections import namedtuple

import torch
from torchvision import transforms
from torchvision.transforms.functional import InterpolationMode

from modules import cmd_opts, opts
from modules import devices, paths

blip_image_eval_size = 384
blip_model_url = 'https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_base_caption_capfilt_large.pth'
clip_model_name = 'ViT-L/14'

Category = namedtuple("Category", ["name", "topn", "items"])

re_topn = re.compile(r"\.top(\d+)\.")


class InterrogateModels:
    blip_model = None
    clip_model = None
    clip_preprocess = None
    categories = None
    dtype = None

    def __init__(self, content_dir):
        self.categories = []

        if os.path.exists(content_dir):
            for filename in os.listdir(content_dir):
                m = re_topn.search(filename)
                topn = 1 if m is None else int(m.group(1))

                with open(os.path.join(content_dir, filename), "r", encoding="utf8") as file:
                    lines = [x.strip() for x in file.readlines()]

                self.categories.append(Category(name=filename, topn=topn, items=lines))

    def load_blip_model(self):
        import blip

        blip_model = blip.blip_decoder(pretrained=blip_model_url, image_size=blip_image_eval_size, vit='base', 
                                       med_config=os.path.join(paths.REPO_PATHS["BLIP"], "configs", "med_config.json"))
        blip_model.eval()

        return blip_model

    def load_clip_model(self):
        import clip

        model, preprocess = clip.load(clip_model_name)
        model.eval()
        model = model.to(devices.device)

        return model, preprocess

    def load(self):
        if self.blip_model is None:
            self.blip_model = self.load_blip_model()
            if not cmd_opts.cmd_opts.no_half:
                self.blip_model = self.blip_model.half()

        self.blip_model = self.blip_model.to(cmd_opts.device)

        if self.clip_model is None:
            self.clip_model, self.clip_preprocess = self.load_clip_model()
            if not cmd_opts.cmd_opts.no_half:
                self.clip_model = self.clip_model.half()

        self.clip_model = self.clip_model.to(cmd_opts.device)

        self.dtype = next(self.clip_model.parameters()).dtype

    def send_clip_to_ram(self):
        if not opts.interrogate_keep_models_in_memory:
            if self.clip_model is not None:
                self.clip_model = self.clip_model.to(devices.cpu)

    def send_blip_to_ram(self):
        if not opts.interrogate_keep_models_in_memory:
            if self.blip_model is not None:
                self.blip_model = self.blip_model.to(devices.cpu)

    def unload(self):
        self.send_clip_to_ram()
        self.send_blip_to_ram()

        devices.torch_gc()

    def rank(self, image_features, text_array, top_count=1):
        import clip

        if opts.interrogate_clip_dict_limit != 0:
            text_array = text_array[0:int(opts.interrogate_clip_dict_limit)]

        top_count = min(top_count, len(text_array))
        text_tokens = clip.tokenize([text for text in text_array], truncate=True).to(cmd_opts.device)
        text_features = self.clip_model.encode_text(text_tokens).type(self.dtype)
        text_features /= text_features.norm(dim=-1, keepdim=True)

        similarity = torch.zeros((1, len(text_array))).to(cmd_opts.device)
        for i in range(image_features.shape[0]):
            similarity += (100.0 * image_features[i].unsqueeze(0) @ text_features.T).softmax(dim=-1)
        similarity /= image_features.shape[0]

        top_probs, top_labels = similarity.cpu().topk(top_count, dim=-1)
        return [(text_array[top_labels[0][i].numpy()], (top_probs[0][i].numpy()*100)) for i in range(top_count)]

    def generate_caption(self, pil_image):
        gpu_image = transforms.Compose([
            transforms.Resize((blip_image_eval_size, blip_image_eval_size), interpolation=InterpolationMode.BICUBIC),
            transforms.ToTensor(),
            transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
        ])(pil_image).unsqueeze(0).type(self.dtype).to(cmd_opts.device)

        with torch.no_grad():
            caption = self.blip_model.generate(gpu_image, sample=False, 
                                               num_beams=cmd_opts.opts.interrogate_clip_num_beams, 
                                               min_length=cmd_opts.opts.interrogate_clip_min_length, 
                                               max_length=cmd_opts.opts.interrogate_clip_max_length)

        return caption[0]

    def interrogate(self, pil_image):
        res = None

        try:

            if cmd_opts.cmd_opts.lowvram or cmd_opts.cmd_opts.medvram:
                devices.send_everything_to_cpu()
                devices.torch_gc()

            self.load()

            caption = self.generate_caption(pil_image)
            self.send_blip_to_ram()
            devices.torch_gc()

            res = caption

            clip_image = self.clip_preprocess(pil_image).unsqueeze(0).type(self.dtype).to(cmd_opts.device)

            with torch.no_grad(), devices.autocast("cuda"):
                image_features = self.clip_model.encode_image(clip_image).type(self.dtype)
                image_features /= image_features.norm(dim=-1, keepdim=True)

                if cmd_opts.opts.interrogate_use_builtin_artists:
                    artist = self.rank(image_features, ["by " + artist.name for artist in cmd_opts.artist_db.artists])[0]

                    res += ", " + artist[0]

                for name, topn, items in self.categories:
                    matches = self.rank(image_features, items, top_count=topn)
                    for match, score in matches:
                        res += ", " + match

        except Exception:
            print(f"Error interrogating", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            res += "<error>"

        self.unload()

        return res
