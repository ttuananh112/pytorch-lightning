# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from contextlib import redirect_stderr
from io import StringIO
from re import escape

import pytest
from torch.utils.data import DataLoader, DistributedSampler
from torch.utils.data.sampler import BatchSampler, Sampler, SequentialSampler

from pytorch_lightning import Trainer
from pytorch_lightning.utilities.enums import DistributedType
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers import BoringModel, RandomDataset
from tests.helpers.runif import RunIf


@RunIf(skip_windows=True, min_torch="1.7.0")
@pytest.mark.parametrize("mode", (1, 2, 3))
def test_replace_distributed_sampler(tmpdir, mode):
    class IndexedRandomDataset(RandomDataset):
        def __getitem__(self, index):
            return self.data[index]

    class CustomDataLoader(DataLoader):
        def __init__(self, num_features, dataset, *args, **kwargs):
            self.num_features = num_features
            super().__init__(dataset, *args, **kwargs)

    class FailureCustomDataLoader(DataLoader):
        def __init__(self, num_features, dataset, *args, **kwargs):
            super().__init__(dataset, *args, **kwargs)

    class CustomBatchSampler(BatchSampler):
        pass

    class TestModel(BoringModel):
        def __init__(self, numbers_test_dataloaders, mode):
            super().__init__()
            self._numbers_test_dataloaders = numbers_test_dataloaders
            self._mode = mode

        def test_step(self, batch, batch_idx, dataloader_idx=None):
            return super().test_step(batch, batch_idx)

        def on_test_start(self) -> None:
            dataloader = self.trainer.test_dataloaders[0]
            assert isinstance(dataloader, CustomDataLoader)
            batch_sampler = dataloader.batch_sampler
            if self._mode == 2:
                assert isinstance(batch_sampler, CustomBatchSampler)
                # the batch_size is set on the batch sampler
                assert dataloader.batch_size is None
            elif self._mode == 3:
                assert type(batch_sampler) is BatchSampler
                assert dataloader.batch_size == self._mode
            assert batch_sampler.batch_size == self._mode
            assert batch_sampler.drop_last
            # the sampler has been replaced
            assert isinstance(batch_sampler.sampler, DistributedSampler)

        def create_dataset(self):
            dataset = IndexedRandomDataset(32, 64)
            if self._mode == 1:
                # this case will raise an error
                return FailureCustomDataLoader(32, dataset)
            if self._mode == 2:
                # with a custom batch sampler
                batch_sampler = CustomBatchSampler(SequentialSampler(dataset), batch_size=2, drop_last=True)
                return CustomDataLoader(32, dataset, batch_sampler=batch_sampler)
            elif self._mode == 3:
                # with no batch sampler provided
                return CustomDataLoader(32, dataset, batch_size=3, drop_last=True)

        def test_dataloader(self):
            return [self.create_dataset()] * self._numbers_test_dataloaders

    model = TestModel(2, mode)
    model.test_epoch_end = None

    trainer = Trainer(
        default_root_dir=tmpdir, limit_test_batches=2, plugins="ddp_find_unused_parameters_false", num_processes=1
    )
    if mode == 1:
        match = escape("missing attributes are ['num_features']")
        with pytest.raises(MisconfigurationException, match=match):
            trainer.test(model)
    else:
        trainer.test(model)


class TestSpawnBoringModel(BoringModel):
    def __init__(self, num_workers):
        super().__init__()
        self.num_workers = num_workers

    def train_dataloader(self):
        return DataLoader(RandomDataset(32, 64), num_workers=self.num_workers)

    def on_pretrain_routine_start(self):
        self._resout = StringIO()
        self.ctx = redirect_stderr(self._resout)
        self.ctx.__enter__()

    def on_train_end(self):
        def _get_warning_msg():
            dl = self.trainer.train_dataloader.loaders
            if hasattr(dl, "persistent_workers"):
                if self.num_workers == 0:
                    warn_str = "Consider setting num_workers>0 and persistent_workers=True"
                else:
                    warn_str = "Consider setting persistent_workers=True"
            else:
                warn_str = "Consider setting accelerator=ddp"

            return warn_str

        if self.trainer.is_global_zero:
            self.ctx.__exit__(None, None, None)
            msg = self._resout.getvalue()
            warn_str = _get_warning_msg()
            assert warn_str in msg


