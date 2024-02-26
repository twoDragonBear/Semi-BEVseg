import os
from datetime import datetime
from argparse import ArgumentParser
from tqdm import tqdm
from yacs.config import CfgNode
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

from torch.optim import SGD, Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.tensorboard import SummaryWriter

import random
import numpy as np

# from src.models.model_by_2dTo3d import model_by_2dTo3d
# from src.models.model_by_3dTo2d import model_by_3dTo2d
from src.models.model_by_mlp import model_by_mlp

from src.models.loss import Dice_Loss

from src.data.data_factory import *
from src.utils.confusion import BinaryConfusionMatrix
from src.data.nuscenes.utils import NUSCENES_CLASS_NAMES
from src.data.argoverse.utils import ARGOVERSE_CLASS_NAMES
from src.utils.visualise import colorise


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark=True  # speed up
    
    
def train(labeled_train_loader, train_sampler, bev_seg_model, criterion, optimizer, config, epoch):
    bev_seg_model.train()

    # Initialise confusion matrix
    confusion = BinaryConfusionMatrix(config.num_class)

    train_sampler.set_epoch(epoch)
    
    if (torch.distributed.get_rank() == 0):
        dataloader = enumerate(tqdm(labeled_train_loader))
    else:
        dataloader = enumerate(labeled_train_loader)
    
    # Iterate over dataloader
    iteration = (epoch - 1) * len(labeled_train_loader)
    
    for i, batch in dataloader:
        labeled_data = [t.cuda() for t in batch]
        image, label, mask, calib = labeled_data
        
        bev_feature, bev_seg = bev_seg_model(image, calib)  # b x 256 x 200 x 200, b x n x 196 x 200

        segmentation_loss = criterion(bev_seg, label)
        
        optimizer.zero_grad()
        segmentation_loss.backward()
        optimizer.step()

        # Update confusion matrix
        scores = bev_seg.cpu().sigmoid()  # 0~1
        confusion.update(scores > config.score_thresh, label > 0, mask.cpu() > 0)

        # Update tensorboard
        if i % config.log_interval == 0 and torch.distributed.get_rank() == 0:
            print('\n segmentation_loss =', float(segmentation_loss),
                  '\n lr =', optimizer.param_groups[0]['lr'])
            # summary.add_scalar('train/segmentation_loss', float(segmentation_loss), iteration)

        # Visualise
        # if i % config.vis_interval == 0:
        #     visualise(summary, image, scores, label, mask, iteration,
        #               config.train_dataset, split='train')

        iteration += 1

    # Print and record results
    if torch.distributed.get_rank() == 0:
        print('Results on nuscenes training set:')
    display_results(confusion, config.train_dataset)
    # log_results(confusion, config.train_dataset, summary, 'train', epoch)


def evaluate(dataloader, model, criterion, config, epoch):
    model.eval()

    # Initialise confusion matrix
    confusion = BinaryConfusionMatrix(config.num_class)

    data = enumerate(tqdm(dataloader)) if (torch.distributed.get_rank() == 0) else enumerate(dataloader)

    # Iterate over dataset
    for i, batch in data:
        # Move tensors to GPU
        batch = [t.cuda() for t in batch]

        # Predict class occupancy scores and compute loss
        image, label, mask, calib = batch
        with torch.no_grad():
            _, logits = model(image, calib)
            loss = criterion(logits, label)

        # Update confusion matrix
        scores = logits.cpu().sigmoid()
        confusion.update(scores > config.score_thresh, label > 0, mask.cpu() > 0)
        '''
        # Update tensorboard
        if i % config.log_interval == 0:
            summary.add_scalar('val/loss', float(loss), epoch)

        # Visualise
        if i % config.vis_interval == 0:
            visualise(summary, image, scores, label, mask, epoch,
                      config.train_dataset, split='val')
        '''
    # Print and record results
    mean_iou = display_results(confusion, config.train_dataset)
    # log_results(confusion, config.train_dataset, summary, 'val', epoch)

    return mean_iou
    

