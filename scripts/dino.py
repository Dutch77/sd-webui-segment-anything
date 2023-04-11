import os
import gc
import torch
import matplotlib.pyplot as plt
from collections import OrderedDict
from huggingface_hub import hf_hub_download

from modules import scripts, shared
from modules.devices import device, torch_gc, cpu

# Grounding DINO
import GroundingDINO.groundingdino.datasets.transforms as T
from GroundingDINO.groundingdino.models import build_model
from GroundingDINO.groundingdino.util.slconfig import SLConfig
from GroundingDINO.groundingdino.util.utils import clean_state_dict


dino_model_cache = OrderedDict()
dino_model_dir = os.path.join(scripts.basedir(), "models/grounding-dino")
dino_model_list = [
    "GroundingDINO_SwinT_OGC (694MB)", "GroundingDINO_SwinB (938MB)"]
dino_model_info = {
    "repo_id": "ShilongLiu/GroundingDINO",
    "GroundingDINO_SwinT_OGC (694MB)": {
        "checkpoint": "groundingdino_swint_ogc.pth",
        "config": os.path.join(scripts.basedir(), "GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py")
    },
    "GroundingDINO_SwinB (938MB)": {
        "checkpoint": "groundingdino_swinb_cogcoor.pth",
        "config": os.path.join(scripts.basedir(), "GroundingDINO/groundingdino/config/GroundingDINO_SwinB.cfg.py")
    },
}


def clear_dino_cache():
    dino_model_cache.clear()
    gc.collect()
    torch_gc()


def load_dino_model(dino_checkpoint):
    print(f"Initializing GroundingDINO {dino_checkpoint}")
    dino_checkpoint_path = os.path.join(
        dino_model_dir, dino_model_info[dino_checkpoint]["checkpoint"])
    if dino_checkpoint in dino_model_cache:
        dino = dino_model_cache[dino_checkpoint]
        if shared.cmd_opts.lowvram:
            dino.to(device=device)
    else:
        clear_dino_cache()
        if not os.path.isfile(dino_checkpoint_path):
            print(f"Downloading {dino_checkpoint} from huggingface")
            hf_hub_download(repo_id=dino_model_info["repo_id"],
                            filename=dino_model_info[dino_checkpoint]["checkpoint"],
                            cache_dir=dino_model_dir)
        args = SLConfig.fromfile(dino_model_info[dino_checkpoint])
        dino = build_model(args)
        checkpoint = torch.load(dino_checkpoint_path)
        dino.load_state_dict(clean_state_dict(
            checkpoint['model']), strict=False)
        dino.to(device=device)
        dino_model_cache[dino_checkpoint] = dino
    dino.eval()
    return dino


def load_dino_image(image_pil):
    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image, _ = transform(image_pil, None)  # 3, h, w
    return image


def get_grounding_output(model, image, caption, box_threshold):
    caption = caption.lower()
    caption = caption.strip()
    if not caption.endswith("."):
        caption = caption + "."
    image = image.to(device)
    with torch.no_grad():
        outputs = model(image[None], captions=[caption])
    if shared.cmd_opts.lowvram:
        model.to(cpu)
    logits = outputs["pred_logits"].sigmoid()[0]  # (nq, 256)
    boxes = outputs["pred_boxes"][0]  # (nq, 4)

    # filter output
    logits_filt = logits.clone()
    boxes_filt = boxes.clone()
    filt_mask = logits_filt.max(dim=1)[0] > box_threshold
    logits_filt = logits_filt[filt_mask]  # num_filt, 256
    boxes_filt = boxes_filt[filt_mask]  # num_filt, 4

    return boxes_filt


def show_box(box, ax, label):
    x0, y0 = box[0], box[1]
    w, h = box[2] - box[0], box[3] - box[1]
    ax.add_patch(plt.Rectangle((x0, y0), w, h, edgecolor='green',
                 facecolor=(0, 0, 0, 0), lw=2))
    ax.text(x0, y0, label)


def dino_predict(input_image, dino_model_name, text_prompt, box_threshold):
    print("Running GroundingDINO Inference")
    dino_image = load_dino_image(input_image.convert("RGB"))
    dino_model = load_dino_model(dino_model_name)

    boxes_filt = get_grounding_output(
        dino_model, dino_image, text_prompt, box_threshold
    )

    H, W = input_image.size[1], input_image.size[0]
    for i in range(boxes_filt.size(0)):
        boxes_filt[i] = boxes_filt[i] * torch.Tensor([W, H, W, H])
        boxes_filt[i][:2] -= boxes_filt[i][2:] / 2
        boxes_filt[i][2:] += boxes_filt[i][:2]
    gc.collect()
    torch_gc()
    return boxes_filt.numpy()
