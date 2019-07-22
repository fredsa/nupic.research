# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2019, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

from collections import defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as schedulers


class BaseModel:
    """Base model, with training loops and logging functions."""

    def __init__(self, network, config=None):
        defaults = dict(
            optim_alg="SGD",
            learning_rate=0.1,
            momentum=0.9,
            device="cpu",
            lr_scheduler=False,
            debug_sparse=False,
            debug_weights=False,
            start_sparse=None,
            end_sparse=None,
            pruning_interval=1,
            log_images=False,
            flip=False,
            weight_prune_perc=0,
            grad_prune_perc=0,
            test_noise=False,
        )
        defaults.update(config or {})
        self.__dict__.update(defaults)

        # init remaining
        self.device = torch.device(self.device)
        self.network = network.to(self.device)

    def setup(self):

        # init optimizer
        if self.optim_alg == "Adam":
            self.optimizer = optim.Adam(
                self.network.parameters(), lr=self.learning_rate
            )
        elif self.optim_alg == "SGD":
            self.optimizer = optim.SGD(
                self.network.parameters(), lr=self.learning_rate, momentum=self.momentum
            )

        # add a learning rate scheduler
        if self.lr_scheduler:
            self.lr_scheduler = schedulers.MultiStepLR(
                self.optimizer, milestones=self.lr_milestones, gamma=self.lr_gamma
            )

        # init loss function
        self.loss_func = nn.CrossEntropyLoss()

    def run_epoch(self, dataset, epoch):
        self.current_epoch = epoch + 1
        self.log = {}
        self.network.train()
        self._run_one_pass(dataset.train_loader, train=True)
        self.network.eval()
        self._run_one_pass(dataset.test_loader, train=False)
        self._post_epoch_updates(dataset)

        return self.log

    def _post_epoch_updates(self, dataset=None):

        # run one additional testing epoch for noise
        if self.test_noise:
            self._run_one_pass(dataset.noise_loader, train=False, noise=True)

        # update learning rate
        if self.lr_scheduler:
            self.lr_scheduler.step()

    def _run_one_pass(self, loader, train=True, noise=False):
        epoch_loss = 0
        correct = 0
        for inputs, targets in loader:
            # setup for training
            inputs = inputs.to(self.device)
            targets = targets.to(self.device)
            self.optimizer.zero_grad()
            # training loop
            with torch.set_grad_enabled(train):
                # forward + backward + optimize
                outputs = self.network(inputs)
                _, preds = torch.max(outputs, 1)
                correct += torch.sum(targets == preds).item()
                loss = self.loss_func(outputs, targets)
                if train:
                    loss.backward()
                    self.optimizer.step()

            # keep track of loss
            epoch_loss += loss.item() * inputs.size(0)

        # store loss and acc at each pass
        loss = epoch_loss / len(loader.dataset)
        acc = correct / len(loader.dataset)
        if train:
            self.log["train_loss"] = loss
            self.log["train_acc"] = acc
        else:
            if noise:
                self.log["noise_loss"] = loss
                self.log["noise_acc"] = acc
            else:
                self.log["val_loss"] = loss
                self.log["val_acc"] = acc

        if train and self.debug_weights:
            self._log_weights()

    @staticmethod
    def has_params(module):
        if isinstance(module, nn.Linear):
            return "linear"
        elif isinstance(module, nn.Conv2d):
            return "conv"

    def _log_weights(self):
        """Log weights for all layers which have params."""
        if "param_layers" not in self.__dict__:
            self.param_layers = defaultdict(list)
            for m, ltype in [(m, self.has_params(m)) for m in self.network.modules()]:
                if ltype:
                    self.param_layers[ltype].append(m)

        # log stats (mean and weight instead of standard distribution)
        for ltype, layers in self.param_layers.items():
            for idx, m in enumerate(layers):
                # keep track of mean and std of weights
                self.log[ltype + "_" + str(idx) + "_mean"] = torch.mean(m.weight).item()
                self.log[ltype + "_" + str(idx) + "_std"] = torch.std(m.weight).item()

    def save(self):
        pass

    def restore(self):
        pass


