import glob
import logging
import os
import re
from os import PathLike
from pathlib import Path
from typing import Any, Dict, List, Union

import numpy as np
import torch
from controlnet_aux import LineartAnimeDetector
from controlnet_aux.processor import MODELS
from controlnet_aux.processor import Processor as ControlnetPreProcessor
from controlnet_aux.util import HWC3, ade_palette
from controlnet_aux.util import resize_image as aux_resize_image
from diffusers import (
    AutoencoderKL,
    ControlNetModel,
    DiffusionPipeline,
    StableDiffusionControlNetImg2ImgPipeline,
    StableDiffusionPipeline,
)
from PIL import Image
from tqdm.rich import tqdm
from transformers import (
    AutoImageProcessor,
    CLIPImageProcessor,
    CLIPTextModel,
    CLIPTokenizer,
    UperNetForSemanticSegmentation,
)

from animatediff import get_dir
from animatediff.dwpose import DWposeDetector
from animatediff.models.clip import CLIPSkipTextModel
from animatediff.models.unet import UNet3DConditionModel
from animatediff.pipelines import AnimationPipeline, load_text_embeddings, AnimationPipelineOutput
from animatediff.pipelines.pipeline_controlnet_img2img_reference import (
    StableDiffusionControlNetImg2ImgReferencePipeline,
)
from animatediff.schedulers import get_scheduler
from animatediff.settings import InferenceConfig, ModelConfig
from animatediff.utils.convert_from_ckpt import convert_ldm_vae_checkpoint
from animatediff.utils.convert_lora_safetensor_to_diffusers import convert_lora
from animatediff.utils.model import ensure_motion_modules, get_checkpoint_weights
from animatediff.utils.util import (
    get_resized_image,
    get_resized_image2,
    get_resized_images,
    get_tensor_interpolation_method,
    prepare_dwpose,
    prepare_ip_adapter,
    prepare_motion_module,
    save_frames,
    save_imgs,
    save_video,
)
from functools import partial


try:
    import onnxruntime

    onnxruntime_installed = True
except:
    onnxruntime_installed = False


logger = logging.getLogger(__name__)

data_dir = get_dir("data")
default_base_path = data_dir.joinpath("models/huggingface/stable-diffusion-v1-5")

re_clean_prompt = re.compile(r"[^\w\-, ]")

controlnet_preprocessor = {}


def load_safetensors_lora(text_encoder, unet, lora_path, alpha=0.75, is_animatediff=True):
    from safetensors.torch import load_file

    from animatediff.utils.lora_diffusers import LoRANetwork, create_network_from_weights

    sd = load_file(lora_path)

    print(f"create LoRA network")
    lora_network: LoRANetwork = create_network_from_weights(
        text_encoder, unet, sd, multiplier=alpha, is_animatediff=is_animatediff
    )
    print(f"load LoRA network weights")
    lora_network.load_state_dict(sd, False)
    lora_network.merge_to(alpha)


def load_tensors(path: Path, framework="pt", device="cpu"):
    tensors = {}
    if path.suffix == ".safetensors":
        from safetensors import safe_open

        with safe_open(path, framework=framework, device=device) as f:
            for k in f.keys():
                tensors[k] = f.get_tensor(k)  # loads the full tensor given a key
    else:
        from torch import load

        tensors = load(path, device)
        if "state_dict" in tensors:
            tensors = tensors["state_dict"]
    return tensors


def load_motion_lora(unet, lora_path: Path, alpha=1.0):
    state_dict = load_tensors(lora_path)

    # directly update weight in diffusers model
    for key in state_dict:
        # only process lora down key
        if "up." in key:
            continue

        up_key = key.replace(".down.", ".up.")
        model_key = key.replace("processor.", "").replace("_lora", "").replace("down.", "").replace("up.", "")
        model_key = model_key.replace("to_out.", "to_out.0.")
        layer_infos = model_key.split(".")[:-1]

        curr_layer = unet
        try:
            while len(layer_infos) > 0:
                temp_name = layer_infos.pop(0)
                curr_layer = curr_layer.__getattr__(temp_name)
        except:
            logger.info(f"{model_key} not found")
            continue

        weight_down = state_dict[key]
        weight_up = state_dict[up_key]
        curr_layer.weight.data += alpha * torch.mm(weight_up, weight_down).to(curr_layer.weight.data.device)


class SegPreProcessor:
    def __init__(self):
        self.image_processor = AutoImageProcessor.from_pretrained("openmmlab/upernet-convnext-small")
        self.processor = UperNetForSemanticSegmentation.from_pretrained("openmmlab/upernet-convnext-small")

    def __call__(self, input_image, detect_resolution=512, image_resolution=512, output_type="pil", **kwargs):
        input_array = np.array(input_image, dtype=np.uint8)
        input_array = HWC3(input_array)
        input_array = aux_resize_image(input_array, detect_resolution)

        pixel_values = self.image_processor(input_array, return_tensors="pt").pixel_values

        with torch.no_grad():
            outputs = self.processor(pixel_values.to(self.processor.device))

        outputs.loss = outputs.loss.to("cpu") if outputs.loss is not None else outputs.loss
        outputs.logits = outputs.logits.to("cpu") if outputs.logits is not None else outputs.logits
        outputs.hidden_states = (
            outputs.hidden_states.to("cpu") if outputs.hidden_states is not None else outputs.hidden_states
        )
        outputs.attentions = (
            outputs.attentions.to("cpu") if outputs.attentions is not None else outputs.attentions
        )

        seg = self.image_processor.post_process_semantic_segmentation(
            outputs, target_sizes=[input_image.size[::-1]]
        )[0]
        color_seg = np.zeros((seg.shape[0], seg.shape[1], 3), dtype=np.uint8)  # height, width, 3

        for label, color in enumerate(ade_palette()):
            color_seg[seg == label, :] = color

        color_seg = color_seg.astype(np.uint8)
        color_seg = aux_resize_image(color_seg, image_resolution)
        color_seg = Image.fromarray(color_seg)

        return color_seg


class NullPreProcessor:
    def __call__(self, input_image, **kwargs):
        return input_image


class BlurPreProcessor:
    def __call__(self, input_image, sigma=5.0, **kwargs):
        import cv2

        input_array = np.array(input_image, dtype=np.uint8)
        input_array = HWC3(input_array)

        dst = cv2.GaussianBlur(input_array, (0, 0), sigma)

        return Image.fromarray(dst)


class TileResamplePreProcessor:
    def resize(self, input_image, resolution):
        import cv2

        H, W, C = input_image.shape
        H = float(H)
        W = float(W)
        k = float(resolution) / min(H, W)
        H *= k
        W *= k
        img = cv2.resize(
            input_image, (int(W), int(H)), interpolation=cv2.INTER_LANCZOS4 if k > 1 else cv2.INTER_AREA
        )
        return img

    def __call__(self, input_image, down_sampling_rate=1.0, **kwargs):
        input_array = np.array(input_image, dtype=np.uint8)
        input_array = HWC3(input_array)

        H, W, C = input_array.shape

        target_res = min(H, W) / down_sampling_rate

        dst = self.resize(input_array, target_res)

        return Image.fromarray(dst)


