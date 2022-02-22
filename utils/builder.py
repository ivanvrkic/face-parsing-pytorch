import os

import torch
import torch.nn as nn
import torch.utils.data
import yaml

import datasets
import models
import utils


def load_cfg() -> dict:
    with open('cfgs/main.yaml') as f:
        main = yaml.safe_load(f)

    with open(main['cfg']) as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)
    cfg['model'] = {}
    cfg['model']['name'] = main['model']
    cfg['model']['amp_enabled'] = main['amp_enabled']
    cfg['resume_training'] = main['resume_training']
    cfg['fine_tuning_batchnorm'] = main['fine_tuning_batchnorm']
    cfg['ddp_enabled'] = main['ddp_enabled']
    return cfg


class Builder:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def build_dataset(self, dataset_type: str, ddp_enabled=False) -> tuple[torch.utils.data.Dataset,
                                                                           torch.utils.data.DataLoader]:
        cfg_dataset = self.cfg['dataset']
        root = cfg_dataset['root']
        batch_size = self.cfg[self.cfg['model']['name']]['batch_size']
        num_workers = self.cfg['dataset']['num_workers']
        if num_workers == 'auto':
            num_workers = 4 * torch.cuda.device_count()

        if dataset_type == 'train':
            transforms = datasets.transforms.Transforms(self.cfg, augmentation=True)
            shuffle = True
            pin_memory = cfg_dataset['pin_memory']
        else:
            transforms = datasets.transforms.Transforms(self.cfg)
            shuffle = False
            pin_memory = False

        # Dataset
        if cfg_dataset['name'] == 'CelebAMaskHQ':
            dataset = datasets.celebamaskhq.CelebAMaskHQ(root, dataset_type, target_type='mask', transforms=transforms)
        else:
            raise NotImplementedError('Wrong dataset name.')

        # Dataloader
        if ddp_enabled:
            sampler = torch.utils.data.DistributedSampler(dataset)
            shuffle = False
            pin_memory = False
        else:
            sampler = None
        dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, sampler=sampler,
                                                 num_workers=num_workers, pin_memory=pin_memory)
        return dataset, dataloader

    def build_model(self, num_classes: int, pretrained=False) -> nn.Module:
        cfg_model_name = self.cfg['model']['name']

        if cfg_model_name == 'Proposed':
            model = models.proposed.Proposed(num_classes)
        elif cfg_model_name == 'UNet':
            model = models.unet.UNet(num_classes)
        elif cfg_model_name == 'EAGRNet':
            model = models.eagr.EAGRNet(num_classes)
        elif cfg_model_name == 'BiSeNet':
            model = models.bisenet.BiSeNet(num_classes)
        else:
            raise NotImplementedError('Wrong model name.')

        if pretrained:
            pretrained_weights_path = self.cfg[cfg_model_name]['pretrained_weights']
            if os.path.isfile(pretrained_weights_path):
                state_dict = torch.load(pretrained_weights_path)
                state_dict = utils.state_dict_converter.convert_ddp_state_dict(state_dict)
                model.load_state_dict(state_dict)
            else:
                print(f'FileNotFound: pretrained_weights ({cfg_model_name})')
        return model

    def build_criterion(self) -> nn.Module:
        cfg_criterion = self.cfg[self.cfg['model']['name']]['criterion']

        if cfg_criterion['name'] == 'CrossEntropyLoss':
            criterion = nn.CrossEntropyLoss()
        elif cfg_criterion['name'] == 'FocalLoss':
            criterion = utils.loss.FocalLoss(alpha=cfg_criterion['alpha'], gamma=cfg_criterion['gamma'])
        else:
            raise NotImplementedError('Wrong criterion name.')
        return criterion

    def build_optimizer(self, model: nn.Module) -> torch.optim.Optimizer:
        cfg_optim = self.cfg[self.cfg['model']['name']]['optimizer']

        if cfg_optim['name'] == 'SGD':
            optimizer = torch.optim.SGD(model.parameters(), lr=cfg_optim['lr'], momentum=cfg_optim['momentum'],
                                        weight_decay=cfg_optim['weight_decay'], nesterov=cfg_optim['nesterov'])
        elif cfg_optim['name'] == 'Adam':
            optimizer = torch.optim.Adam(model.parameters(), cfg_optim['lr'], weight_decay=cfg_optim['weight_decay'])
        elif cfg_optim['name'] == 'AdamW':
            optimizer = torch.optim.AdamW(model.parameters(), cfg_optim['lr'], weight_decay=cfg_optim['weight_decay'])
        elif cfg_optim['name'] == 'RAdam':
            optimizer = torch.optim.RAdam(model.parameters(), cfg_optim['lr'], weight_decay=cfg_optim['weight_decay'])
        else:
            raise NotImplementedError('Wrong optimizer name.')
        return optimizer

    def build_scheduler(self, optimizer: torch.optim.Optimizer, max_iter: int):
        cfg_scheduler = self.cfg[self.cfg['model']['name']]['scheduler']

        if cfg_scheduler['name'] == 'PolyLR':
            scheduler = utils.lr_scheduler.PolyLR(optimizer, max_iter)
        else:
            raise NotImplementedError('Wrong scheduler name.')
        return scheduler

    def build_aux_criterion(self) -> nn.Module:
        cfg_aux_criterion = self.cfg[self.cfg['model']['name']]['aux_criterion']

        if cfg_aux_criterion['name'] == 'CrossEntropyLoss':
            aux_criterion = nn.CrossEntropyLoss()
        else:
            raise NotImplementedError('Wrong aux_criterion name.')
        return aux_criterion

    def build_aux_factor(self) -> tuple:
        cfg_aux_factor = self.cfg[self.cfg['model']['name']]['aux_factor']
        return cfg_aux_factor
