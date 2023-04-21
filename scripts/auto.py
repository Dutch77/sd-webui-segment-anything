import os
import gc
from PIL import Image
from collections import OrderedDict
import numpy as np
import torch
from segment_anything import SamAutomaticMaskGenerator
from modules import scripts, shared
from modules.paths import extensions_dir
from modules.devices import torch_gc


global_sam = None
sem_seg_cache = OrderedDict()
original_uniformer_inference_segmentor = None
original_oneformer_draw_sem_seg = None


def strengthen_sem_seg(class_ids, img):
    import pycocotools.mask as maskUtils
    semantc_mask = class_ids.clone()
    annotations = global_sam(img)
    annotations = sorted(annotations, key=lambda x: x['area'], reverse=True)
    for ann in annotations:
        valid_mask = torch.tensor(maskUtils.decode(ann['segmentation'])).bool()
        propose_classes_ids = class_ids[valid_mask]
        num_class_proposals = len(torch.unique(propose_classes_ids))
        if num_class_proposals == 1:
            semantc_mask[valid_mask] = propose_classes_ids[0]
            continue
        top_1_propose_class_ids = torch.bincount(propose_classes_ids.flatten()).topk(1).indices
        semantc_mask[valid_mask] = top_1_propose_class_ids
    return semantc_mask


def random_segmentation(img):
    img_np = np.array(img)
    annotations = global_sam(img_np)
    annotations = sorted(annotations, key=lambda x: x['area'], reverse=True)
    if len(annotations) == 0:
        return []
    H, W, C = img_np.shape
    cnet_input = np.zeros((H, W), dtype=np.uint16)
    for idx, annotation in enumerate(annotations):
        current_seg = annotation['segmentation']
        cnet_input[current_seg] = idx + 1
    detected_map = np.zeros((cnet_input.shape[0], cnet_input.shape[1], 3))
    detected_map[:, :, 0] = cnet_input % 256
    detected_map[:, :, 1] = cnet_input // 256
    from annotator.util import HWC3
    detected_map = HWC3(detected_map.astype(np.uint8))
    return [blend_image_and_seg(img, detected_map), Image.fromarray(detected_map)], "Random segmentation done. Left is blended image, right is ControlNet input."


def inject_inference_segmentor(model, img):
    original_result = original_uniformer_inference_segmentor(model, img)
    original_result[0] = strengthen_sem_seg(original_result[0], img)
    return original_result


def inject_sem_seg(self, sem_seg, area_threshold=None, alpha=0.8, is_text=True, edge_color=(1.0, 1.0, 1.0)):
    if isinstance(sem_seg, torch.Tensor):
        sem_seg = sem_seg.numpy()
    return original_oneformer_draw_sem_seg(self, strengthen_sem_seg(sem_seg), area_threshold, alpha, is_text, edge_color)


def _uniformer(img):
    if "uniformer" not in sem_seg_cache:
        from annotator.uniformer import apply_uniformer
        sem_seg_cache["uniformer"] = apply_uniformer
    result = sem_seg_cache["uniformer"](img)
    return result, True


def _oneformer(img, dataset="coco"):
    oneformer_key = f"oneformer_{dataset}"
    if oneformer_key not in sem_seg_cache:
        from annotator.oneformer import OneformerDetector
        sem_seg_cache[oneformer_key] = OneformerDetector(OneformerDetector.configs[dataset])
    result = sem_seg_cache[oneformer_key](img)
    return result, True


def create_symbolic_link():
    cnet_annotator_dir = os.path.join(extensions_dir, "sd-webui-controlnet/annotator")
    if os.path.isdir(cnet_annotator_dir):
        if not os.path.isdir(os.path.join(scripts.basedir(), "annotator")):
            os.symlink(cnet_annotator_dir, scripts.basedir())
        return True
    return False


def clear_sem_sam_cache():
    sem_seg_cache.clear()
    gc.collect()
    torch_gc()
    

def sem_sam_garbage_collect():
    if shared.cmd_opts.lowvram:
        for _, model in sem_seg_cache:
            model.unload_model()
    gc.collect()
    torch_gc()


def blend_image_and_seg(image, seg, alpha=0.5):
    image_blend = np.array(image) * (1 - alpha) + np.array(seg) * alpha
    return Image.fromarray(image_blend.astype(np.uint8))


def semantic_segmentation(input_image, annotator_name):
    if input_image is None:
        return [], "No input image."
    if "seg" in annotator_name:
        if not os.path.isdir(os.path.join(scripts.basedir(), "annotator")) and not create_symbolic_link():
            return [], "ControlNet extension not found."
        input_image_np = np.array(input_image)
        if annotator_name == "seg_ufade20k":
            original_semseg = _uniformer(input_image_np)
            import annotator.uniformer as uniformer
            uniformer.inference_segmentor = inject_inference_segmentor
            sam_semseg = _uniformer(input_image_np)
            output_gallery = [original_semseg, sam_semseg, blend_image_and_seg(input_image, original_semseg), blend_image_and_seg(input_image, sam_semseg)]
            return output_gallery, "Uniformer semantic segmentation of ade20k done. Left is segmentation before SAM, right is segmentation after SAM."
        else:
            dataset = annotator_name.split('_')[-1][2:]
            original_semseg = _oneformer(input_image_np, dataset)
            from annotator.oneformer.oneformer.demo.visualizer import Visualizer
            Visualizer.draw_sem_seg = inject_sem_seg
            sam_semseg = _oneformer(input_image_np, dataset)
            output_gallery = [original_semseg, sam_semseg, blend_image_and_seg(input_image, original_semseg), blend_image_and_seg(input_image, sam_semseg)]
            return output_gallery, f"Oneformer semantic segmentation of {dataset} done. Left is segmentation before SAM, right is segmentation after SAM."
    else:
        return random_segmentation(input_image)


def register_auto_sam(sam, 
    auto_sam_points_per_side, auto_sam_points_per_batch, auto_sam_pred_iou_thresh, 
    auto_sam_stability_score_thresh, auto_sam_stability_score_offset, auto_sam_box_nms_thresh, 
    auto_sam_crop_n_layers, auto_sam_crop_nms_thresh, auto_sam_crop_overlap_ratio, 
    auto_sam_crop_n_points_downscale_factor, auto_sam_min_mask_region_area, auto_sam_output_mode):
    global global_sam
    global_sam = SamAutomaticMaskGenerator(
        sam, auto_sam_points_per_side, auto_sam_points_per_batch, 
        auto_sam_pred_iou_thresh, auto_sam_stability_score_thresh, 
        auto_sam_stability_score_offset, auto_sam_box_nms_thresh, 
        auto_sam_crop_n_layers, auto_sam_crop_nms_thresh, auto_sam_crop_overlap_ratio, 
        auto_sam_crop_n_points_downscale_factor, None, 
        auto_sam_min_mask_region_area, auto_sam_output_mode)