def create_controlnet_model(type_str):
    if type_str == "controlnet_tile":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11f1e_sd15_tile")
    elif type_str == "controlnet_lineart_anime":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15s2_lineart_anime")
    elif type_str == "controlnet_ip2p":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11e_sd15_ip2p")
    elif type_str == "controlnet_openpose":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_openpose")
    elif type_str == "controlnet_softedge":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_softedge")
    elif type_str == "controlnet_shuffle":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11e_sd15_shuffle")
    elif type_str == "controlnet_depth":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11f1p_sd15_depth")
    elif type_str == "controlnet_canny":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_canny")
    elif type_str == "controlnet_inpaint":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_inpaint")
    elif type_str == "controlnet_lineart":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_lineart")
    elif type_str == "controlnet_mlsd":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_mlsd")
    elif type_str == "controlnet_normalbae":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_normalbae")
    elif type_str == "controlnet_scribble":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_scribble")
    elif type_str == "controlnet_seg":
        return ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15_seg")
    elif type_str == "qr_code_monster_v1":
        return ControlNetModel.from_pretrained("monster-labs/control_v1p_sd15_qrcode_monster")
    elif type_str == "qr_code_monster_v2":
        return ControlNetModel.from_pretrained("monster-labs/control_v1p_sd15_qrcode_monster", subfolder="v2")
    elif type_str == "controlnet_mediapipe_face":
        return ControlNetModel.from_pretrained(
            "CrucibleAI/ControlNetMediaPipeFace", subfolder="diffusion_sd15"
        )
    else:
        raise ValueError(f"unknown controlnet type {type_str}")


default_preprocessor_table = {
    "controlnet_lineart_anime": "lineart_anime",
    "controlnet_openpose": "openpose_full" if onnxruntime_installed == False else "dwpose",
    "controlnet_softedge": "softedge_hedsafe",
    "controlnet_shuffle": "shuffle",
    "controlnet_depth": "depth_midas",
    "controlnet_canny": "canny",
    "controlnet_lineart": "lineart_realistic",
    "controlnet_mlsd": "mlsd",
    "controlnet_normalbae": "normal_bae",
    "controlnet_scribble": "scribble_pidsafe",
    "controlnet_seg": "upernet_seg",
    "controlnet_mediapipe_face": "mediapipe_face",
}


def create_preprocessor_from_name(pre_type):
    if pre_type == "dwpose":
        prepare_dwpose()
        return DWposeDetector()
    elif pre_type == "upernet_seg":
        return SegPreProcessor()
    elif pre_type == "blur":
        return BlurPreProcessor()
    elif pre_type == "tile_resample":
        return TileResamplePreProcessor()
    elif pre_type == "none":
        return NullPreProcessor()
    elif pre_type in MODELS:
        return ControlnetPreProcessor(pre_type)
    else:
        raise ValueError(f"unknown controlnet preprocessor type {pre_type}")


def create_default_preprocessor(type_str):
    if type_str in default_preprocessor_table:
        pre_type = default_preprocessor_table[type_str]
    else:
        pre_type = "none"

    return create_preprocessor_from_name(pre_type)


def get_preprocessor(type_str, device_str, preprocessor_map):
    if type_str not in controlnet_preprocessor:
        if preprocessor_map:
            controlnet_preprocessor[type_str] = create_preprocessor_from_name(preprocessor_map["type"])

        if type_str not in controlnet_preprocessor:
            controlnet_preprocessor[type_str] = create_default_preprocessor(type_str)

        if hasattr(controlnet_preprocessor[type_str], "processor"):
            if hasattr(controlnet_preprocessor[type_str].processor, "to"):
                if device_str:
                    controlnet_preprocessor[type_str].processor.to(device_str)
        elif hasattr(controlnet_preprocessor[type_str], "to"):
            if device_str:
                controlnet_preprocessor[type_str].to(device_str)

    return controlnet_preprocessor[type_str]


def clear_controlnet_preprocessor(type_str=None):
    global controlnet_preprocessor
    if type_str == None:
        for t in controlnet_preprocessor:
            controlnet_preprocessor[t] = None
        controlnet_preprocessor = {}
        torch.cuda.empty_cache()
    else:
        controlnet_preprocessor[type_str] = None
        torch.cuda.empty_cache()


def get_preprocessed_img(type_str, img, use_preprocessor, device_str, preprocessor_map):
    if use_preprocessor:
        param = {}
        if preprocessor_map:
            param = preprocessor_map["param"] if "param" in preprocessor_map else {}
        return get_preprocessor(type_str, device_str, preprocessor_map)(img, **param)
    else:
        return img


def create_pipeline(
    base_model: Union[str, PathLike] = default_base_path,
    model_config: ModelConfig = ...,
    infer_config: InferenceConfig = ...,
    use_xformers: bool = True,
) -> AnimationPipeline:
    """Create an AnimationPipeline from a pretrained model.
    Uses the base_model argument to load or download the pretrained reference pipeline model."""

    # make sure motion_module is a Path and exists
    logger.info("Checking motion module...")
    motion_module = data_dir.joinpath(model_config.motion_module)
    if not (motion_module.exists() and motion_module.is_file()):
        prepare_motion_module()
        if not (motion_module.exists() and motion_module.is_file()):
            # check for safetensors version
            motion_module = motion_module.with_suffix(".safetensors")
            if not (motion_module.exists() and motion_module.is_file()):
                # download from HuggingFace Hub if not found
                ensure_motion_modules()
            if not (motion_module.exists() and motion_module.is_file()):
                # this should never happen, but just in case...
                raise FileNotFoundError(f"Motion module {motion_module} does not exist or is not a file!")

    logger.info("Loading tokenizer...")
    tokenizer: CLIPTokenizer = CLIPTokenizer.from_pretrained(base_model, subfolder="tokenizer")
    logger.info("Loading text encoder...")
    text_encoder: CLIPSkipTextModel = CLIPSkipTextModel.from_pretrained(base_model, subfolder="text_encoder")
    logger.info("Loading VAE...")
    vae: AutoencoderKL = AutoencoderKL.from_pretrained(base_model, subfolder="vae")
    logger.info("Loading UNet...")
    unet: UNet3DConditionModel = UNet3DConditionModel.from_pretrained_2d(
        pretrained_model_path=base_model,
        motion_module_path=motion_module,
        subfolder="unet",
        unet_additional_kwargs=infer_config.unet_additional_kwargs,
    )
    feature_extractor = CLIPImageProcessor.from_pretrained(base_model, subfolder="feature_extractor")

    # set up scheduler
    sched_kwargs = infer_config.noise_scheduler_kwargs
    scheduler = get_scheduler(model_config.scheduler, sched_kwargs)
    logger.info(f'Using scheduler "{model_config.scheduler}" ({scheduler.__class__.__name__})')

    # Load the checkpoint weights into the pipeline
    if model_config.path is not None:
        model_path = data_dir.joinpath(model_config.path)
        logger.info(f"Loading weights from {model_path}")
        if model_path.is_file():
            logger.debug("Loading from single checkpoint file")
            unet_state_dict, tenc_state_dict, vae_state_dict = get_checkpoint_weights(model_path)
        elif model_path.is_dir():
            logger.debug("Loading from Diffusers model directory")
            temp_pipeline = StableDiffusionPipeline.from_pretrained(model_path)
            unet_state_dict, tenc_state_dict, vae_state_dict = (
                temp_pipeline.unet.state_dict(),
                temp_pipeline.text_encoder.state_dict(),
                temp_pipeline.vae.state_dict(),
            )
            del temp_pipeline
        else:
            raise FileNotFoundError(f"model_path {model_path} is not a file or directory")

        # Load into the unet, TE, and VAE
        logger.info("Merging weights into UNet...")
        _, unet_unex = unet.load_state_dict(unet_state_dict, strict=False)
        if len(unet_unex) > 0:
            raise ValueError(f"UNet has unexpected keys: {unet_unex}")
        tenc_missing, _ = text_encoder.load_state_dict(tenc_state_dict, strict=False)
        if len(tenc_missing) > 0:
            raise ValueError(f"TextEncoder has missing keys: {tenc_missing}")
        vae_missing, _ = vae.load_state_dict(vae_state_dict, strict=False)
        if len(vae_missing) > 0:
            raise ValueError(f"VAE has missing keys: {vae_missing}")
    else:
        logger.info("Using base model weights (no checkpoint/LoRA)")

    if model_config.vae_path:
        vae_path = data_dir.joinpath(model_config.vae_path)
        logger.info(f"Loading vae from {vae_path}")

        if vae_path.is_dir():
            vae = AutoencoderKL.from_pretrained(vae_path)
        else:
            tensors = load_tensors(vae_path)
            tensors = convert_ldm_vae_checkpoint(tensors, vae.config)
            vae.load_state_dict(tensors)

    # enable xformers if available
    if use_xformers:
        logger.info("Enabling xformers memory-efficient attention")
        unet.enable_xformers_memory_efficient_attention()

    # lora
    for l in model_config.lora_map:
        lora_path = data_dir.joinpath(l)
        if lora_path.is_file():
            logger.info(f"Loading lora {lora_path}")
            logger.info(f"alpha = {model_config.lora_map[l]}")
            load_safetensors_lora(text_encoder, unet, lora_path, alpha=model_config.lora_map[l])

    # motion lora
    for l in model_config.motion_lora_map:
        lora_path = data_dir.joinpath(l)
        if lora_path.is_file():
            logger.info(f"Loading motion lora {lora_path}")
            logger.info(f"alpha = {model_config.motion_lora_map[l]}")
            load_motion_lora(unet, lora_path, alpha=model_config.motion_lora_map[l])

    logger.info("Creating AnimationPipeline...")
    pipeline = AnimationPipeline(
        vae=vae,
        text_encoder=text_encoder,
        tokenizer=tokenizer,
        unet=unet,
        scheduler=scheduler,
        feature_extractor=feature_extractor,
        controlnet_map=None,
    )

    # Load TI embeddings
    load_text_embeddings(pipeline)

    return pipeline


