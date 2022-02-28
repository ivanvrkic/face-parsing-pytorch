import os

import random
import numpy as np
import torch.backends.cudnn
import torch.distributed
import torch.utils.data
import torch.utils.tensorboard
import tqdm

import datasets
import eval
import utils

if __name__ == '__main__':
    # Create components builder
    builder = utils.builder.Builder()
    config = builder.config
    model_name = builder.model_name

    # Create variables that control training
    epoch = config[model_name]['epoch']
    amp_enabled = config['train']['amp_enabled']
    ddp_enabled = config['train']['ddp_enabled']
    ddp_find_unused_parameters = config['train']['ddp_find_unused_parameters']
    optimizer_zero_grad_set_to_none = config['train']['optimizer_zero_grad_set_to_none']
    reproducibility = config['train']['reproducibility']
    seed = config['train']['reproducibility_seed']
    resume_training = config['train']['resume_training']
    resume_training_checkpoint = config['train']['resume_training_checkpoint']

    # Distributed Data-Parallel Training (DDP)
    local_rank = 0
    world_size = 0
    if ddp_enabled:
        assert torch.distributed.is_nccl_available(), 'NCCL backend is not available.'
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        local_rank = torch.distributed.get_rank()
        world_size = torch.distributed.get_world_size()
        os.system('clear')

    # Device
    if torch.cuda.is_available():
        torch.cuda.set_device(local_rank)
        device = torch.device('cuda', local_rank)
    else:
        device = torch.device('cpu')

    # Reproducibility
    if reproducibility:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        # torch.use_deterministic_algorithms(True) # strict method

    # 1. Dataset
    trainset, trainloader = builder.build_dataset('train', ddp_enabled)
    _, valloader = builder.build_dataset('test', ddp_enabled)

    # 2. Model
    model = builder.build_model(trainset.num_classes).to(device)
    if ddp_enabled:
        model = torch.nn.parallel.DistributedDataParallel(
            model, find_unused_parameters=ddp_find_unused_parameters
        )
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    print(f'Activated model: {model_name} (rank{local_rank})')

    # 3. Loss function, optimizer, lr scheduler, scaler, aux loss function
    criterion = builder.build_criterion()
    optimizer = builder.build_optimizer(model)
    scheduler = builder.build_scheduler(optimizer, len(trainloader) * epoch)
    scaler = torch.cuda.amp.GradScaler(enabled=amp_enabled)
    if config[model_name]['aux_criterion'] is not None:
        aux_criterion = builder.build_aux_criterion()
        aux_factor = builder.build_aux_factor()
    else:
        aux_criterion = None
        aux_factor = None

    # Resume training at checkpoint
    start_epoch = 0
    prev_mean_f1 = 0.0
    prev_val_loss = 2 ** 32 - 1
    if resume_training:
        if ddp_enabled:
            torch.distributed.barrier()
            checkpoint = torch.load(resume_training_checkpoint, map_location={'cuda:0': f'cuda:{local_rank}'})
        else:
            checkpoint = torch.load(resume_training_checkpoint)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        scaler.load_state_dict(checkpoint['scaler_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        prev_mean_f1 = checkpoint['mean_f1']
        prev_val_loss = checkpoint['val_loss']
        print(f'Resume training. {resume_training_checkpoint} (rank{local_rank})')

    # 4. Tensorboard
    if local_rank == 0:
        writer = torch.utils.tensorboard.SummaryWriter(os.path.join('runs', model_name))
    else:
        writer = None

    # 5. Train and evaluate
    for eph in tqdm.tqdm(range(start_epoch, epoch),
                         desc='Epoch', disable=False if local_rank == 0 else True):
        if utils.train_interupter.train_interupter():
            print('Train interrupt occurs.')
            break
        if ddp_enabled:
            trainloader.sampler.set_epoch(eph)
            torch.distributed.barrier()
        model.train()

        for batch_idx, (images, targets) in enumerate(tqdm.tqdm(trainloader, desc='Batch', leave=False,
                                                                disable=False if local_rank == 0 else True)):
            iters = len(trainloader) * eph + batch_idx
            images, targets = images.to(device), targets.to(device)

            optimizer.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast(enabled=amp_enabled):
                if aux_criterion is not None:
                    outputs, aux_outputs = model(images)
                    aux_loss = 0
                    for i, aux_output in enumerate(aux_outputs):
                        aux_loss += aux_criterion(aux_output, targets) * aux_factor[i]
                    loss = criterion(outputs, targets) + aux_loss
                else:
                    outputs = model(images)
                    loss = criterion(outputs, targets)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            if ddp_enabled:
                loss_list = [torch.zeros(1, device=device) for _ in range(world_size)]
                torch.distributed.all_gather_multigpu([loss_list], [loss])
                if writer is not None:
                    for i, rank_loss in enumerate(loss_list):
                        writer.add_scalar(f'loss/training (rank{i})', rank_loss.item(), iters)
                    writer.add_scalar('lr', optimizer.param_groups[0]['lr'], iters)
            else:
                writer.add_scalar(f'loss/training (rank{local_rank})', loss.item(), iters)
                writer.add_scalar('lr', optimizer.param_groups[0]['lr'], iters)

            scheduler.step()

        # Evaluate
        val_loss, mean_f1, _, _ = eval.evaluate(model, valloader, criterion, trainset.num_classes,
                                                amp_enabled, ddp_enabled, device)
        if writer is not None:
            writer.add_scalar('loss/validation', val_loss, eph)
            writer.add_scalar('metrics/mean F1', mean_f1, eph)

        # Write predicted segmentation map
        if writer is not None:
            images, targets = valloader.__iter__().__next__()
            images, targets = images[10:13].to(device), targets[10:13].to(device)
            with torch.no_grad():
                outputs = model(images)
                outputs = torch.argmax(outputs, dim=1)

            mean = torch.tensor(trainset.transforms.normalize.mean)
            std = torch.tensor(trainset.transforms.normalize.std)
            images = datasets.utils.inverse_to_tensor_normalize(datasets.utils.inverse_normalize(images, mean, std))
            if eph == 0:
                targets = datasets.utils.draw_segmentation_masks(images, targets, trainset.colors)
                writer.add_images('eval/1Groundtruth', targets, eph)
            outputs = datasets.utils.draw_segmentation_masks(images, outputs, trainset.colors)
            writer.add_images('eval/2' + model_name, outputs, eph)

        if local_rank == 0:
            # Save checkpoint
            os.makedirs('weights', exist_ok=True)
            torch.save({
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scheduler_state_dict': scheduler.state_dict(),
                'scaler_state_dict': scaler.state_dict(),
                'epoch': eph,
                'mean_f1': mean_f1,
                'val_loss': val_loss
            }, os.path.join('weights', f'{model_name}_checkpoint.pth'))

            # Save best mean_f1 model
            if mean_f1 > prev_mean_f1:
                torch.save(model.state_dict(), os.path.join('weights', f'{model_name}_best_mean_f1.pth'))
                prev_mean_f1 = mean_f1

            # Save best val_loss model
            if val_loss < prev_val_loss:
                torch.save(model.state_dict(), os.path.join('weights', f'{model_name}_best_val_loss.pth'))
                prev_val_loss = val_loss
    if writer is not None:
        writer.close()
    if ddp_enabled:
        torch.distributed.destroy_process_group()
