import os
import wandb
import torch
import logging
import time
import evaluation
from data.dataset import BaseDataset
from copy import deepcopy

from tqdm import tqdm
from torch import optim
from utils import get_model_class, MetaOptimizer
from collections import defaultdict
from model.loss_func import *
from data.dataset import *
from typing import Dict, List, Optional, Tuple

from utils.utils import xavier_normal_initialization, normal_initialization

from model.basemodel import BaseModel

class MetaModel(BaseModel):
    def __init__(self, config: Dict, dataset_list: List[BaseDataset]) -> None:
        super().__init__(config, dataset_list)
        self.interval = config['train']['interval']
        self.step_counter = 0
        self.item_embedding = None # MetaModel is just a trainer without item embedding

    def _init_model(self, train_data):
        self.sub_model : BaseModel = self._register_sub_model()
        self.sub_model._init_model(train_data)
        self.item_embedding = self.sub_model.item_embedding

        self.meta_module : nn.Module = self._register_meta_modules()
        self.meta_module = self.meta_module.to(self.device)
        self.meta_module.apply(normal_initialization)

        self.meta_optimizer = self._get_meta_optimizers()
        self.metaloader_iter = iter(self.current_epoch_metaloaders(nepoch=0))

    def _register_sub_model(self) -> BaseModel:
        sub_model_config = deepcopy(self.config)
        sub_model_config['model']['model'] = self.config['model']['sub_model']
        model_class = get_model_class(sub_model_config['model'])
        return model_class(sub_model_config, self.dataset_list)

    def _register_meta_modules(self) -> nn.Module:
        return nn.Sequential(
            nn.Linear(self.embed_dim, self.embed_dim),
            nn.ReLU(),
            nn.Linear(self.embed_dim, 1),
        )

    def _get_meta_optimizers(self):
        opt_name = self.config['train']['meta_optimizer']
        lr = self.config['train']['meta_learning_rate']
        weight_decay = self.config['train']['meta_weight_decay']
        params = self.meta_module.parameters()

        if opt_name.lower() == 'adam':
            optimizer = optim.Adam(params, lr=lr)
        elif opt_name.lower() == 'sgd':
            optimizer = optim.SGD(params, lr=lr, weight_decay=weight_decay)
        elif opt_name.lower() == 'adagrad':
            optimizer = optim.Adagrad(params, lr=lr)
        elif opt_name.lower() == 'rmsprop':
            optimizer = optim.RMSprop(params, lr=lr)
        elif opt_name.lower() == 'sparse_adam':
            optimizer = optim.SparseAdam(params, lr=lr)
        else:
            optimizer = optim.Adam(params, lr=lr, weight_decay=2)

        optimizer = MetaOptimizer(optimizer, hpo_lr=1)

        return optimizer

    def forward(self, batch):
        return self.sub_model.forward(batch)

    def current_epoch_metaloaders(self, nepoch):
        return self.dataset_list[0].get_loader()

    def training_epoch(self, nepoch):
        output_list = []

        trn_dataloaders = self.current_epoch_trainloaders(nepoch)
        trn_dataloaders = [trn_dataloaders]

        for loader_idx, loader in enumerate(trn_dataloaders):
            outputs = []
            loader = tqdm(
                loader,
                total=len(loader),
                ncols=75,
                desc=f"Training {nepoch:>5}",
                leave=False,
            )
            for batch_idx, batch in enumerate(loader):
                batch = {k: v.to(self.device) for k, v in batch.items()}
                batch['neg_item'] = self._neg_sampling(batch)
                self.sub_model.optimizer.zero_grad()
                if nepoch > self.config['train']['warmup_epoch']:
                    training_step_args = {'batch': batch, 'sub_model': self.sub_model}
                    loss = self.training_step(**training_step_args)
                else:
                    training_step_args = {'batch': batch}
                    loss = self.sub_model.training_step(**training_step_args)
                loss.backward()
                self.sub_model.optimizer.step()
                outputs.append({f"loss_{loader_idx}": loss.detach()})
                self.step_counter += 1
                if self.step_counter % self.config['train']['interval'] == 0 and \
                    nepoch > self.config['train']['warmup_epoch']:
                    self._outter_loop(nepoch)
            output_list.append(outputs)
        return output_list

    def _outter_loop(self, nepoch):
        meta_dataloader = self.current_epoch_metaloaders(nepoch)

        assert self.config['train']['descent_step'] < len(meta_dataloader)
        loader = tqdm(
            meta_dataloader,
            total=self.config['train']['descent_step'],
            ncols=75,
            desc=f"Meta Training {nepoch:>5}",
            leave=False,
        )
        proxy_model = self._register_sub_model().to(self.device)
        proxy_model._init_model(self.dataset_list[0])
        proxy_model.load_state_dict(self.sub_model.state_dict())
        for batch_idx, batch in enumerate(loader):
            if batch_idx >= self.config['train']['descent_step']:
                break
            batch = {k: v.to(self.device) for k, v in batch.items()}
            batch['neg_item'] = self._neg_sampling(batch)
            training_step_args = {'batch': batch, 'sub_model': proxy_model}
            loss = self.training_step(**training_step_args)
            proxy_model.optimizer.zero_grad()
            loss.backward()
            proxy_model.optimizer.step()

        try:
            batch = next(self.metaloader_iter)
        except StopIteration:
            self.metaloader_iter = iter(self.current_epoch_metaloaders(nepoch))
            batch = next(self.metaloader_iter)
        batch = {k: v.to(self.device) for k, v in batch.items()}
        batch['neg_item'] = self._neg_sampling(batch)
        training_step_args = {'batch': batch, 'sub_model': proxy_model}
        meta_loss = self.training_step(**training_step_args)

        trainloader = self.current_epoch_trainloaders(nepoch)
        batch = next(iter(trainloader))
        batch = {k: v.to(self.device) for k, v in batch.items()}
        batch['neg_item'] = self._neg_sampling(batch)
        training_step_args = {'batch': batch, 'sub_model': self.sub_model}
        meta_train_loss = self.training_step(**training_step_args)

        hyper_grads = self.meta_optimizer.step(
            val_loss=meta_loss,
            train_loss=meta_train_loss,
            aux_params = list(self.meta_module.parameters()),
            parameters = list(self.sub_model.parameters()),
            return_grads = True,
            entropy = None
        )

    def selection(self, query):
        logits = self.meta_module(query)
        logits = F.softmax(logits, dim=0)
        return logits.squeeze()

    def training_step(self, batch, sub_model):
        query = self.forward(batch)
        pos_score = (query * sub_model.item_embedding.weight[batch[self.fiid]]).sum(-1)
        neg_score = (query * sub_model.item_embedding.weight[batch['neg_item']]).sum(-1)
        pos_score[batch[self.fiid] == 0] = -torch.inf # padding

        loss_value = sub_model.loss_fn(pos_score, neg_score, sum=False)
        weight = self.selection(query) * query.shape[0]
        loss_value = (loss_value * weight).sum()

        return loss_value