def load_controlnet_models(
    pipe: AnimationPipeline,
    model_config: ModelConfig = ...,
):
    # controlnet
    controlnet_map = {}
    if model_config.controlnet_map:
        c_image_dir = data_dir.joinpath(model_config.controlnet_map["input_image_dir"])

        for c in model_config.controlnet_map:
            item = model_config.controlnet_map[c]
            if type(item) is dict:
                if item["enable"] == True:
                    img_dir = c_image_dir.joinpath(c)
                    cond_imgs = sorted(glob.glob(os.path.join(img_dir, "[0-9]*.png"), recursive=False))
                    if len(cond_imgs) > 0:
                        logger.info(f"loading {c=} model")
                        controlnet_map[c] = create_controlnet_model(c)

    if not controlnet_map:
        controlnet_map = None

    pipe.controlnet_map = controlnet_map


def unload_controlnet_models(pipe: AnimationPipeline):
    from animatediff.utils.util import show_gpu

    show_gpu("before uload controlnet")
    pipe.controlnet_map = None
    torch.cuda.empty_cache()
    show_gpu("after unload controlnet")


def create_us_pipeline(
    model_config: ModelConfig = ...,
    infer_config: InferenceConfig = ...,
    use_xformers: bool = True,
    use_controlnet_ref: bool = False,
    use_controlnet_tile: bool = False,
    use_controlnet_line_anime: bool = False,
    use_controlnet_ip2p: bool = False,
) -> DiffusionPipeline:
    # set up scheduler
    sched_kwargs = infer_config.noise_scheduler_kwargs
    scheduler = get_scheduler(model_config.scheduler, sched_kwargs)
    logger.info(f'Using scheduler "{model_config.scheduler}" ({scheduler.__class__.__name__})')

    controlnet = []
    if use_controlnet_tile:
        controlnet.append(ControlNetModel.from_pretrained("lllyasviel/control_v11f1e_sd15_tile"))
    if use_controlnet_line_anime:
        controlnet.append(ControlNetModel.from_pretrained("lllyasviel/control_v11p_sd15s2_lineart_anime"))
    if use_controlnet_ip2p:
        controlnet.append(ControlNetModel.from_pretrained("lllyasviel/control_v11e_sd15_ip2p"))

    if len(controlnet) == 1:
        controlnet = controlnet[0]
    elif len(controlnet) == 0:
        controlnet = None

    # Load the checkpoint weights into the pipeline
    pipeline: DiffusionPipeline

    if model_config.path is not None:
        model_path = data_dir.joinpath(model_config.path)
        logger.info(f"Loading weights from {model_path}")
        if model_path.is_file():

            def is_empty_dir(path):
                import os

                return len(os.listdir(path)) == 0

            save_path = data_dir.joinpath(
                "models/huggingface/" + model_path.stem + "_" + str(model_path.stat().st_size)
            )
            save_path.mkdir(exist_ok=True)
            if save_path.is_dir() and is_empty_dir(save_path):
                # StableDiffusionControlNetImg2ImgPipeline.from_single_file does not exist in version 18.2
                logger.debug("Loading from single checkpoint file")
                tmp_pipeline = StableDiffusionPipeline.from_single_file(
                    pretrained_model_link_or_path=str(model_path.absolute())
                )
                tmp_pipeline.save_pretrained(save_path, safe_serialization=True)
                del tmp_pipeline

            if use_controlnet_ref:
                pipeline = StableDiffusionControlNetImg2ImgReferencePipeline.from_pretrained(
                    save_path,
                    controlnet=controlnet,
                    local_files_only=False,
                    load_safety_checker=False,
                    safety_checker=None,
                )
            else:
                pipeline = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
                    save_path,
                    controlnet=controlnet,
                    local_files_only=False,
                    load_safety_checker=False,
                    safety_checker=None,
                )

        elif model_path.is_dir():
            logger.debug("Loading from Diffusers model directory")
            if use_controlnet_ref:
                pipeline = StableDiffusionControlNetImg2ImgReferencePipeline.from_pretrained(
                    model_path,
                    controlnet=controlnet,
                    local_files_only=True,
                    load_safety_checker=False,
                    safety_checker=None,
                )
            else:
                pipeline = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
                    model_path,
                    controlnet=controlnet,
                    local_files_only=True,
                    load_safety_checker=False,
                    safety_checker=None,
                )
        else:
            raise FileNotFoundError(f"model_path {model_path} is not a file or directory")
    else:
        raise ValueError("model_config.path is invalid")

    pipeline.scheduler = scheduler

    # enable xformers if available
    if use_xformers:
        logger.info("Enabling xformers memory-efficient attention")
        pipeline.enable_xformers_memory_efficient_attention()

    # lora
    for l in model_config.lora_map:
        lora_path = data_dir.joinpath(l)
        if lora_path.is_file():
            logger.info(f"Loading lora {lora_path}")
            logger.info(f"alpha = {model_config.lora_map[l]}")
            load_safetensors_lora(
                pipeline.text_encoder,
                pipeline.unet,
                lora_path,
                alpha=model_config.lora_map[l],
                is_animatediff=False,
            )

    # Load TI embeddings
    load_text_embeddings(pipeline)

    return pipeline


