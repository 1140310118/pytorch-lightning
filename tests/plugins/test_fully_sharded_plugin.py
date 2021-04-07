import os
from unittest import mock

import pytest
import torch

from pytorch_lightning import Trainer
from pytorch_lightning.plugins import FullyShardedNativeMixedPrecisionPlugin, FullyShardedPlugin
from pytorch_lightning.utilities import _FAIRSCALE_FULLY_SHARDED_AVAILABLE
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers.boring_model import BoringModel
from tests.helpers.runif import RunIf

if _FAIRSCALE_FULLY_SHARDED_AVAILABLE:
    from fairscale.nn import auto_wrap, default_auto_wrap_policy, FullyShardedDataParallel


@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
def test_sharded_ddp_choice(tmpdir):
    """
        Test to ensure that plugin is correctly chosen
    """
    trainer = Trainer(
        fast_dev_run=True,
        plugins='ddp_fully_sharded',
    )
    assert isinstance(trainer.accelerator.training_type_plugin, FullyShardedPlugin)


@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
@RunIf(amp_apex=True)
def test_invalid_apex_sharded(tmpdir):
    """
        Test to ensure that we raise an error when we try to use apex and sharded
    """

    model = BoringModel()
    with pytest.raises(MisconfigurationException, match='Sharded Plugins are not supported with Apex AMP'):
        trainer = Trainer(
            fast_dev_run=True,
            plugins='ddp_fully_sharded',
            precision=16,
            amp_backend='apex',
        )

        trainer.fit(model)


@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
@mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "0"})
@mock.patch('torch.cuda.device_count', return_value=1)
@mock.patch('torch.cuda.is_available', return_value=True)
@RunIf(amp_native=True)
def test_ddp_choice_sharded_amp(device_count_mock, mock_cuda_available, tmpdir):
    """
        Test to ensure that plugin native amp plugin is correctly chosen when using sharded
    """
    trainer = Trainer(
        fast_dev_run=True,
        gpus=1,
        precision=16,
        plugins='ddp_fully_sharded',
    )

    assert isinstance(trainer.accelerator.training_type_plugin, FullyShardedPlugin)
    assert isinstance(trainer.accelerator.precision_plugin, FullyShardedNativeMixedPrecisionPlugin)


@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
@RunIf(min_gpus=1, skip_windows=True)
def test_fully_sharded_plugin_checkpoint(tmpdir):
    """
        Test to ensure that checkpoint is saved correctly when using a single GPU.
    """

    class TestModel(BoringModel):

        def configure_optimizers(self):
            return torch.optim.SGD(self.trainer.model.parameters(), lr=0.1)

    model = TestModel()
    trainer = Trainer(
        gpus=1,
        plugins='ddp_fully_sharded',
        fast_dev_run=True,
        precision=16,
    )

    trainer.fit(model)

    _assert_save_equality(tmpdir, trainer)


@pytest.mark.parametrize('automatic_module_wrap', [True, False])
@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
@RunIf(min_gpus=1, skip_windows=True)
def test_fully_sharded_plugin_checkpoint_manual_autowrap(automatic_module_wrap, tmpdir):
    """
        Test to ensure that checkpoint is saved correctly when using automatic, and manual auto_wrap.
    """

    class TestModel(BoringModel):

        def configure_sharded_model(self) -> None:
            if not automatic_module_wrap:

                def wrap_policy(*args, **kwargs):
                    return default_auto_wrap_policy(*args, **kwargs, min_num_params=1)

                self.layer = auto_wrap(self.layer, auto_wrap_policy=wrap_policy)

        def on_train_start(self) -> None:
            assert isinstance(self.layer, FullyShardedDataParallel)
            assert isinstance(self.trainer.model, FullyShardedDataParallel)

        def configure_optimizers(self):
            return torch.optim.SGD(self.trainer.model.parameters(), lr=0.1)

    model = TestModel()

    trainer = Trainer(
        gpus=1,
        plugins=FullyShardedPlugin(automatic_module_wrap=automatic_module_wrap, min_num_params=1),
        fast_dev_run=True,
        precision=16,
    )

    trainer.fit(model)

    _assert_save_equality(tmpdir, trainer)


@pytest.mark.skipif(not _FAIRSCALE_FULLY_SHARDED_AVAILABLE, reason="Fairscale is not available")
@pytest.mark.skipif(
    not os.getenv("PL_RUNNING_SPECIAL_TESTS", '0') == '1', reason="test should be run outside of pytest"
)
@RunIf(min_gpus=2, skip_windows=True)
def test_fully_sharded_plugin_checkpoint_multi_gpu(tmpdir):
    """
        Test to ensure that checkpoint is saved correctly when using multiple GPUs
    """

    class TestModel(BoringModel):

        def configure_optimizers(self):
            return torch.optim.SGD(self.trainer.model.parameters(), lr=0.1)

    model = TestModel()
    trainer = Trainer(
        gpus=2,
        plugins='fully_sharded',
        fast_dev_run=True,
        precision=16,
    )

    trainer.fit(model)

    _assert_save_equality(tmpdir, trainer)


def _assert_save_equality(tmpdir, trainer):
    if trainer.global_rank == 0:

        checkpoint_path = os.path.join(tmpdir, 'model.pt')
        trainer.save_checkpoint(checkpoint_path)
        saved_model = BoringModel.load_from_checkpoint(checkpoint_path)

        # Ensure we gather all shards for comparison
        model_state_dict = trainer.accelerator.training_type_plugin.collate_state_dict()
        # Assert model parameters are identical after loading
        for ddp_param, shard_param in zip(model_state_dict.values(), saved_model.state_dict().values()):
            assert torch.equal(ddp_param.float().cpu(), shard_param)
