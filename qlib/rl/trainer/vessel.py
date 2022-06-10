# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from __future__ import annotations

import logging
import weakref
from typing import Callable, Generic, Iterable, TYPE_CHECKING, Sequence, Any, TypeVar

import numpy as np
from tianshou.data import Collector, VectorReplayBuffer
from tianshou.env import BaseVectorEnv
from tianshou.policy import BasePolicy

from qlib.constant import INF
from qlib.rl.interpreter import StateType, ActType, ObsType, PolicyActType
from qlib.rl.simulator import InitialStateType, Simulator
from qlib.rl.interpreter import StateInterpreter, ActionInterpreter
from qlib.rl.reward import Reward
from qlib.rl.utils import DataQueue, EnvWrapper, FiniteEnvType, LogCollector, LogWriter, finite_env_factory
from qlib.log import get_module_logger
from qlib.rl.utils.finite_env import FiniteVectorEnv
from qlib.typehint import Literal

if TYPE_CHECKING:
    from .trainer import Trainer


T = TypeVar('T')
_logger = logging.getLogger(__name__)

class SeedIteratorNotAvailable(BaseException):
    pass


class TrainingVesselBase(Generic[InitialStateType, StateType, ActType, ObsType, PolicyActType]):
    """A ship that contains simulator, interpreter, and policy, will be sent to trainer.
    This class controls algorithm-related parts of training, while trainer is responible for runtime part.
    
    The ship also defines the most important logic of the core training part,
    and (optionally) some callbacks to insert customized logics at specific events.
    """

    simulator_fn: Callable[[InitialStateType], Simulator[InitialStateType, StateType, ActType]]
    state_interpreter: StateInterpreter[StateType, ObsType]
    action_interpreter: ActionInterpreter[StateType, PolicyActType, ActType]
    policy: BasePolicy
    reward: Reward
    trainer: Trainer

    def assign_trainer(self, trainer: Trainer) -> None:
        self.trainer = weakref.proxy(trainer)  # type: ignore

    def train_seed_iterator(self) -> Iterable[InitialStateType]:
        """Override this to create a seed iterator for training."""
        raise SeedIteratorNotAvailable('Seed iterator for training is not available.')

    def val_seed_iterator(self) -> Iterable[InitialStateType]:
        """Override this to create a seed iterator for validation."""
        raise SeedIteratorNotAvailable('Seed iterator for validation is not available.')

    def test_seed_iterator(self) -> Iterable[InitialStateType]:
        """Override this to create a seed iterator for testing."""
        raise SeedIteratorNotAvailable('Seed iterator for testing is not available.')

    def train(self, vector_env: BaseVectorEnv) -> dict[str, Any]:
        """Implement this to train one iteration. In RL, one iteration usually refers to one collect."""
        raise NotImplementedError()

    def validate(self, vector_env: FiniteVectorEnv) -> dict[str, Any]:
        """Implement this to validate the policy once."""
        raise NotImplementedError()

    def test(self, vector_env: FiniteVectorEnv) -> dict[str, Any]:
        """Implement this to evaluate the policy on test environment once."""
        raise NotImplementedError()

    def state_dict(self) -> dict:
        """Return a checkpoint of current vessel state."""
        return {
            "policy": self.policy.state_dict()
        }

    def load_state_dict(self, state_dict: dict) -> None:
        """Restore a checkpoint from a previously saved state dict."""
        self.policy.load_state_dict(state_dict)