def seed_everything(seed):
    import random

    import numpy as np

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed % (2**32))
    random.seed(seed)


def controlnet_preprocess(
    controlnet_map: Dict[str, Any] = None,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    out_dir: PathLike = ...,
    device_str: str = None,
):
    if not controlnet_map:
        return None, None, None

    out_dir = Path(out_dir)  # ensure out_dir is a Path

    # { 0 : { "type_str" : IMAGE, "type_str2" : IMAGE }  }
    controlnet_image_map = {}

    controlnet_type_map = {}

    c_image_dir = data_dir.joinpath(controlnet_map["input_image_dir"])
    save_detectmap = controlnet_map["save_detectmap"] if "save_detectmap" in controlnet_map else True

    preprocess_on_gpu = controlnet_map["preprocess_on_gpu"] if "preprocess_on_gpu" in controlnet_map else True
    device_str = device_str if preprocess_on_gpu else None

    for c in controlnet_map:
        if c == "controlnet_ref":
            continue

        item = controlnet_map[c]

        processed = False

        if type(item) is dict:
            if item["enable"] == True:
                preprocessor_map = item["preprocessor"] if "preprocessor" in item else {}

                img_dir = c_image_dir.joinpath(c)
                cond_imgs = sorted(glob.glob(os.path.join(img_dir, "[0-9]*.png"), recursive=False))
                if len(cond_imgs) > 0:
                    controlnet_type_map[c] = {
                        "controlnet_conditioning_scale": item["controlnet_conditioning_scale"],
                        "control_guidance_start": item["control_guidance_start"],
                        "control_guidance_end": item["control_guidance_end"],
                        "control_scale_list": item["control_scale_list"],
                        "guess_mode": item["guess_mode"] if "guess_mode" in item else False,
                    }

                    use_preprocessor = item["use_preprocessor"] if "use_preprocessor" in item else True

                    for img_path in tqdm(cond_imgs, desc=f"Preprocessing images ({c})"):
                        frame_no = int(Path(img_path).stem)
                        if frame_no < duration:
                            if frame_no not in controlnet_image_map:
                                controlnet_image_map[frame_no] = {}
                            controlnet_image_map[frame_no][c] = get_preprocessed_img(
                                c,
                                get_resized_image2(img_path, 512),
                                use_preprocessor,
                                device_str,
                                preprocessor_map,
                            )
                            processed = True

        if save_detectmap and processed:
            det_dir = out_dir.joinpath(f"{0:02d}_detectmap/{c}")
            det_dir.mkdir(parents=True, exist_ok=True)
            for frame_no in tqdm(controlnet_image_map, desc=f"Saving Preprocessed images ({c})"):
                save_path = det_dir.joinpath(f"{frame_no:08d}.png")
                if c in controlnet_image_map[frame_no]:
                    controlnet_image_map[frame_no][c].save(save_path)

        clear_controlnet_preprocessor(c)

    clear_controlnet_preprocessor()

    controlnet_ref_map = None

    if "controlnet_ref" in controlnet_map:
        r = controlnet_map["controlnet_ref"]
        if r["enable"] == True:
            org_name = data_dir.joinpath(r["ref_image"]).stem
            #            ref_image = get_resized_image( data_dir.joinpath( r["ref_image"] ) , width, height)
            ref_image = get_resized_image2(data_dir.joinpath(r["ref_image"]), 512)

            if ref_image is not None:
                controlnet_ref_map = {
                    "ref_image": ref_image,
                    "style_fidelity": r["style_fidelity"],
                    "attention_auto_machine_weight": r["attention_auto_machine_weight"],
                    "gn_auto_machine_weight": r["gn_auto_machine_weight"],
                    "reference_attn": r["reference_attn"],
                    "reference_adain": r["reference_adain"],
                    "scale_pattern": r["scale_pattern"],
                }

                if save_detectmap:
                    det_dir = out_dir.joinpath(f"{0:02d}_detectmap/controlnet_ref")
                    det_dir.mkdir(parents=True, exist_ok=True)
                    save_path = det_dir.joinpath(f"{org_name}.png")
                    ref_image.save(save_path)

    return controlnet_image_map, controlnet_type_map, controlnet_ref_map


def ip_adapter_preprocess(
    ip_adapter_config_map: Dict[str, Any] = None,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    out_dir: PathLike = ...,
):
    ip_adapter_map = {}

    processed = False

    if ip_adapter_config_map:
        if ip_adapter_config_map["enable"] == True:
            resized_to_square = (
                ip_adapter_config_map["resized_to_square"]
                if "resized_to_square" in ip_adapter_config_map
                else False
            )
            image_dir = data_dir.joinpath(ip_adapter_config_map["input_image_dir"])
            imgs = sorted(glob.glob(os.path.join(image_dir, "[0-9]*.png"), recursive=False))
            if len(imgs) > 0:
                prepare_ip_adapter()
                ip_adapter_map["images"] = {}
                for img_path in tqdm(imgs, desc=f"Preprocessing images (ip_adapter)"):
                    frame_no = int(Path(img_path).stem)
                    if frame_no < duration:
                        if resized_to_square:
                            ip_adapter_map["images"][frame_no] = get_resized_image(img_path, 256, 256)
                        else:
                            ip_adapter_map["images"][frame_no] = get_resized_image2(img_path, 256)
                        processed = True

            if processed:
                ip_adapter_config_map["prompt_fixed_ratio"] = max(
                    min(1.0, ip_adapter_config_map["prompt_fixed_ratio"]), 0
                )

                prompt_fixed_ratio = ip_adapter_config_map["prompt_fixed_ratio"]
                prompt_map = ip_adapter_map["images"]
                prompt_map = dict(sorted(prompt_map.items()))
                key_list = list(prompt_map.keys())
                for k0, k1 in zip(key_list, key_list[1:] + [duration]):
                    k05 = k0 + round((k1 - k0) * prompt_fixed_ratio)
                    if k05 == k1:
                        k05 -= 1
                    if k05 != k0:
                        prompt_map[k05] = prompt_map[k0]
                ip_adapter_map["images"] = prompt_map

            if (ip_adapter_config_map["save_input_image"] == True) and processed:
                det_dir = out_dir.joinpath(f"{0:02d}_ip_adapter/")
                det_dir.mkdir(parents=True, exist_ok=True)
                for frame_no in tqdm(
                    ip_adapter_map["images"], desc=f"Saving Preprocessed images (ip_adapter)"
                ):
                    save_path = det_dir.joinpath(f"{frame_no:08d}.png")
                    ip_adapter_map["images"][frame_no].save(save_path)

    return ip_adapter_map if processed else None


def prompt_preprocess(
    prompt_config_map: Dict[str, Any],
    head_prompt: str,
    tail_prompt: str,
    prompt_fixed_ratio: float,
    video_length: int,
):
    prompt_map = {}
    for k in prompt_config_map.keys():
        if int(k) < video_length:
            pr = prompt_config_map[k]
            if head_prompt:
                pr = head_prompt + "," + pr
            if tail_prompt:
                pr = pr + "," + tail_prompt

            prompt_map[int(k)] = pr

    prompt_map = dict(sorted(prompt_map.items()))
    key_list = list(prompt_map.keys())
    for k0, k1 in zip(key_list, key_list[1:] + [video_length]):
        k05 = k0 + round((k1 - k0) * prompt_fixed_ratio)
        if k05 == k1:
            k05 -= 1
        if k05 != k0:
            prompt_map[k05] = prompt_map[k0]

    return prompt_map