class SparseModel(BaseModel):
    """Sparsity implemented by:

    - Masking on the weights
    - Zeroing out gradients in backprop before optimizer steps
    """

    def setup(self):
        super(SparseModel, self).setup()

        # add specific defaults
        if "epsilon" not in self.__dict__:
            self.epsilon = 20

        with torch.no_grad():
            # calculate sparsity masks
            self.masks = []
            self.num_params = []  # added for paper implementation

            # define sparse modules
            self.sparse_modules = []
            for m in list(self.network.modules())[self.start_sparse : self.end_sparse]:
                if self.has_params(m):
                    self.sparse_modules.append(m)

            # initialize masks
            for m in self.sparse_modules:
                shape = m.weight.shape
                on_perc = self.epsilon * np.sum(shape) / np.prod(shape)
                mask = (torch.rand(shape) < on_perc).float().to(self.device)
                m.weight.data *= mask
                self.masks.append(mask)
                self.num_params.append(torch.sum(mask).item())

    def _run_one_pass(self, loader, train, noise=False):
        """TODO: reimplement by calling super and passing a hook"""
        epoch_loss = 0
        epoch_correct = 0
        for inputs, targets in loader:
            # setup for training
            inputs = inputs.to(self.device)
            targets = targets.to(self.device)
            self.optimizer.zero_grad()

            # forward + backward + optimize
            with torch.set_grad_enabled(train):
                outputs = self.network(inputs)
                _, preds = torch.max(outputs, 1)
                epoch_correct += torch.sum(targets == preds).item()
                loss = self.loss_func(outputs, targets)
                if train:
                    loss.backward()
                    # zero the gradients for dead connections
                    for mask, m in zip(self.masks, self.sparse_modules):
                        m.weight.grad *= mask
                    self.optimizer.step()

            # keep track of loss and accuracy
            epoch_loss += loss.item() * inputs.size(0)

        # store loss and acc at each pass
        loss = epoch_loss / len(loader.dataset)
        acc = epoch_correct / len(loader.dataset)
        if train:
            self.log["train_loss"] = loss
            self.log["train_acc"] = acc
        else:
            if noise:
                self.log["noise_loss"] = loss
                self.log["noise_acc"] = acc
            else:
                self.log["val_loss"] = loss
                self.log["val_acc"] = acc

        if train and self.debug_weights:
            self._log_weights()

        # add monitoring of sparse levels
        if train and self.debug_sparse:
            self._log_sparse_levels()

    def _log_sparse_levels(self):
        with torch.no_grad():
            for idx, m in enumerate(self.sparse_modules):
                zero_mask = m.weight == 0
                zero_count = torch.sum(zero_mask.int()).item()
                size = np.prod(m.weight.shape)
                log_name = "sparse_level_l" + str(idx)
                self.log[log_name] = 1 - zero_count / size

                # log image as well
                if self.log_images:
                    if self.has_params(m) == "conv":
                        ratio = 255 / np.prod(m.weight.shape[2:])
                        heatmap = (
                            torch.sum(m.weight, dim=[2, 3]).float() * ratio
                        ).int()
                        self.log["img_" + log_name] = heatmap.tolist()