'''
def visualise(summary, image, scores, label, mask, step, dataset, split):
    class_names = NUSCENES_CLASS_NAMES if dataset == 'nuscenes' \
        else ARGOVERSE_CLASS_NAMES

    summary.add_image(split + '/image', image[0], step, dataformats='CHW')
    summary.add_image(split + '/pred', colorise(scores[0], 'coolwarm', 0, 1),
                      step, dataformats='NHWC')
    summary.add_image(split + '/gt', colorise(label[0], 'coolwarm', 0, 1),
                      step, dataformats='NHWC')
'''

def display_results(confusion, dataset):
    torch.distributed.barrier()

    # Display confusion matrix summary
    class_names = NUSCENES_CLASS_NAMES if dataset == 'nuscenes' else ARGOVERSE_CLASS_NAMES

    iou = confusion.iou.cuda().clone()
    torch.distributed.all_reduce(iou, op=torch.distributed.ReduceOp.SUM)
    iou /= torch.distributed.get_world_size()

    if torch.distributed.get_rank() == 0:
        for name, iou_score in zip(class_names, iou):
            print('{:20s} {:.3f}'.format(name, iou_score))

    mean_iou = torch.tensor(confusion.mean_iou).cuda().clone()
    torch.distributed.all_reduce(mean_iou, op=torch.distributed.ReduceOp.SUM)
    mean_iou /= torch.distributed.get_world_size()

    if torch.distributed.get_rank() == 0:
        print('{:20s} {:.3f}'.format('MEAN', mean_iou))

    return mean_iou

'''
def log_results(confusion, dataset, summary, split, epoch):
    # Display and record epoch IoU scores
    class_names = NUSCENES_CLASS_NAMES if dataset == 'nuscenes' \
        else ARGOVERSE_CLASS_NAMES

    for name, iou_score in zip(class_names, confusion.iou):
        summary.add_scalar(f'{split}/iou/{name}', iou_score, epoch)
    summary.add_scalar(f'{split}/iou/MEAN', confusion.mean_iou, epoch)
'''

def save_checkpoint(path, model, optimizer, scheduler, epoch):
    if isinstance(model, nn.parallel.distributed.DistributedDataParallel):
        model = model.module

    ckpt = {
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'scheduler': scheduler.state_dict(),
        'epoch': epoch
    }

    torch.save(ckpt, path)


def load_checkpoint(path, model, optimizer, scheduler):
    ckpt = torch.load(path)

    # Load model weights
    if isinstance(model, nn.parallel.distributed.DistributedDataParallel):
        model = model.module
    model.load_state_dict(ckpt['model'])

    # Load optimiser state
    optimizer.load_state_dict(ckpt['optimizer'])

    # Load scheduler state
    scheduler.load_state_dict(ckpt['scheduler'])

    return ckpt['epoch']


def create_experiment(config, tag, resume=None):
    # Restore an existing experiment if a directory is specified
    if resume is not None:
        print("\n==> Restoring experiment from directory:\n" + resume)
        logdir = resume
    else:
        # Otherwise, generate a run directory based on the current time
        name = datetime.now().strftime('{}_%y-%m-%d--%H-%M-%S').format(tag)
        logdir = os.path.join(os.path.expandvars(config.logdir), name)
        print("\n==> Creating new experiment in directory:\n" + logdir)
        os.makedirs(logdir)

    # Display the config options on-screen
    print(config.dump())

    # Save the current config
    with open(os.path.join(logdir, 'config.yml'), 'w') as f:
        f.write(config.dump())

    return logdir