def region_preprocess(
    model_config: ModelConfig = ...,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    out_dir: PathLike = ...,
    is_init_img_exist: bool = False,
):
    is_bg_init_img = False
    if is_init_img_exist:
        if model_config.region_map:
            if "background" in model_config.region_map:
                is_bg_init_img = model_config.region_map["background"]["is_init_img"]

    region_condi_list = []

    condi_index = 0

    prev_ip_map = None

    if not is_bg_init_img:
        ip_map = ip_adapter_preprocess(model_config.ip_adapter_map, width, height, duration, out_dir)

        if ip_map:
            prev_ip_map = ip_map

        condition_map = {
            "prompt_map": prompt_preprocess(
                model_config.prompt_map,
                model_config.head_prompt,
                model_config.tail_prompt,
                model_config.prompt_fixed_ratio,
                duration,
            ),
            "ip_adapter_map": ip_map,
        }

        region_condi_list.append(condition_map)

        bg_src = condi_index
        condi_index += 1
    else:
        bg_src = -1

    region_list = [{"mask_images": None, "src": bg_src}]

    if model_config.region_map:
        for r in model_config.region_map:
            if r == "background":
                continue
            if model_config.region_map[r]["enable"] != True:
                continue
            region_dir = out_dir.joinpath(f"region_{int(r):05d}/")
            region_dir.mkdir(parents=True, exist_ok=True)

            mask_map = mask_preprocess(model_config.region_map[r], width, height, duration, region_dir)

            if not mask_map:
                continue

            if model_config.region_map[r]["is_init_img"] == False:
                ip_map = ip_adapter_preprocess(
                    model_config.region_map[r]["condition"]["ip_adapter_map"],
                    width,
                    height,
                    duration,
                    region_dir,
                )

                if ip_map:
                    prev_ip_map = ip_map

                condition_map = {
                    "prompt_map": prompt_preprocess(
                        model_config.region_map[r]["condition"]["prompt_map"],
                        model_config.region_map[r]["condition"]["head_prompt"],
                        model_config.region_map[r]["condition"]["tail_prompt"],
                        model_config.region_map[r]["condition"]["prompt_fixed_ratio"],
                        duration,
                    ),
                    "ip_adapter_map": ip_map,
                }

                region_condi_list.append(condition_map)

                src = condi_index
                condi_index += 1
            else:
                if is_init_img_exist == False:
                    continue
                src = -1

            region_list.append({"mask_images": mask_map, "src": src})

    ip_adapter_config_map = None

    if prev_ip_map is not None:
        ip_adapter_config_map = {}
        ip_adapter_config_map["scale"] = model_config.ip_adapter_map["scale"]
        ip_adapter_config_map["is_plus"] = model_config.ip_adapter_map["is_plus"]
        ip_adapter_config_map["is_plus_face"] = (
            model_config.ip_adapter_map["is_plus_face"]
            if "is_plus_face" in model_config.ip_adapter_map
            else False
        )
        ip_adapter_config_map["is_light"] = (
            model_config.ip_adapter_map["is_light"] if "is_light" in model_config.ip_adapter_map else False
        )
        for c in region_condi_list:
            if c["ip_adapter_map"] == None:
                logger.info(f"fill map")
                c["ip_adapter_map"] = prev_ip_map

    # for c in region_condi_list:
    #    logger.info(f"{c['prompt_map']=}")

    if not region_condi_list:
        raise ValueError("erro! There is not a single valid region")

    return region_condi_list, region_list, ip_adapter_config_map


def img2img_preprocess(
    img2img_config_map: Dict[str, Any] = None,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    out_dir: PathLike = ...,
):
    img2img_map = {}

    processed = False

    if img2img_config_map:
        if img2img_config_map["enable"] == True:
            image_dir = data_dir.joinpath(img2img_config_map["init_img_dir"])
            imgs = sorted(glob.glob(os.path.join(image_dir, "[0-9]*.png"), recursive=False))
            if len(imgs) > 0:
                img2img_map["images"] = {}
                img2img_map["denoising_strength"] = img2img_config_map["denoising_strength"]
                for img_path in tqdm(imgs, desc=f"Preprocessing images (img2img)"):
                    frame_no = int(Path(img_path).stem)
                    if frame_no < duration:
                        img2img_map["images"][frame_no] = get_resized_image(img_path, width, height)
                        processed = True

            if (img2img_config_map["save_init_image"] == True) and processed:
                det_dir = out_dir.joinpath(f"{0:02d}_img2img_init_img/")
                det_dir.mkdir(parents=True, exist_ok=True)
                for frame_no in tqdm(img2img_map["images"], desc=f"Saving Preprocessed images (img2img)"):
                    save_path = det_dir.joinpath(f"{frame_no:08d}.png")
                    img2img_map["images"][frame_no].save(save_path)

    return img2img_map if processed else None


def mask_preprocess(
    region_config_map: Dict[str, Any] = None,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    out_dir: PathLike = ...,
):
    mask_map = {}

    processed = False
    size = None
    mode = None

    if region_config_map:
        image_dir = data_dir.joinpath(region_config_map["mask_dir"])
        imgs = sorted(glob.glob(os.path.join(image_dir, "[0-9]*.png"), recursive=False))
        if len(imgs) > 0:
            for img_path in tqdm(imgs, desc=f"Preprocessing images (mask)"):
                frame_no = int(Path(img_path).stem)
                if frame_no < duration:
                    mask_map[frame_no] = get_resized_image(img_path, width, height)
                    if size is None:
                        size = mask_map[frame_no].size
                        mode = mask_map[frame_no].mode

                    processed = True

        if processed:
            if 0 in mask_map:
                prev_img = mask_map[0]
            else:
                prev_img = Image.new(mode, size, color=0)

            for i in range(duration):
                if i in mask_map:
                    prev_img = mask_map[i]
                else:
                    mask_map[i] = prev_img

        if (region_config_map["save_mask"] == True) and processed:
            det_dir = out_dir.joinpath(f"mask/")
            det_dir.mkdir(parents=True, exist_ok=True)
            for frame_no in tqdm(mask_map, desc=f"Saving Preprocessed images (mask)"):
                save_path = det_dir.joinpath(f"{frame_no:08d}.png")
                mask_map[frame_no].save(save_path)

    return mask_map if processed else None