class TrainingVessel(TrainingVesselBase):
    """The default implementation of training vessel.
    
    ``__init__`` accepts a sequence of initial states so that iterator can be created.
    ``train``, ``validate``, ``test`` each do one collect (and also update in train).

    Extra hyper-parameters (only used in train) include:

    - ``buffer_size``: Size of replay buffer.
    - ``episode_per_iter``: Episodes per collect at training.
    - ``update_per_iter``: Number of updates happening after each collect. This is used in ``repeat`` parameter in ``policy.update``.
    - ``batch_size``: Batch size in ``self.policy.update`` after each collect.
    """

    def __init__(
        self, *,
        simulator_fn: Callable[[InitialStateType], Simulator[InitialStateType, StateType, ActType]],
        state_interpreter: StateInterpreter[StateType, ObsType],
        action_interpreter: ActionInterpreter[StateType, PolicyActType, ActType],
        policy: BasePolicy,
        reward: Reward,
        train_initial_states: Sequence[InitialStateType] | None,
        val_initial_states: Sequence[InitialStateType] | None,
        test_initial_states: Sequence[InitialStateType] | None,
        buffer_size: int,
        episode_per_iter: int,
        update_per_iter: int,
        batch_size: int
    ):
        self.simulator_fn = simulator_fn
        self.state_interpreter = state_interpreter
        self.action_interpreter = action_interpreter
        self.policy = policy
        self.reward = reward
        self.train_initial_states = train_initial_states
        self.val_initial_states = val_initial_states
        self.test_initial_states = test_initial_states
        self.buffer_size = buffer_size
        self.episode_per_iter = episode_per_iter
        self.update_per_iter = update_per_iter
        self.batch_size = batch_size

    def train_seed_iterator(self) -> Iterable[InitialStateType]:
        if self.train_initial_states is not None:
            _logger.info("Training initial states collection size: %d", len(self.train_initial_states))
            # Implement fast_dev_run here.
            train_initial_states = self._random_subset('train', self.train_initial_states, self.trainer.fast_dev_run)
            return DataQueue(train_initial_states, repeat=-1, shuffle=True)
        return super().train_seed_iterator()

    def val_seed_iterator(self) -> Iterable[InitialStateType]:
        if self.val_initial_states is not None:
            _logger.info("Validation initial states collection size: %d", len(self.val_initial_states))
            val_initial_states = self._random_subset('val', self.val_initial_states, self.trainer.fast_dev_run)
            return DataQueue(val_initial_states, repeat=1)
        return super().val_seed_iterator()

    def test_seed_iterator(self) -> Iterable[InitialStateType]:
        if self.test_initial_states is not None:
            _logger.info("Testing initial states collection size: %d", len(self.test_initial_states))
            test_initial_states = self._random_subset('test', self.test_initial_states, self.trainer.fast_dev_run)
            return DataQueue(test_initial_states, repeat=1)
        return super().test_seed_iterator()

    def train(self, vector_env: BaseVectorEnv) -> dict[str, Any]:
        self.policy.train()
        collector = Collector(
            self.policy,
            vector_env,
            VectorReplayBuffer(self.buffer_size, len(vector_env))
        )
        col_result = collector.collect(n_episode=self.episode_per_iter)
        update_result = self.policy.update(
            0, collector.buffer, batch_size=self.batch_size, repeat=self.update_per_iter
        )
        return {**col_result, **update_result}

    def validate(self, vector_env: FiniteVectorEnv) -> dict[str, Any]:
        self.policy.eval()

        with vector_env.collector_guard():
            test_collector = Collector(self.policy, vector_env)
            return test_collector.collect(n_step=INF * len(vector_env))

    def test(self, vector_env: FiniteVectorEnv) -> dict[str, Any]:
        self.policy.eval()

        with vector_env.collector_guard():
            test_collector = Collector(self.policy, vector_env)
            return test_collector.collect(n_step=INF * len(vector_env))

    @staticmethod
    def _random_subset(name: str, collection: Sequence[T], size: int | None) -> Sequence[T]:
        if size is None:
            # Size = None -> original collection
            return collection
        order = np.random.permutation(len(collection))
        res = [collection[o] for o in order[:size]]
        _logger.info(
            "Fast running in development mode. Cut %s initial states from %d to %d.",
            name, len(collection), len(res)
        )
        return res
