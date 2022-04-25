# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import copy
import dataclasses
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import torch
from tianshou.data import Collector, VectorReplayBuffer
from tianshou.env import BaseVectorEnv
from tianshou.policy import BasePolicy
from torch.utils.data import Dataset
from utilsd import get_output_dir, get_checkpoint_dir, setup_experiment, use_cuda
from utilsd.experiment import print_config
from utilsd.earlystop import EarlyStop, EarlyStopStatus
from utilsd.logging import print_log
from utilsd.config import RegistryConfig

from neutrader.action import BaseAction
from neutrader.env import Logger, EnvConfig, SIMULATORS, env_factory
from neutrader.data import DataConfig, DataConsumerFactory, data_factory
from neutrader.observation import BaseObservation
from neutrader.reward import BaseReward
from .config import RunConfig, TrainerConfig


class OnPolicyTrainer:
    def __init__(self,
                 checkpoint_dir: Optional[Path] = None,
                 metrics_dir: Optional[Path] = None,
                 preserve_intermediate_checkpoints: bool = False):
        self.checkpoint_dir = checkpoint_dir
        self.metrics_dir = metrics_dir
        self.preserve_intermediate_checkpoints = preserve_intermediate_checkpoints

    def _train_epoch(self, policy: BasePolicy, train_env: BaseVectorEnv, *,
                     buffer_size: int, episode_per_collect: int,
                     batch_size: int, repeat_per_collect: int) -> Dict[str, Any]:
        # 1 epoch = 1 collect
        collector = Collector(policy, train_env, VectorReplayBuffer(buffer_size, len(train_env)))
        policy.train()
        col_result = collector.collect(n_episode=episode_per_collect)
        update_result = policy.update(0, collector.buffer, batch_size=batch_size, repeat=repeat_per_collect)
        return {"collect/" + k: np.mean(v) for k, v in {**col_result, **update_result}.items()}

    def train(self, policy: BasePolicy,
              env_fn: Callable[[Logger, Dataset, bool], Tuple[BaseVectorEnv, DataConsumerFactory]],
              train_dataset: Dataset, val_dataset: Dataset,
              *, max_epoch: int, repeat_per_collect: int,
              batch_size: int, episode_per_collect: int, buffer_size: int = 200000,
              earlystop_patience: int = 5, val_every_n_epoch: int = 1) -> Tuple[Logger, Logger]:
        if self.checkpoint_dir is not None:
            _resume_path = self.checkpoint_dir / "resume.pth"
        else:
            _resume_path = Path("/tmp/resume.pth")

        def _resume():
            nonlocal best_state_dict, cur_epoch
            if _resume_path.exists():
                print_log(f"Resume from checkpoint: {_resume_path}", __name__)
                data = torch.load(_resume_path)
                logger.load_state_dict(data["logger"])
                val_logger.load_state_dict(data["val_logger"])
                earlystop.load_state_dict(data["earlystop"])
                policy.load_state_dict(data["policy"])
                best_state_dict = data["policy_best"]
                if hasattr(policy, "optim"):
                    policy.optim.load_state_dict(data["optim"])
                cur_epoch = data["epoch"]

        def _checkpoint():
            torch.save({
                "logger": logger.state_dict(),
                "val_logger": val_logger.state_dict(),
                "earlystop": earlystop.state_dict(),
                "policy": policy.state_dict(),
                "policy_best": best_state_dict,
                "optim": policy.optim.state_dict() if hasattr(policy, "optim") else None,
                "epoch": cur_epoch
            }, _resume_path)
            print_log(f"Checkpoint saved to {_resume_path}", __name__)

        logger = Logger(episode_per_collect, log_interval=500, tb_prefix="train", count_global="step")
        val_logger = Logger(len(val_dataset), log_interval=2000, tb_prefix="val")
        earlystop = EarlyStop(patience=earlystop_patience)
        cur_epoch = 0
        train_env = data_fn = best_state_dict = None

        _resume()

        try:
            if self.checkpoint_dir is not None and self.preserve_intermediate_checkpoints:
                torch.save(policy.state_dict(), self.checkpoint_dir / f"epoch_{cur_epoch:04d}.pth")

            while cur_epoch < max_epoch:
                cur_epoch += 1
                if train_env is None:
                    train_env, data_fn = env_fn(logger, train_dataset, True)

                logger.reset(f"Train Epoch [{cur_epoch}/{max_epoch}] Episode")
                val_logger.reset(f"Val Epoch [{cur_epoch}/{max_epoch}] Episode")

                collector_res = self._train_epoch(
                    policy, train_env,
                    buffer_size=buffer_size,
                    episode_per_collect=episode_per_collect,
                    batch_size=batch_size,
                    repeat_per_collect=repeat_per_collect)
                logger.write_summary(collector_res)

                if self.checkpoint_dir is not None:
                    torch.save(policy.state_dict(), self.checkpoint_dir / "latest.pth")
                    if self.preserve_intermediate_checkpoints:
                        torch.save(policy.state_dict(), self.checkpoint_dir / f"epoch_{cur_epoch:04d}.pth")

                if cur_epoch == max_epoch or cur_epoch % val_every_n_epoch == 0:
                    data_fn.cleanup()  # do this to save memory
                    train_env = data_fn = None

                    val_result, _ = self.evaluate(policy, env_fn, val_dataset, val_logger)
                    val_logger.global_step = logger.global_step  # sync two loggers
                    val_logger.write_summary()

                    es = earlystop.step(val_result)
                    if es == EarlyStopStatus.BEST:
                        best_state_dict = copy.deepcopy(policy.state_dict())
                        if self.checkpoint_dir is not None:
                            torch.save(best_state_dict, self.checkpoint_dir / "best.pth")
                        pd.DataFrame.from_records(val_logger.logs).to_csv(
                            get_output_dir() / "metrics_val.csv", index=False)
                    elif es == EarlyStopStatus.STOP:
                        break

                _checkpoint()

        finally:
            if data_fn is not None:
                data_fn.cleanup()

        if best_state_dict is not None:
            policy.load_state_dict(best_state_dict)

        return logger, val_logger

    def evaluate(self,
                 policy: BasePolicy,
                 env_fn: Callable[[Logger, Dataset, bool], Tuple[BaseVectorEnv, DataConsumerFactory]],
                 dataset: Dataset,
                 logger: Optional[Logger] = None):
        if logger is None:
            logger = Logger(len(dataset))
        try:
            venv, data_fn = env_fn(logger, dataset, False)
            test_collector = Collector(policy, venv)
            policy.eval()
            test_collector.collect(n_step=int(1E18) * len(venv))
        except StopIteration:
            pass
        finally:
            data_fn.cleanup()

        return logger.summary()["reward"], pd.DataFrame.from_records(logger.logs)