def wild_card_conversion(
    model_config: ModelConfig = ...,
):
    from animatediff.utils.wild_card import replace_wild_card

    wild_card_dir = get_dir("wildcards")
    for k in model_config.prompt_map.keys():
        model_config.prompt_map[k] = replace_wild_card(model_config.prompt_map[k], wild_card_dir)

    if model_config.head_prompt:
        model_config.head_prompt = replace_wild_card(model_config.head_prompt, wild_card_dir)
    if model_config.tail_prompt:
        model_config.tail_prompt = replace_wild_card(model_config.tail_prompt, wild_card_dir)

    model_config.prompt_fixed_ratio = max(min(1.0, model_config.prompt_fixed_ratio), 0)

    if model_config.region_map:
        for r in model_config.region_map:
            if r == "background":
                continue

            if "condition" in model_config.region_map[r]:
                c = model_config.region_map[r]["condition"]
                for k in c["prompt_map"].keys():
                    c["prompt_map"][k] = replace_wild_card(c["prompt_map"][k], wild_card_dir)

                if "head_prompt" in c:
                    c["head_prompt"] = replace_wild_card(c["head_prompt"], wild_card_dir)
                if "tail_prompt" in c:
                    c["tail_prompt"] = replace_wild_card(c["tail_prompt"], wild_card_dir)
                if "prompt_fixed_ratio" in c:
                    c["prompt_fixed_ratio"] = max(min(1.0, c["prompt_fixed_ratio"]), 0)


def save_output(
    pipeline_output,
    frame_dir: str,
    out_file: str,
    output_map: Dict[str, Any] = {},
    no_frames: bool = False,
    save_frames=save_frames,
    save_video=None,
):
    output_format = "gif"
    output_fps = 8
    if output_map:
        output_format = output_map["format"] if "format" in output_map else output_format
        output_fps = output_map["fps"] if "fps" in output_map else output_fps
        if output_format == "mp4":
            output_format = "h264"

    if output_format == "gif":
        out_file = out_file.with_suffix(".gif")
        if no_frames is not True:
            if save_frames:
                save_frames(pipeline_output, frame_dir)

            # generate the output filename and save the video
            if save_video:
                save_video(pipeline_output, out_file, output_fps)
            else:
                pipeline_output[0].save(
                    fp=out_file,
                    format="GIF",
                    append_images=pipeline_output[1:],
                    save_all=True,
                    duration=(1 / output_fps * 1000),
                    loop=0,
                )

    else:
        if save_frames:
            save_frames(pipeline_output, frame_dir)

        from animatediff.rife.ffmpeg import FfmpegEncoder, VideoCodec, codec_extn

        out_file = out_file.with_suffix(f".{codec_extn(output_format)}")

        logger.info("Creating ffmpeg encoder...")
        encoder = FfmpegEncoder(
            frames_dir=frame_dir,
            out_file=out_file,
            codec=output_format,
            in_fps=output_fps,
            out_fps=output_fps,
            lossless=False,
            param=output_map["encode_param"] if "encode_param" in output_map else {},
        )
        logger.info("Encoding interpolated frames with ffmpeg...")
        result = encoder.encode()
        logger.debug(f"ffmpeg result: {result}")