def main():
    parser = ArgumentParser()
    parser.add_argument('--tag', type=str, default='run',
                        help='optional tag to identify the run')
    parser.add_argument('--resume', default=None,
                        help='path to an experiment to resume')
    parser.add_argument('--options', nargs='*', default=[],
                        help='list of addition config options as key-val pairs')
    
    parser.add_argument('--model', choices=['2dTo3d_based', '3dTo2d_based', 'mlp_based'], default='mlp_based', help='type of bev seg model')
    parser.add_argument('--img_size', help='resolution of input img', nargs="+", type=int, default=[800, 600])  # 224 x 448, 300x400, 600 x 800
    parser.add_argument('--dataset', choices=['nuscenes', 'argoverse'], default='nuscenes', help='dataset to train on')

    args = parser.parse_args()

    # Load configuration
    with open('./configs/config.yml') as f:
        config = CfgNode.load_cfg(f)
    
    config['model'] = args.model
    config['img_size'] = args.img_size
    config['train_dataset'] = args.dataset
    
    # if args.dataset == 'nuscenes':
    config['num_class'] = 14
    
    # 1) 初始化
    torch.distributed.init_process_group(backend="nccl")
    # 2） 配置每个进程的gpu
    local_rank = torch.distributed.get_rank()
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)

    setup_seed(42 + torch.distributed.get_rank())
    
    # Setup experiment
    bev_seg_model = model_by_mlp(config).to(device)  # B x 3 x 600 x 800, B x 3 x 3 ==> B x n x 196 x 200
    
    bev_seg_model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(bev_seg_model)
    bev_seg_model = DDP(bev_seg_model, device_ids=[local_rank], output_device=local_rank, find_unused_parameters=True)

    criterion = Dice_Loss().cuda()
    
    if torch.distributed.get_rank() == 0:
        # print('test: random.random() =', random.random())
        # print('test: np.random.random() =', np.random.random())
        # print('test: torch.rand(1) =', torch.randn(1))
        print('num of trainable parameters =', sum(p.numel() for p in bev_seg_model.parameters() if p.requires_grad))

    train_data, test_data = build_fullNu_datasets(config)
    
    train_sampler = DistributedSampler(train_data)
    train_loader = DataLoader(train_data, batch_size=config.batch_size // torch.cuda.device_count(), 
                              num_workers=config.num_workers // torch.cuda.device_count(), sampler=train_sampler)
    
    test_sampler = DistributedSampler(test_data)
    test_loader = DataLoader(test_data, batch_size=4, num_workers=8, sampler=test_sampler)

    # Build optimiser and learning rate scheduler
    optimizer = Adam(bev_seg_model.parameters(), lr=config.learning_rate)
    lr_scheduler = MultiStepLR(optimizer, config.lr_milestones, 0.5)
    
    if torch.distributed.get_rank() == 0:
        # Create a directory for the experiment
        logdir = create_experiment(config, args.tag, args.resume)
        # Create tensorboard summary
        # summary = SummaryWriter(logdir)
    
    epoch, best_iou = 1, 0

    '''
    if torch.distributed.get_rank() == 0:
        print('Results on nuscenes testing set:')
    test_iou = evaluate(nuscenes_test_loader, bev_seg_model, criterion, config, 0)
    '''

    # Main training loop
    while epoch <= config.num_epochs:
        if torch.distributed.get_rank() == 0:
            print('\n\n=== Beginning epoch {} of {} ==='.format(epoch, config.num_epochs))
        # Train model for one epoch
        train(train_loader, train_sampler, bev_seg_model, criterion, optimizer, config, epoch)

        # Update learning rate
        lr_scheduler.step()

        '''
        if epoch <= config.lr_milestones[0]:
            epoch += 1
            continue
        '''

        # Evaluate on the test set
        if torch.distributed.get_rank() == 0:
            print('Results on nuscenes testing set:')
        test_iou = evaluate(test_loader, bev_seg_model, criterion, config, epoch)
        
        if torch.distributed.get_rank() == 0:
            if test_iou > best_iou:
                best_iou = test_iou
                save_checkpoint(os.path.join(logdir, 'iou_{}.pth'.format(best_iou)), bev_seg_model, optimizer, lr_scheduler, epoch)
            print('Best IOU =', best_iou)

        epoch += 1

    print("\n Process {} complete!".format(torch.distributed.get_rank()))


if __name__ == '__main__':
    main()