@RunIf(skip_windows=True)
@pytest.mark.parametrize("num_workers", [0, 1])
def test_dataloader_warnings(tmpdir, num_workers):
    trainer = Trainer(default_root_dir=tmpdir, accelerator="ddp_spawn", num_processes=2, fast_dev_run=4)
    assert trainer.accelerator_connector._distrib_type == DistributedType.DDP_SPAWN
    trainer.fit(TestSpawnBoringModel(num_workers))


def test_update_dataloader_raises():
    trainer = Trainer()
    with pytest.raises(ValueError, match="needs to subclass `torch.utils.data.DataLoader"):
        trainer._update_dataloader(object(), object(), mode="fit")


def test_dataloaders_with_missing_keyword_arguments():
    trainer = Trainer()
    ds = RandomDataset(10, 20)

    class TestDataLoader(DataLoader):
        def __init__(self, dataset):
            super().__init__(dataset)

    loader = TestDataLoader(ds)
    sampler = SequentialSampler(ds)
    match = escape("missing arguments are ['batch_sampler', 'sampler', 'shuffle']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="fit")
    match = escape("missing arguments are ['batch_sampler', 'batch_size', 'drop_last', 'sampler', 'shuffle']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="predict")

    class TestDataLoader(DataLoader):
        def __init__(self, dataset, *args, **kwargs):
            super().__init__(dataset)

    loader = TestDataLoader(ds)
    sampler = SequentialSampler(ds)
    trainer._update_dataloader(loader, sampler, mode="fit")
    trainer._update_dataloader(loader, sampler, mode="predict")

    class TestDataLoader(DataLoader):
        def __init__(self, *foo, **bar):
            super().__init__(*foo, **bar)

    loader = TestDataLoader(ds)
    sampler = SequentialSampler(ds)
    trainer._update_dataloader(loader, sampler, mode="fit")
    trainer._update_dataloader(loader, sampler, mode="predict")

    class TestDataLoader(DataLoader):
        def __init__(self, num_feat, dataset, *args, shuffle=False):
            self.num_feat = num_feat
            super().__init__(dataset)

    loader = TestDataLoader(1, ds)
    sampler = SequentialSampler(ds)
    match = escape("missing arguments are ['batch_sampler', 'sampler']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="fit")
    match = escape("missing arguments are ['batch_sampler', 'batch_size', 'drop_last', 'sampler']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="predict")

    class TestDataLoader(DataLoader):
        def __init__(self, num_feat, dataset, **kwargs):
            self.feat_num = num_feat
            super().__init__(dataset)

    loader = TestDataLoader(1, ds)
    sampler = SequentialSampler(ds)
    match = escape("missing attributes are ['num_feat']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="fit")
    match = escape("missing attributes are ['num_feat']")
    with pytest.raises(MisconfigurationException, match=match):
        trainer._update_dataloader(loader, sampler, mode="predict")


def test_update_dataloader_with_multiprocessing_context():
    """This test verifies that replace_sampler conserves multiprocessing context."""
    train = RandomDataset(32, 64)
    context = "spawn"
    train = DataLoader(train, batch_size=32, num_workers=2, multiprocessing_context=context, shuffle=True)
    trainer = Trainer()
    new_data_loader = trainer._update_dataloader(train, SequentialSampler(train.dataset))
    assert new_data_loader.multiprocessing_context == train.multiprocessing_context


def test_dataloader_reinit_for_subclass():
    class CustomDataLoader(DataLoader):
        def __init__(
            self,
            dataset,
            batch_size=1,
            shuffle=False,
            sampler=None,
            batch_sampler=None,
            num_workers=0,
            collate_fn=None,
            pin_memory=False,
            drop_last=False,
            timeout=0,
            worker_init_fn=None,
            dummy_kwarg=None,
        ):
            super().__init__(
                dataset,
                batch_size,
                shuffle,
                sampler,
                batch_sampler,
                num_workers,
                collate_fn,
                pin_memory,
                drop_last,
                timeout,
                worker_init_fn,
            )
            self.dummy_kwarg = dummy_kwarg
            self.something_unrelated = 1

    trainer = Trainer(num_processes=2, strategy="ddp_spawn")

    class CustomDummyObj:
        sampler = None

    result = trainer.prepare_dataloader(CustomDummyObj(), shuffle=True)
    assert isinstance(result, CustomDummyObj), "Wrongly reinstantiated data loader"

    dataset = list(range(10))
    result = trainer.prepare_dataloader(CustomDataLoader(dataset), shuffle=True)
    assert isinstance(result, DataLoader)
    assert isinstance(result, CustomDataLoader)
    assert result.dummy_kwarg is None

    # Shuffled DataLoader should also work
    result = trainer.prepare_dataloader(CustomDataLoader(dataset, shuffle=True), shuffle=True)
    assert isinstance(result, DataLoader)
    assert isinstance(result, CustomDataLoader)
    assert result.dummy_kwarg is None

    class CustomSampler(Sampler):
        pass

    # Should raise an error if existing sampler is being replaced
    dataloader = CustomDataLoader(dataset, sampler=CustomSampler(dataset))
    with pytest.raises(MisconfigurationException, match="will be replaced  by `DistributedSampler`"):
        trainer.prepare_dataloader(dataloader, shuffle=True)


def test_loader_detaching():
    """Checks that the loader has been resetted after the entrypoint."""

    class LoaderTestModel(BoringModel):
        def training_step(self, batch, batch_idx):
            assert len(self.trainer.train_dataloader.loaders) == 10
            return super().training_step(batch, batch_idx)

        def validation_step(self, batch, batch_idx):
            assert len(self.trainer.val_dataloaders[0]) == 10
            return super().validation_step(batch, batch_idx)

        def test_step(self, batch, batch_idx):
            assert len(self.trainer.test_dataloaders[0]) == 10
            return super().test_step(batch, batch_idx)

        def predict_step(self, batch, batch_idx, dataloader_idx=None):
            assert len(self.trainer.predict_dataloaders[0]) == 10
            return super().predict_step(batch, batch_idx, dataloader_idx=dataloader_idx)

    loader = DataLoader(RandomDataset(32, 10), batch_size=1)

    model = LoaderTestModel()

    assert len(model.train_dataloader()) == 64
    assert len(model.val_dataloader()) == 64
    assert len(model.predict_dataloader()) == 64
    assert len(model.test_dataloader()) == 64

    trainer = Trainer(fast_dev_run=1)
    trainer.fit(model, loader, loader)

    assert len(model.train_dataloader()) == 64
    assert len(model.val_dataloader()) == 64
    assert len(model.predict_dataloader()) == 64
    assert len(model.test_dataloader()) == 64

    trainer.validate(model, loader)

    assert len(model.train_dataloader()) == 64
    assert len(model.val_dataloader()) == 64
    assert len(model.predict_dataloader()) == 64
    assert len(model.test_dataloader()) == 64

    trainer.predict(model, loader)

    assert len(model.train_dataloader()) == 64
    assert len(model.val_dataloader()) == 64
    assert len(model.predict_dataloader()) == 64
    assert len(model.test_dataloader()) == 64

    trainer.test(model, loader)

    assert len(model.train_dataloader()) == 64
    assert len(model.val_dataloader()) == 64
    assert len(model.predict_dataloader()) == 64
    assert len(model.test_dataloader()) == 64