class SET(SparseModel):
    """
    Implementation of SET with a more efficient approach of adding new
    weights (vectorized) The overhead in computation is 10x smaller compared to
    the original version.
    """

    def setup(self):
        super(SET, self).setup()

        # initialize data structure keep track of added synapses
        self.added_synapses = [None for m in self.masks]

    def _run_one_pass(self, loader, train, noise=False):
        super(SET, self)._run_one_pass(loader, train, noise)
        if train:
            self.reinitialize_weights()

    def prune(self, weight, num_params, zeta=0.3):
        """
        Calculate new weight based on SET approach weight vectorized version
        aimed at keeping the mask with the similar level of sparsity.
        """
        with torch.no_grad():

            # NOTE: another approach is counting how many numbers to prune
            # calculate thresholds and decay weak connections
            weight_pos = weight[weight > 0]
            pos_threshold, _ = torch.kthvalue(weight_pos, int(zeta * len(weight_pos)))
            weight_neg = weight[weight < 0]
            neg_threshold, _ = torch.kthvalue(
                weight_neg, int((1 - zeta) * len(weight_neg))
            )
            prune_mask = ((weight >= pos_threshold) | (weight <= neg_threshold)).to(
                self.device
            )

            # change mask to add new weight
            num_add = num_params - torch.sum(prune_mask).item()
            current_sparsity = torch.sum(weight == 0).item()
            p_add = num_add / max(current_sparsity, num_add)  # avoid div by zero
            probs = torch.rand(weight.shape).to(self.device) < p_add
            new_synapses = probs & (weight == 0)
            new_mask = prune_mask | new_synapses

        # track added connections
        return new_mask, prune_mask, new_synapses

    def reinitialize_weights(self):
        """Reinitialize weights."""
        for idx, m in enumerate(self.sparse_modules):
            layer_weights = m.weight.clone().detach()
            new_mask, prune_mask, new_synapses = self.prune(
                layer_weights, self.num_params[idx]
            )
            with torch.no_grad():
                self.masks[idx] = new_mask.float()
                m.weight.data *= prune_mask.float()

                # keep track of added synapes
                if self.debug_sparse:
                    self.log["added_synapses_l" + str(idx)] = torch.sum(
                        new_synapses
                    ).item()
                    if self.added_synapses[idx] is not None:
                        total_added = torch.sum(self.added_synapses[idx]).item()
                        surviving = torch.sum(
                            self.added_synapses[idx] & prune_mask
                        ).item()
                        if total_added:
                            self.log["surviving_synapses_l" + str(idx)] = (
                                surviving / total_added
                            )
                self.added_synapses[idx] = new_synapses

        # keep track of mask sizes when debugging
        if self.debug_sparse:
            for idx, m in enumerate(self.masks):
                self.log["mask_sizes_l" + str(idx)] = torch.sum(m).item()


