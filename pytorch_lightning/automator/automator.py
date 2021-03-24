from weakref import proxy
from collections import Callable
from contextlib import contextmanager
from typing import Any, Union, Optional

import torch.nn as nn
from torch import Tensor
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from pytorch_lightning.accelerators import Accelerator
from pytorch_lightning.trainer.connectors.accelerator_connector import (
    AcceleratorConnector,
)
from pytorch_lightning.utilities import move_data_to_device


class AutomatedOptimizer(Optimizer):
    def __init__(self, optimizer: Optimizer, accelerator: Accelerator):
        super().__init__(params=optimizer.param_groups, defaults={})
        self.optimizer = optimizer
        self._accelerator = accelerator

    def step(self, closure=None, **kwargs: Any):
        # TODO: do precision magic here
        print("running automated step")
        output = self._accelerator.run_optimizer_step(
            self.optimizer,
            lambda_closure=closure,
            **kwargs,
        )
        return output


class AutomatedModel(nn.Module):

    def __init__(self, module: nn.Module, accelerator: Accelerator):
        super().__init__()
        self._module = module
        self._accelerator = accelerator

    @property
    def module(self):
        return self._module

    def forward(self, *args, **kwargs):
        with self._accelerator.forward_context():
            output = self.module.forward(*args, **kwargs)
        return output


class Automator:
    def __init__(
        self,
        accelerator=None,
        plugin=None,
        gpus=None,
        tpus=None,
        num_processes=1,
        num_nodes=1,
        precision=32,
        amp_backend: str = "native",
        amp_level: str = "O2",
    ):
        backend_connector = AcceleratorConnector(
            gpus=gpus,
            tpu_cores=tpus,
            num_processes=num_processes,
            distributed_backend=accelerator,
            num_nodes=num_nodes,
            precision=precision,
            amp_type=amp_backend,
            amp_level=amp_level,
            plugins=plugin,
            # TODO:
            deterministic=False,
            sync_batchnorm=False,
            benchmark=False,
            replace_sampler_ddp=True,
            auto_select_gpus=False,
        )
        self.accelerator = backend_connector.select_accelerator()

    @property
    def training_type_plugin(self):
        return self.accelerator.training_type_plugin

    @property
    def precision_plugin(self):
        return self.accelerator.precision_plugin

    @property
    def device(self):
        # the device on the local rank
        return self.training_type_plugin.root_device

    def setup(self, *objects: Union[nn.Module, Optimizer, DataLoader]):
        # wrap all objects passed in and return them in the same order
        wrapped_objects = []
        for obj in objects:
            if isinstance(obj, nn.Module):
                wrapped_objects.extend(self.setup_model(obj))
            if isinstance(obj, Optimizer):
                wrapped_objects.extend(self.setup_optimizer(obj))
            if isinstance(obj, DataLoader):
                wrapped_objects.extend(self.setup_dataloader(obj))

        if len(wrapped_objects) == 1:
            return wrapped_objects[0]
        return wrapped_objects

    def setup_model(self, *models: nn.Module):
        # user can call this method independently instead of the general purpose setup method
        models = [
            AutomatedModel(module=self.training_type_plugin.setup_model(model), accelerator=self.accelerator)
            for model in models
        ]
        return models

    def setup_optimizer(self, *optimizers: Optimizer):
        # user can call this method independently instead of the general purpose setup method
        # TODO: let plugin setup optimizer too?
        optimizers = [
            AutomatedOptimizer(optimizer=optimizer, accelerator=self.accelerator)
            for optimizer in optimizers
        ]
        return optimizers

    def setup_dataloader(self, *dataloaders: DataLoader):
        # user can call this method independently instead of the general purpose setup method
        dataloaders = [
            self.training_type_plugin.setup_dataloader(dataloader)
            for dataloader in dataloaders
        ]
        return dataloaders

    def backward(self, tensor: Tensor, *args, **kwargs):
        # user will call automator.backward(loss) instead of loss.backward()
        self.accelerator.run_backward(tensor, *args, **kwargs)

    @contextmanager
    def forward_context(self):
        with self.accelerator.forward_context():
            yield

    # @contextmanager
    # def backward_context(self, *args, **kwargs):
    #     yield
    #
    # @contextmanager
    # def optimizer_step_context(self, *args, **kwargs):
    #     # necessary for deepspeed + scaling
    #     yield

    def to_device(self, obj: Union[nn.Module, Tensor]) -> Union[nn.Module, Tensor]:
        if isinstance(obj, nn.Module):
            return obj.to(self.device)
        return move_data_to_device(obj, device=self.device)

    def sync(self, data: Any) -> Any:
        pass

    def reduce_data(self, data: Any) -> Any:
        return self.training_type_plugin.reduce(data)

    def reduce_decision(self, decision: bool) -> bool:
        return self.training_type_plugin.reduce_boolean_decision(decision)

    def broadcast_decision(self, decision: bool):
        # return self.training_type_plugin.broadcast_boolean_decision(decision)
        return False

    def save_checkpoint(self, filepath):
        pass

    def execute_on_rank(self, func: Callable, rank: int):
        pass