def run_inference(
    pipeline: AnimationPipeline,
    n_prompt: str = ...,
    seed: int = -1,
    steps: int = 25,
    guidance_scale: float = 7.5,
    unet_batch_size: int = 1,
    width: int = 512,
    height: int = 512,
    duration: int = 16,
    idx: int = 0,
    out_dir: PathLike = ...,
    context_frames: int = -1,
    context_stride: int = 3,
    context_overlap: int = 4,
    context_schedule: str = "uniform",
    clip_skip: int = 1,
    controlnet_map: Dict[str, Any] = None,
    controlnet_image_map: Dict[str, Any] = None,
    controlnet_type_map: Dict[str, Any] = None,
    controlnet_ref_map: Dict[str, Any] = None,
    no_frames: bool = False,
    img2img_map: Dict[str, Any] = None,
    ip_adapter_config_map: Dict[str, Any] = None,
    region_list: List[Any] = None,
    region_condi_list: List[Any] = None,
    output_map: Dict[str, Any] = None,
    is_single_prompt_mode: bool = False,
):
    out_dir = Path(out_dir)  # ensure out_dir is a Path

    seed_everything(seed)

    logger.info(f"{len( region_condi_list )=}")
    #    logger.info(f"{region_condi_list=}")
    logger.info(f"{len( region_list )=}")
    #    logger.info(f"{region_list=}")

    # print(
    #     f"controlnet_image_map={controlnet_image_map}|controlnet_type_map={controlnet_type_map}",
    #     f"controlnet_ref_map={controlnet_ref_map}",
    #     f"img2img_map={img2img_map}",
    #     f"ip_adapter_config_map={ip_adapter_config_map}",
    #     f"region_list={region_list}",
    #     f"region_condi_list={region_condi_list}",
    # )

    # controlnet_image_map={0: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4AF0>}, 1: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4F40>}, 2: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4F10>}, 3: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D45B0>}, 4: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D42B0>}, 5: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4B50>}, 6: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4C40>}, 7: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D5480>}, 8: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4D90>}, 9: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4FA0>}, 10: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4610>}, 11: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4460>}, 12: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4EB0>}, 13: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D59F0>}, 14: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D44F0>}, 15: {'controlnet_scribble': <PIL.Image.Image image mode=RGB size=512x512 at 0x7F4DF56D4130>}}

    # controlnet_type_map={'controlnet_scribble': {'controlnet_conditioning_scale': 1.0, 'control_guidance_start': 0.0, 'control_guidance_end': 1.0, 'control_scale_list': [], 'guess_mode': False}}

    # region_list=[{'mask_images': None, 'src': 0}]
    # region_condi_list=[{'prompt_map': {0: 'masterpiece, best quality, ((boots)), ((no_humans)),(simple_background:0.79), ((no_girls)), ((no_legs)), ((no_hands)), ((no heads)),', 8: 'masterpiece, best quality, ((boots)), ((no_humans)),(simple_background:0.79), ((no_girls)), ((no_legs)), ((no_hands)), ((no heads)),'}, 'ip_adapter_map': None}]

    # 先不管 img2img_map 和 ip_adapter_config_map 这样的实现了

    def segment_dict(dictionary, segment_size=16, overlap=15):
        """
        分段给定字典，每段大小为segment_size，并保证段与段之间至少有一个元素重叠。
        当字典的键值对数量是segment_size的倍数时，也要保证重叠。

        Args:
        dictionary (dict): 输入的字典，其中键值对表示元素。
        segment_size (int): 每段的元素数量。
        overlap (int): 相邻段之间的重叠元素数量。

        Returns:
        tuple: 包含分段的列表和重叠键列表的元组，每个段以字典的形式表示。
        """
        segments = []
        overlaps = []
        keys = list(dictionary.keys())

        if len(keys) < segment_size:
            raise ValueError("Dictionary must contain at least as many keys as segment_size")

        if len(keys) == segment_size:
            return [dictionary], []

        previous_segment_keys = []
        start = 0
        while start < len(keys):
            # 计算下一段的结束位置，确保段与段之间有重叠
            next_start = start + segment_size - overlap

            # 如果下一段的开始位置超出字典长度，调整开始位置使得最后一段满足segment_size
            if len(keys) - start < segment_size:
                start = len(keys) - segment_size

            # 构建当前段的字典
            current_segment = {}
            current_overlap_keys = []
            for i in range(start, start + segment_size):
                key = keys[i]
                current_segment[key] = dictionary[key]
                # 如果当前键在上一段中出现过，则将其添加到重叠键列表中
                if key in previous_segment_keys:
                    current_overlap_keys.append(key)

            segments.append(current_segment)
            overlaps.append(current_overlap_keys)
            previous_segment_keys = keys[start : start + segment_size]

            if (start + segment_size) >= len(keys):
                break
            start = next_start

        return segments, overlaps

    # 简便起见， 直接把 region_condi_list 的 key都做一个 copy and paste
    latents_cache = {}

    controlnet_image_maps, overlaps = segment_dict(
        controlnet_image_map, segment_size=context_frames, overlap=15
    )
    print(
        "overlaps=",
        overlaps,
        # "|controlnet_image_maps=", controlnet_image_maps
    )

    def example_callback(idx, iteration, t, latents, debug=False):
        # 确保 idx 存在于 latents_cache
        if idx not in latents_cache:
            latents_cache[idx] = {}
        latents_cache[idx][iteration] = latents
        if debug:
            temp_video = pipeline.decode_latents(latents)

            # 看代码 video 是 numpy 类型，先转一下
            temp_video_res = torch.from_numpy(temp_video)
            output_directory = f"./output_tmp2/{idx}_{iteration}_{t}"
            save_frames(temp_video_res, output_directory, False)

        if idx > 0:
            for i, _ in enumerate(overlaps[idx]):
                last_latents = latents_cache[idx - 1][iteration]
                last_idx = latents.shape[2] - len(overlaps[idx]) + i
                latents[:, :, i, :, :] = last_latents[:, :, last_idx, :, :]

    def transform_region_condi_list(region_condi_list):
        transformed_list = []

        for region_dict in region_condi_list:
            if "prompt_map" in region_dict and isinstance(region_dict["prompt_map"], dict):
                new_region_dict = {"prompt_map": {}}
                prompt_map = region_dict["prompt_map"]

                # 从1到15的键中复制值
                for i in range(1, 16):
                    if 0 in prompt_map:
                        new_region_dict["prompt_map"][i] = prompt_map[0]
                    else:
                        new_region_dict["prompt_map"][i] = None

                # 复制其他键的值
                for key, value in region_dict.items():
                    if key != "prompt_map":
                        new_region_dict[key] = value

                # 添加到转换后的列表
                transformed_list.append(new_region_dict)
            else:
                # 如果没有有效的 'prompt_map'，直接复制当前字典
                transformed_list.append(region_dict)

        return transformed_list

    # context_frames 默认为16
    pipeline_outputs = []

    for i, c in enumerate(controlnet_image_maps):
        new_region_condi_list = transform_region_condi_list(region_condi_list)
        example_callback_with_i = partial(example_callback, i)

        pipeline_output = pipeline(
            negative_prompt=n_prompt,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            unet_batch_size=unet_batch_size,
            width=width,
            height=height,
            video_length=context_frames,
            return_dict=False,
            context_frames=context_frames,
            context_stride=context_stride + 1,
            context_overlap=context_overlap,
            context_schedule=context_schedule,
            clip_skip=clip_skip,
            controlnet_type_map=controlnet_type_map,
            controlnet_image_map=c,
            controlnet_ref_map=controlnet_ref_map,
            controlnet_max_samples_on_vram=controlnet_map["max_samples_on_vram"]
            if "max_samples_on_vram" in controlnet_map
            else 999,
            controlnet_max_models_on_vram=controlnet_map["max_models_on_vram"]
            if "max_models_on_vram" in controlnet_map
            else 99,
            controlnet_is_loop=controlnet_map["is_loop"] if "is_loop" in controlnet_map else True,
            img2img_map=img2img_map,
            ip_adapter_config_map=ip_adapter_config_map,
            region_list=region_list,
            region_condi_list=new_region_condi_list,
            interpolation_factor=1,
            is_single_prompt_mode=is_single_prompt_mode,
            callback=example_callback_with_i,
        )
        # print("pipeline_output=", pipeline_output)
        pipeline_outputs.append(pipeline_output)

    def combine_videos(videos, overlaps):
        """
        合并多个视频。

        Args:
        videos (list): 包含多个视频的列表，每个视频可以是PyTorch张量或NumPy数组。

        Returns:
        Union[torch.Tensor, np.ndarray]: 合并后的视频，数据类型与输入视频相同。
        """
        # 先不考虑重复帧，先简单处理一下

        # 确定输入视频的数据类型
        if isinstance(videos[0], torch.Tensor):
            for i, video in enumerate(videos):
                if i == 0:
                    combined_video = video
                else:
                    slice_length = len(overlaps[i])
                    video_sliced = video[:, :, slice_length:, :, :]

                    combined_video = torch.cat((combined_video, video_sliced), dim=2)

            # combined_video = torch.cat(videos, dim=2)
        elif isinstance(videos[0], np.ndarray):
            combined_video = np.concatenate(videos, axis=2)
        else:
            raise ValueError("Unsupported data type for videos")

        return combined_video

    # print("pipeline_outputs=", pipeline_outputs)
    pipeline_output = combine_videos(pipeline_outputs, overlaps)
    # pipeline_output = AnimationPipelineOutput(videos=combined_videos_res)

    logger.info("Generation complete, saving...")

    prompt_map = region_condi_list[0]["prompt_map"]

    # Trim and clean up the prompt for filename use
    prompt_tags = [
        re_clean_prompt.sub("", tag).strip().replace(" ", "-")
        for tag in prompt_map[list(prompt_map.keys())[0]].split(",")
    ]
    prompt_str = "_".join((prompt_tags[:6]))[:50]

    frame_dir = out_dir.joinpath(f"{idx:02d}-{seed}")
    # print("will save to frame_dir:", frame_dir, "|save_frames=", save_frames)
    out_file = out_dir.joinpath(f"{idx:02d}_{seed}_{prompt_str}")

    save_output(pipeline_output, frame_dir, out_file, output_map, no_frames, save_frames, save_video)

    logger.info(f"Saved sample to {out_file}")
    return pipeline_output