class DSNN(SparseModel):
    """
    Dynamically sparse neural networks.
    Our improved version of SET
    """

    def setup(self):
        super(DSNN, self).setup()
        self.added_synapses = [None for m in self.masks]
        self.last_gradients = [None for m in self.masks]

        # initializae sign to 1 if to be flipped later
        self.prune_grad_sign = -1
        if self.flip:
            self.prune_grad_sign = 1
            self.flip_epoch = 30

    def _post_epoch_updates(self, dataset=None):
        super(DSNN, self)._post_epoch_updates(dataset)

        # flip at a fixed interval
        if self.flip:
            if self.current_epoch == self.flip_epoch and self.prune_grad_sign == 1:
                self.prune_grad_sign = -1

        # update only when learning rate updates
        # froze this for now
        # if self.current_epoch in self.lr_milestones:
        #     # decay pruning interval, inversely proportional with learning rate
        #     self.pruning_interval = max(self.pruning_interval,
        #         int((self.pruning_interval * (1/self.lr_gamma))/3))

    def _run_one_pass(self, loader, train, noise=False):
        """TODO: reimplement by calling super and passing a hook"""
        epoch_loss = 0
        epoch_correct = 0
        for inputs, targets in loader:
            # setup for training
            inputs = inputs.to(self.device)
            targets = targets.to(self.device)
            self.optimizer.zero_grad()

            # forward + backward + optimize
            with torch.set_grad_enabled(train):
                outputs = self.network(inputs)
                _, preds = torch.max(outputs, 1)
                epoch_correct += torch.sum(targets == preds).item()
                loss = self.loss_func(outputs, targets)
                if train:
                    loss.backward()
                    # zero the gradients for dead connections
                    for idx, (mask, m) in enumerate(
                        zip(self.masks, self.sparse_modules)
                    ):
                        m.weight.grad *= mask
                        # save gradients before any operation
                        # TODO: will need to integrate over several epochs later
                        self.last_gradients[idx] = m.weight.grad

                    self.optimizer.step()

            # keep track of loss and accuracy
            epoch_loss += loss.item() * inputs.size(0)

        # store loss and acc at each pass
        loss = epoch_loss / len(loader.dataset)
        acc = epoch_correct / len(loader.dataset)
        if train:
            self.log["train_loss"] = loss
            self.log["train_acc"] = acc
        else:
            if noise:
                self.log["noise_loss"] = loss
                self.log["noise_acc"] = acc
            else:
                self.log["val_loss"] = loss
                self.log["val_acc"] = acc

        # add monitoring of weights
        if train and self.debug_weights:
            self._log_weights()

        # add monitoring of sparse levels
        if train and self.debug_sparse:
            self._log_sparse_levels()

        # dynamically decide pruning interval
        if train:
            # no dynamic interval at this moment
            # if self.current_epoch % self.pruning_interval == 0:
            self.reinitialize_weights()

    def prune(self, weight, grad, num_params, zeta=0.30, idx=0):
        """
        Calculate new weight based on SET approach weight vectorized version
        aimed at keeping the mask with the similar level of sparsity.

        Changes:
        - higher zeta
        - two masks: one based on weights, another based on gradients
        """
        with torch.no_grad():

            # calculate weight mask
            zeta = self.weight_prune_perc
            weight_pos = weight[weight > 0]
            pos_threshold, _ = torch.kthvalue(
                weight_pos, max(int(zeta * len(weight_pos)), 1)
            )
            weight_neg = weight[weight < 0]
            neg_threshold, _ = torch.kthvalue(
                weight_neg, max(int((1 - zeta) * len(weight_neg)), 1)
            )
            weight_keep_mask = (weight >= pos_threshold) | (weight <= neg_threshold)
            weight_keep_mask.to(self.device)
            self.log["weight_keep_mask_l" + str(idx)] = torch.sum(
                weight_keep_mask
            ).item()

            # calculate gradient mask
            kappa = self.grad_prune_perc
            grad = grad * self.prune_grad_sign * torch.sign(weight)
            deltas = grad.view(-1)
            grad_treshold, _ = torch.kthvalue(deltas, max(int(kappa * len(deltas)), 1))
            grad_keep_mask = (grad >= grad_treshold).to(self.device)
            # keep only those which are in the original weight matrix
            grad_keep_mask = grad_keep_mask & (weight != 0)
            self.log["grad_keep_mask_l" + str(idx)] = torch.sum(grad_keep_mask).item()

            # combine both masks
            keep_mask = weight_keep_mask & grad_keep_mask

            # change mask to add new weight
            num_add = num_params - torch.sum(keep_mask).item()
            self.log["missing_weights_l" + str(idx)] = num_add
            current_sparsity = torch.sum(weight == 0).item()
            self.log["zero_weights_l" + str(idx)] = current_sparsity
            p_add = num_add / max(current_sparsity, 1)  # avoid div by zero
            probs = torch.rand(weight.shape).to(self.device) < p_add
            new_synapses = probs & (weight == 0)
            new_mask = keep_mask | new_synapses
            self.log["added_synapses_l" + str(idx)] = torch.sum(new_synapses).item()

        # track added connections
        return new_mask, keep_mask, new_synapses

    def reinitialize_weights(self):
        """Reinitialize weights."""
        for idx, (m, grad) in enumerate(zip(self.sparse_modules, self.last_gradients)):
            new_mask, keep_mask, new_synapses = self.prune(
                m.weight.clone().detach(), grad, self.num_params[idx], idx=idx
            )
            with torch.no_grad():
                self.masks[idx] = new_mask.float()
                m.weight.data *= keep_mask.float()

                # keep track of added synapes
                if self.debug_sparse:
                    # count new synapses at this round
                    # total_new = torch.sum(new_synapses).item()
                    # total = np.prod(new_synapses.shape)
                    # self.log["added_synapses_l" + str(idx)] = total_new
                    # count how many synapses from last round have survived
                    if self.added_synapses[idx] is not None:
                        total_added = torch.sum(self.added_synapses[idx]).item()
                        surviving = torch.sum(
                            self.added_synapses[idx] & keep_mask
                        ).item()
                        if total_added:
                            self.log["surviving_synapses_l" + str(idx)] = (
                                surviving / total_added
                            )
                # keep track of new synapses to count surviving on next round
                self.added_synapses[idx] = new_synapses

        # keep track of mask sizes when debugging
        if self.debug_sparse:
            for idx, m in enumerate(self.masks):
                self.log["mask_sizes_l" + str(idx)] = torch.sum(m).item()