def train_and_test(env_config: EnvConfig,
                   simulator_config: RegistryConfig[SIMULATORS],
                   train_config: TrainerConfig,
                   data_config: DataConfig,
                   action: BaseAction,
                   observation: BaseObservation,
                   reward: BaseReward,
                   policy: BasePolicy):

    def env_fn(logger: Logger, dataset: Dataset, is_training: bool) -> Tuple[BaseVectorEnv, DataConsumerFactory]:
        data_fn = data_factory(data_config, dataset=dataset, infinite=is_training)

        # CAUTION: assumes no parallelism between train and test
        reward.train(is_training)

        venv = env_factory(env_config, simulator_config, action, observation, reward, data_fn, logger)
        return venv, data_fn

    print_log("Loading dataset...", __name__)
    train_dataset = data_config.source.build(subset="train")
    val_dataset = data_config.source.build(subset="valid")
    test_dataset = data_config.source.build(subset="test")
    print_log(f"Dataset loaded. train: {len(train_dataset)} valid: {len(val_dataset)} test: {len(test_dataset)}",
              __name__)

    if train_config.fast_dev_run:
        print_log("Fast running in development mode. Cutting the dataset...", __name__)
        assert min(len(train_dataset), len(val_dataset), len(test_dataset)) >= 100, \
            "For the purpose of fast dev run, all the datasets must have at least 100 samples."
        from torch.utils.data import Subset
        train_dataset = Subset(train_dataset, np.random.permutation(len(train_dataset))[:100])
        val_dataset = Subset(val_dataset, np.random.permutation(len(val_dataset))[:100])
        test_dataset = Subset(test_dataset, np.random.permutation(len(test_dataset))[:100])
        print_log("Dataset cut done.", __name__)

    trainer = OnPolicyTrainer(checkpoint_dir=get_checkpoint_dir(), metrics_dir=get_output_dir(),
                              preserve_intermediate_checkpoints=train_config.preserve_intermediate_checkpoints)

    train_kwargs = dataclasses.asdict(train_config)
    train_kwargs.pop("fast_dev_run")
    train_kwargs.pop("preserve_intermediate_checkpoints")

    train_logger, _ = trainer.train(policy, env_fn, train_dataset, val_dataset, **train_kwargs)

    test_logger = Logger(len(test_dataset), log_interval=2000, prefix="Test Episode", tb_prefix="test")
    test_logger.global_step = train_logger.global_step
    _, test_result = trainer.evaluate(policy, env_fn, test_dataset, test_logger)
    test_logger.write_summary()
    test_result.to_csv(get_output_dir() / "metrics.csv", index=False)
    return test_result


def main(config):
    setup_experiment(config.runtime)
    print_config(config)

    action = config.action.build()
    observation = config.observation.build()
    reward = config.reward.build()

    if config.network is not None:
        network = config.network.build()
        policy = config.policy.build(network=network,
                                     obs_space=observation.observation_space,
                                     action_space=action.action_space)
    else:
        policy = config.policy.build(obs_space=observation.observation_space,
                                     action_space=action.action_space)

    if use_cuda():
        policy.cuda()
    train_and_test(config.env, config.simulator, config.trainer, config.data, action, observation, reward, policy)


if __name__ == "__main__":
    _config = RunConfig.fromcli()
    main(_config)