def run_upscale(
    org_imgs: List[str],
    pipeline: DiffusionPipeline,
    prompt_map: Dict[int, str] = None,
    n_prompt: str = ...,
    seed: int = -1,
    steps: int = 25,
    strength: float = 0.5,
    guidance_scale: float = 7.5,
    clip_skip: int = 1,
    us_width: int = 512,
    us_height: int = 512,
    idx: int = 0,
    out_dir: PathLike = ...,
    upscale_config: Dict[str, Any] = None,
    use_controlnet_ref: bool = False,
    use_controlnet_tile: bool = False,
    use_controlnet_line_anime: bool = False,
    use_controlnet_ip2p: bool = False,
    no_frames: bool = False,
    output_map: Dict[str, Any] = None,
):
    from animatediff.utils.lpw_stable_diffusion import lpw_encode_prompt

    pipeline.set_progress_bar_config(disable=True)

    images = get_resized_images(org_imgs, us_width, us_height)

    steps = steps if "steps" not in upscale_config else upscale_config["steps"]
    scheduler = scheduler if "scheduler" not in upscale_config else upscale_config["scheduler"]
    guidance_scale = (
        guidance_scale if "guidance_scale" not in upscale_config else upscale_config["guidance_scale"]
    )
    clip_skip = clip_skip if "clip_skip" not in upscale_config else upscale_config["clip_skip"]
    strength = strength if "strength" not in upscale_config else upscale_config["strength"]

    controlnet_conditioning_scale = []
    guess_mode = []
    control_guidance_start = []
    control_guidance_end = []

    # for controlnet tile
    if use_controlnet_tile:
        controlnet_conditioning_scale.append(
            upscale_config["controlnet_tile"]["controlnet_conditioning_scale"]
        )
        guess_mode.append(upscale_config["controlnet_tile"]["guess_mode"])
        control_guidance_start.append(upscale_config["controlnet_tile"]["control_guidance_start"])
        control_guidance_end.append(upscale_config["controlnet_tile"]["control_guidance_end"])

    # for controlnet line_anime
    if use_controlnet_line_anime:
        controlnet_conditioning_scale.append(
            upscale_config["controlnet_line_anime"]["controlnet_conditioning_scale"]
        )
        guess_mode.append(upscale_config["controlnet_line_anime"]["guess_mode"])
        control_guidance_start.append(upscale_config["controlnet_line_anime"]["control_guidance_start"])
        control_guidance_end.append(upscale_config["controlnet_line_anime"]["control_guidance_end"])

    # for controlnet ip2p
    if use_controlnet_ip2p:
        controlnet_conditioning_scale.append(
            upscale_config["controlnet_ip2p"]["controlnet_conditioning_scale"]
        )
        guess_mode.append(upscale_config["controlnet_ip2p"]["guess_mode"])
        control_guidance_start.append(upscale_config["controlnet_ip2p"]["control_guidance_start"])
        control_guidance_end.append(upscale_config["controlnet_ip2p"]["control_guidance_end"])

    # for controlnet ref
    ref_image = None
    if use_controlnet_ref:
        if (
            not upscale_config["controlnet_ref"]["use_frame_as_ref_image"]
            and not upscale_config["controlnet_ref"]["use_1st_frame_as_ref_image"]
        ):
            ref_image = get_resized_images(
                [data_dir.joinpath(upscale_config["controlnet_ref"]["ref_image"])], us_width, us_height
            )[0]

    generator = torch.manual_seed(seed)

    seed_everything(seed)

    prompt_embeds_map = {}
    prompt_map = dict(sorted(prompt_map.items()))
    negative = None

    do_classifier_free_guidance = guidance_scale > 1.0

    prompt_list = [prompt_map[key_frame] for key_frame in prompt_map.keys()]

    prompt_embeds, neg_embeds = lpw_encode_prompt(
        pipe=pipeline,
        prompt=prompt_list,
        do_classifier_free_guidance=do_classifier_free_guidance,
        negative_prompt=n_prompt,
    )

    if do_classifier_free_guidance:
        negative = neg_embeds.chunk(neg_embeds.shape[0], 0)
        positive = prompt_embeds.chunk(prompt_embeds.shape[0], 0)
    else:
        negative = [None]
        positive = prompt_embeds.chunk(prompt_embeds.shape[0], 0)

    for i, key_frame in enumerate(prompt_map):
        prompt_embeds_map[key_frame] = positive[i]

    key_first = list(prompt_map.keys())[0]
    key_last = list(prompt_map.keys())[-1]

    def get_current_prompt_embeds(center_frame: int = 0, video_length: int = 0):
        key_prev = key_last
        key_next = key_first

        for p in prompt_map.keys():
            if p > center_frame:
                key_next = p
                break
            key_prev = p

        dist_prev = center_frame - key_prev
        if dist_prev < 0:
            dist_prev += video_length
        dist_next = key_next - center_frame
        if dist_next < 0:
            dist_next += video_length

        if key_prev == key_next or dist_prev + dist_next == 0:
            return prompt_embeds_map[key_prev]

        rate = dist_prev / (dist_prev + dist_next)

        return get_tensor_interpolation_method()(
            prompt_embeds_map[key_prev], prompt_embeds_map[key_next], rate
        )

    line_anime_processor = LineartAnimeDetector.from_pretrained("lllyasviel/Annotators")

    out_images = []

    logger.info(f"{use_controlnet_tile=}")
    logger.info(f"{use_controlnet_line_anime=}")
    logger.info(f"{use_controlnet_ip2p=}")

    logger.info(f"{controlnet_conditioning_scale=}")
    logger.info(f"{guess_mode=}")
    logger.info(f"{control_guidance_start=}")
    logger.info(f"{control_guidance_end=}")

    for i, org_image in enumerate(tqdm(images, desc=f"Upscaling...")):
        cur_positive = get_current_prompt_embeds(i, len(images))

        #        logger.info(f"w {condition_image.size[0]}")
        #        logger.info(f"h {condition_image.size[1]}")
        condition_image = []

        if use_controlnet_tile:
            condition_image.append(org_image)
        if use_controlnet_line_anime:
            condition_image.append(line_anime_processor(org_image))
        if use_controlnet_ip2p:
            condition_image.append(org_image)

        if not use_controlnet_ref:
            out_image = pipeline(
                prompt_embeds=cur_positive,
                negative_prompt_embeds=negative[0],
                image=org_image,
                control_image=condition_image,
                width=org_image.size[0],
                height=org_image.size[1],
                strength=strength,
                num_inference_steps=steps,
                guidance_scale=guidance_scale,
                generator=generator,
                controlnet_conditioning_scale=controlnet_conditioning_scale
                if len(controlnet_conditioning_scale) > 1
                else controlnet_conditioning_scale[0],
                guess_mode=guess_mode[0],
                control_guidance_start=control_guidance_start
                if len(control_guidance_start) > 1
                else control_guidance_start[0],
                control_guidance_end=control_guidance_end
                if len(control_guidance_end) > 1
                else control_guidance_end[0],
            ).images[0]
        else:
            if upscale_config["controlnet_ref"]["use_1st_frame_as_ref_image"]:
                if i == 0:
                    ref_image = org_image
            elif upscale_config["controlnet_ref"]["use_frame_as_ref_image"]:
                ref_image = org_image

            out_image = pipeline(
                prompt_embeds=cur_positive,
                negative_prompt_embeds=negative[0],
                image=org_image,
                control_image=condition_image,
                width=org_image.size[0],
                height=org_image.size[1],
                strength=strength,
                num_inference_steps=steps,
                guidance_scale=guidance_scale,
                generator=generator,
                controlnet_conditioning_scale=controlnet_conditioning_scale
                if len(controlnet_conditioning_scale) > 1
                else controlnet_conditioning_scale[0],
                guess_mode=guess_mode[0],
                # control_guidance_start= control_guidance_start,
                # control_guidance_end= control_guidance_end,
                ### for controlnet ref
                ref_image=ref_image,
                attention_auto_machine_weight=upscale_config["controlnet_ref"][
                    "attention_auto_machine_weight"
                ],
                gn_auto_machine_weight=upscale_config["controlnet_ref"]["gn_auto_machine_weight"],
                style_fidelity=upscale_config["controlnet_ref"]["style_fidelity"],
                reference_attn=upscale_config["controlnet_ref"]["reference_attn"],
                reference_adain=upscale_config["controlnet_ref"]["reference_adain"],
            ).images[0]

        out_images.append(out_image)

    # Trim and clean up the prompt for filename use
    prompt_tags = [
        re_clean_prompt.sub("", tag).strip().replace(" ", "-")
        for tag in prompt_map[list(prompt_map.keys())[0]].split(",")
    ]
    prompt_str = "_".join((prompt_tags[:6]))[:50]

    # generate the output filename and save the video
    out_file = out_dir.joinpath(f"{idx:02d}_{seed}_{prompt_str}")

    frame_dir = out_dir.joinpath(f"{idx:02d}-{seed}-upscaled")

    save_output(out_images, frame_dir, out_file, output_map, no_frames, save_imgs, None)

    logger.info(f"Saved sample to {out_file}")

    return out_images
