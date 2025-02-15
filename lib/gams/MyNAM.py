import os
from collections import OrderedDict
from os.path import join as pjoin  # pylint: disable=g-importing-member
from os.path import exists as pexists
import time

from argparse import Namespace
import numpy as np
import math
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import torchvision as tv
from pytorch_lightning.core import LightningModule
from sklearn.metrics import average_precision_score
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.utils.data import TensorDataset, DataLoader
from apex import amp
from .utils import DotDict
from .base import MyGAMPlotMixinBase
from .EncodingBase import OnehotEncodingClassifierMixin, OnehotEncodingRegressorMixin
from .MyBaselines import MyMaxMinTransformMixin, MyTransformClassifierMixin


# TODO: implement the custom activation layer
# https://github.com/google-research/google-research/blob/master/neural_additive_models/models.py



def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # Patched from pytorch 1.6 to be put here
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def relu_n(x, n = 1):
    """ReLU activation clipped at n."""
    return torch.clamp_(x, 0, n)


class ActivationLayer(nn.Module):
    '''
    Follow https://github.com/google-research/google-research/blob/master/neural_additive_models/models.py
    To implement custom layer.
    '''
    def __init__(self, in_features, out_features, activation='exu'):
        super(ActivationLayer, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.activation = activation

        self.weight = Parameter(torch.Tensor(out_features, in_features))

        if self.activation == 'exu':
            self.bias = Parameter(torch.tensor(0.))
        else:
            self.bias = Parameter(torch.Tensor(out_features))
        self.reset_parameters()

    def reset_parameters(self):
        # init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.activation == 'exu':
            trunc_normal_(self.weight, mean=4.0, std=0.5, a=3., b=5.)
        else:
            nn.init.xavier_uniform_(self.weight)

        if self.bias is not None:
            trunc_normal_(self.bias, mean=0., std=0.5, a=-1., b=1.)
    
    def forward(self, input):
        if self.activation == 'exu':
            input = input - self.bias
            h = F.linear(input, torch.exp(self.weight))
            return relu_n(h)
        
        h = F.linear(input, self.weight, self.bias)
        return torch.clamp_(h, min=0.) # relu


class MyNAMBase(nn.Module):
    def __init__(
        self,
        # Training
        deep=1,
        activation='exu',
        lr=0.02,
        lambda_1=0.,
        lambda_2=0.,
        dropout=0.1, 
        feature_dropout=0.05, 
        # Training parameters
        batch_size=1024,
        max_epochs=1000,
        early_stop=5,
        holdout_split=0.176,
        obj='bce',
        opt_level='O1',
        seed=1377,
    ):
        super().__init__()
        self.deep = deep
        self.activation = activation
        self.lr = lr
        self.lambda_1 = lambda_1
        self.lambda_2 = lambda_2
        self.dropout = dropout
        self.feature_dropout = feature_dropout
        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self.early_stop = early_stop
        self.holdout_split = holdout_split
        self.opt_level = opt_level
        self.seed = seed

        # Move model to cuda if available
        self.cur_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # Binary prediction
        self.obj_fn = F.binary_cross_entropy_with_logits

    def init_arch(self, n_features):
        self.n_features = n_features

         # Make intialization the same seed
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        self.layers = nn.ModuleList()
        for _ in range(n_features):
            self.layers.append(self._create_per_feat_layer())
        self.final_bias = nn.Parameter(torch.tensor(0.))
        self.feat_dropout_layer = nn.Dropout(p=self.feature_dropout)

    def _create_per_feat_layer(self):
        hidden_nodes = [64, 64, 32] if self.deep else [1024]

        layers = []
        for layer_i, (nh_s, nh_e) in enumerate(zip([1] + hidden_nodes, hidden_nodes + [1])):
            if layer_i == 0: # the first hidden layer
                layers.append(ActivationLayer(nh_s, nh_e, activation=self.activation))
            else:
                layers.append(ActivationLayer(nh_s, nh_e, activation='relu'))
            
            if self.dropout > 0.:
                layers.append(nn.Dropout(p=self.dropout))
        
        return nn.Sequential(*layers)
    
    def forward(self, X):
        scores = self.predict_per_feat(X)
        scores = scores.sum(dim=1)
        return scores + self.final_bias
    
    def predict_per_feat(self, X):
        assert self.n_features == X.shape[1]
        
        scores = []
        for fi in range(X.shape[1]):
            scores.append(self.layers[fi](X[:, fi:(fi+1)]))
        scores = torch.cat(scores, dim=1)
        scores = self.feat_dropout_layer(scores)
        return scores

    def fit(self, X, y):
        # Initialize architecture on the fly to know how many features
        self.init_arch(X.shape[1])

        y = y.values # Make them numpy
        stratify = y
        X_train, X_val, y_train, y_val = train_test_split(
            X, y,
            test_size=self.holdout_split,
            random_state=self.seed,
            stratify=stratify)

        X_train = torch.from_numpy(X_train).float()
        X_val = torch.from_numpy(X_val).float()
        y_train = torch.from_numpy(y_train).float()
        y_val = torch.from_numpy(y_val).float()

        train_d = TensorDataset(X_train, y_train)
        train_loader = DataLoader(
            train_d, 
            batch_size=self.batch_size,
            shuffle=True,
            pin_memory=True,
            num_workers=1,
        )

        val_d = TensorDataset(X_val)
        val_loader = DataLoader(
            val_d, 
            batch_size=self.batch_size,
            shuffle=False,
            pin_memory=True,
            num_workers=1,
        )

        self.to(self.cur_device)

        opt = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.lambda_1)
        self, opt = amp.initialize(self, opt, opt_level=self.opt_level)

        scheduler = torch.optim.lr_scheduler.ExponentialLR(opt, gamma=0.995)
        es_patience, es_best_epoch, es_best_metric = self.early_stop, -1, -1

        # Training epochs
        start_train_time = time.time()
        print('Epoch \t Seconds \t LR \t \t Train Loss \t Train Acc \t Val AUC \t Val Loss \t Val Acc')
        for epoch in range(self.max_epochs):
            self.train()
            start_epoch_time = time.time()
            train_loss = 0
            train_acc = 0
            train_n = 0
            for batch_idx, (s, l) in enumerate(train_loader):
                s, l = s.to(self.cur_device), l.to(self.cur_device)

                scores = self.predict_per_feat(s)
                logit = scores.sum(dim=1) + self.final_bias
                loss = self.obj_fn(logit, l)
                # Add output weight decay
                if self.lambda_2 > 0.:
                    loss += self.lambda_2 * (scores ** 2).mean()
                
                opt.zero_grad()
                with amp.scale_loss(loss, opt) as scaled_loss:
                    scaled_loss.backward()
                opt.step()
                train_loss += loss.item() * l.size(0)
                train_acc += ((logit >= 0) == l).sum().item()
                train_n += l.size(0)

            scheduler.step()

            # Implemetn val step and early stopping
            self.eval()
            y_val = y_val.to(self.cur_device)
            val_logits = []
            for s, in val_loader:
                s = s.to(self.cur_device)

                with torch.no_grad():
                    output = self(s)
                val_logits.append(output)
            
            val_logits = torch.cat(val_logits, dim=0)
            
            val_loss = self.obj_fn(val_logits, y_val).item()
            val_acc = ((val_logits >= 0) == y_val).float().mean().item()
            val_auc = roc_auc_score(y_val.cpu().numpy(), val_logits.cpu().numpy())

            epoch_time = time.time()
            lr = scheduler.get_last_lr()[0]
            print('%d \t %.1f \t \t %.4f \t %.4f \t %.4f \t %.4f \t %.4f \t %.4f' 
                % (epoch, epoch_time - start_epoch_time, lr, train_loss/train_n, train_acc/train_n,
                   val_auc, val_loss, val_acc))
            
            if val_auc >= es_best_metric:
                es_patience = self.early_stop
                es_best_epoch = epoch
                es_best_metric = val_auc
            else:
                es_patience -= 1

            if es_patience <= 0:
                print('Early stopping! The best epoch is %d with %.4f auc' % (es_best_epoch, es_best_metric))
                break

    def predict_proba(self, X):
        X = torch.from_numpy(X.values).float()
        X = X.to(self.cur_device)
        output = self(X)
        return torch.sigmoid(output).cpu().numpy()

    @staticmethod
    def parse_str_to_parameters(model_name):
        # Understand the format
        params = {}

        name_split = model_name.split('-')
        assert name_split[0] == 'nam'
        for param_str in name_split[1:]:
            if param_str.startswith('d'):
                params['deep'] = int(param_str[1])
            elif param_str.startswith('lr'):
                params['lr'] = float(param_str[2:])
            elif param_str.startswith('l1'):
                params['lambda_1'] = float(param_str[2:])
            elif param_str.startswith('l2'):
                params['lambda_2'] = float(param_str[2:])
            elif param_str.startswith('l3'):
                params['dropout'] = float(param_str[2:])
            elif param_str.startswith('l4'):
                params['feature_dropout'] = float(param_str[2:])
            elif param_str.startswith('r'):
                params['seed'] = float(param_str[1:])
            else:
                raise NotImplementedError('the param_str is not in the supported format %s' % param_str)
        return params


class MyNAMClassifier(OnehotEncodingClassifierMixin, MyGAMPlotMixinBase, MyMaxMinTransformMixin, 
                      MyTransformClassifierMixin, MyNAMBase):
    pass


# class MyNAMRegressor(MyNAMBase):
#     ''' Have not fully implemented like different loss etc. '''
#     def predict(self, X):
#         X = torch.from_numpy(X.values)
#         X = X.to(self.cur_device)
#         output = self(X)
#         return output.cpu().numpy()
