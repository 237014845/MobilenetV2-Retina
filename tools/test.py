import argparse

import torch
import mmcv
from mmcv.runner import load_checkpoint, parallel_test, obj_from_dict
from mmcv.parallel import scatter, collate, MMDataParallel

from mmdet import datasets
from mmdet.core import results2json, coco_eval
from mmdet.datasets import build_dataloader
from mmdet.models import build_detector, detectors

# train_data:
# data = dict(
        #     img=DC(to_tensor(img), stack=True),
        #     img_meta=DC(img_meta, cpu_only=True),
        #     gt_bboxes=DC(to_tensor(gt_bboxes)),
        #     gt_labels=DC(to_tensor(gt_labels)),
        #     proposals=DC(to_tensor(proposals)),
        #     gt_bboxes_ignore= DC(to_tensor(gt_bboxes_ignore)),
        #     gt_masks= DC(gt_masks, cpu_only=True) )
# test_data:
# data = dict(img=imgs, img_meta=img_metas)
def single_test(model, data_loader, show=False):
    model.eval()
    results = []
    dataset = data_loader.dataset
    prog_bar = mmcv.ProgressBar(len(dataset))
    for i, data in enumerate(data_loader):
        with torch.no_grad():
            result = model(return_loss=False, rescale=not show, **data)
        results.append(result)

        if show:
            model.module.show_result(data, result, dataset.img_norm_cfg,
                                     dataset=dataset.CLASSES)

        batch_size = data['img'][0].size(0)
        for _ in range(batch_size):
            prog_bar.update()
    return results


def _data_func(data, device_id):
    data = scatter(collate([data], samples_per_gpu=1), [device_id])[0]
    return dict(return_loss=False, rescale=True, **data)


def parse_args():
    parser = argparse.ArgumentParser(description='MMDet test detector')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument(
        '--gpus', default=1, type=int, help='GPU number used for testing')
    parser.add_argument(
        '--proc_per_gpu',
        default=1,
        type=int,
        help='Number of processes per GPU')
    parser.add_argument('--out', help='output result file')
    parser.add_argument(
        '--eval',
        type=str,
        nargs='+',
        choices=['proposal', 'proposal_fast', 'bbox', 'segm', 'keypoints'],
        help='eval types')
    parser.add_argument('--show', action='store_true', help='show results')
    args = parser.parse_args()
    return args


def main():
    args = parse_args()

    if args.out is not None and not args.out.endswith(('.pkl', '.pickle')):
        raise ValueError('The output file must be a pkl file.')

    cfg = mmcv.Config.fromfile(args.config)
    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = True
    cfg.model.pretrained = None
    cfg.data.test.test_mode = True

    # VOCDataset(ann_file=data_root + 'VOC2007/ImageSets/Main/test.txt',
    #           img_prefix=data_root + 'VOC2007/',
    #           img_scale=(300, 300),
    #           img_norm_cfg=img_norm_cfg,
    #           size_divisor=None,
    #           flip_ratio=0,
    #           with_mask=False,
    #           with_label=False,
    #           test_mode=True,
    #           resize_keep_ratio=False)
    dataset = obj_from_dict(cfg.data.test, datasets, dict(test_mode=True))
    if args.gpus == 1:
        # build(cfg, DETECTORS, dict(train_cfg=train_cfg, test_cfg=test_cfg))
        # SingleStageDetector(pretrained=..., backbone=..., neck=..., bbox_head=...,
        #                     train_cfg=None, test_cfg=...)

        # 首先要先注册 BACKBONES、 NECKS、 ROI_EXTRACTORS、 HEADS、 DETECTORS、
        # 然后 BACKBONES.register_module（class SSDVGG） @HEADS.register_module(class AnchorHead)
        #     @HEADS.register_module(class SSDHead)   @DETECTORS.register_module(class SingleStageDetector)
        # 最后 build_detector() 相当于SingleStageDetector(**args)
        model = build_detector(
            cfg.model, train_cfg=None, test_cfg=cfg.test_cfg)
        load_checkpoint(model, args.checkpoint)
        model = MMDataParallel(model, device_ids=[0])

        data_loader = build_dataloader(
            dataset,
            imgs_per_gpu=1,
            workers_per_gpu=cfg.data.workers_per_gpu,
            num_gpus=1,
            dist=False,
            shuffle=False)
        outputs = single_test(model, data_loader, args.show)
    else:
        model_args = cfg.model.copy()
        model_args.update(train_cfg=None, test_cfg=cfg.test_cfg)
        model_type = getattr(detectors, model_args.pop('type'))
        outputs = parallel_test(
            model_type,
            model_args,
            args.checkpoint,
            dataset,
            _data_func,
            range(args.gpus),
            workers_per_gpu=args.proc_per_gpu)

    if args.out:
        print('writing results to {}'.format(args.out))
        mmcv.dump(outputs, args.out)
        eval_types = args.eval
        if eval_types:
            print('Starting evaluate {}'.format(' and '.join(eval_types)))
            if eval_types == ['proposal_fast']:
                result_file = args.out
                coco_eval(result_file, eval_types, dataset.coco)
            else:
                if not isinstance(outputs[0], dict):
                    result_file = args.out + '.json'
                    results2json(dataset, outputs, result_file)
                    coco_eval(result_file, eval_types, dataset.coco)
                else:
                    for name in outputs[0]:
                        print('\nEvaluating {}'.format(name))
                        outputs_ = [out[name] for out in outputs]
                        result_file = args.out + '.{}.json'.format(name)
                        results2json(dataset, outputs_, result_file)
                        coco_eval(result_file, eval_types, dataset.coco)


if __name__ == '__main__':
    main()
