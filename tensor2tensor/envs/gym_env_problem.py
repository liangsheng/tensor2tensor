# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
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

"""Base class for envs that store their history.

EnvProblem subclasses Problem and also implements the Gym interface (step,
reset, render, close, seed)
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import multiprocessing.pool
import time

import gym
import numpy as np
from tensor2tensor.envs import env_problem
from tensor2tensor.envs import trajectory
import tensorflow as tf


class GymEnvProblem(env_problem.EnvProblem):
  """An EnvProblem implemented as a batch of gym envs.

  This implementation should work well for cases where the env is not batched by
  default ex: any gym env. In this case we create `batch_size` number of envs
  and store them in a list. Any function then that interacts with the envs, like
  reset, step or close goes over the env list to do the needful, ex: when reset
  is called with specific indices we reset only those indices, etc.

  The usage of this class will look like the following:

  # 1. Creates and initializes the env_problem.
  ep = env_problem.EnvProblem(...)

  # 2. One needs to call reset() at the start, this resets all envs.
  ep.reset()

  # 3. Call step with actions for all envs, i.e. len(action) = batch_size
  obs, rewards, dones, infos = ep.step(actions)

  # 4. Figure out which envs got done and reset only those.
  ep.reset(indices=env_problem_utils.done_indices(dones))

  # 5. Go back to Step #3 to further interact with the env or just dump the
  # generated data to disk by calling:
  ep.generate_data(...)

  # 6. If we now need to use this object again to play a few more iterations
  # perhaps with a different batch size or maybe not recording the data, then
  # we need to re-initialize environments and do some book-keeping, call:
  ep.initialize_environments(batch_size)

  # 7. Go back to Step #2, i.e. reset all envs.

  NOTE: Look at `EnvProblemTest.test_interaction_with_env` and/or
  `EnvProblemTest.test_generate_data`

  NOTE: We rely heavily that the underlying environments expose a gym style
  interface, i.e. in addition to reset(), step() and close() we have access to
  the following properties: observation_space, action_space, reward_range.
  """

  def __init__(self, base_env_name=None, env_wrapper_fn=None, **kwargs):
    """Initializes this class by creating the envs and managing trajectories.

    Args:
      base_env_name: (string) passed to `gym.make` to make the underlying
        environment.
      env_wrapper_fn: (callable(env): env) Applies gym wrappers to the base
        environment.
      **kwargs: (dict) Arguments passed to the base class.
    """
    # Name for the base environment, will be used in `gym.make` in
    # the default implementation of `initialize_environments`.
    self._base_env_name = base_env_name

    # An env generates data when it is given actions by an agent which is either
    # a policy or a human -- this is supposed to be the `id` of the agent.
    #
    # In practice, this is used only to store (and possibly retrieve) history
    # to an appropriate directory.
    self._agent_id = "default"

    # Initialize the environment(s).

    # This can either be a list of environments of len `batch_size` or this can
    # be a Neural Network, in which case it will be fed input with first
    # dimension = `batch_size`.
    self._envs = None
    self._pool = None

    self._env_wrapper_fn = env_wrapper_fn

    # Call the super's ctor. It will use some of the member fields, so we call
    # it in the end.
    super(GymEnvProblem, self).__init__(**kwargs)

  @property
  def base_env_name(self):
    return self._base_env_name

  def _verify_same_spaces(self):
    """Verifies that all the envs have the same observation and action space."""

    # Pre-conditions: self._envs is initialized.

    if self._envs is None:
      raise ValueError("Environments not initialized.")

    if not isinstance(self._envs, list):
      tf.logging.warning("Not checking observation and action space "
                         "compatibility across envs, since there is just one.")
      return

    # NOTE: We compare string representations of observation_space and
    # action_space because compositional classes like space.Tuple don't return
    # true on object comparison.

    if not all(
        str(env.observation_space) == str(self.observation_space)
        for env in self._envs):
      err_str = ("All environments should have the same observation space, but "
                 "don't.")
      tf.logging.error(err_str)
      # Log all observation spaces.
      for i, env in enumerate(self._envs):
        tf.logging.error("Env[%d] has observation space [%s]", i,
                         env.observation_space)
      raise ValueError(err_str)

    if not all(
        str(env.action_space) == str(self.action_space) for env in self._envs):
      err_str = "All environments should have the same action space, but don't."
      tf.logging.error(err_str)
      # Log all action spaces.
      for i, env in enumerate(self._envs):
        tf.logging.error("Env[%d] has action space [%s]", i, env.action_space)
      raise ValueError(err_str)

  def initialize_environments(self, batch_size=1, parallelism=1, **kwargs):
    """Initializes the environments.

    Args:
      batch_size: (int) Number of `self.base_env_name` envs to initialize.
      parallelism: (int) If this is greater than one then we run the envs in
        parallel using multi-threading.
      **kwargs: (dict) Kwargs to pass to gym.make.
    """
    assert batch_size >= 1

    self._envs = [
        gym.make(self.base_env_name, **kwargs) for _ in range(batch_size)
    ]
    self._parallelism = parallelism
    self._pool = multiprocessing.pool.ThreadPool(self._parallelism)
    if self._env_wrapper_fn is not None:
      self._envs = list(map(self._env_wrapper_fn, self._envs))

    # If self.observation_space and self.action_space aren't None, then it means
    # that this is a re-initialization of this class, in that case make sure
    # that this matches our previous behaviour.
    if self._observation_space:
      assert str(self._observation_space) == str(
          self._envs[0].observation_space)
    else:
      # This means that we are initializing this class for the first time.
      #
      # We set this equal to the first env's observation space, later on we'll
      # verify that all envs have the same observation space.
      self._observation_space = self._envs[0].observation_space

    # Similarly for action_space
    if self._action_space:
      assert str(self._action_space) == str(self._envs[0].action_space)
    else:
      self._action_space = self._envs[0].action_space

    self._verify_same_spaces()

    # If self.reward_range is None, i.e. this means that we should take the
    # reward range of the env.
    if self.reward_range is None:
      self._reward_range = self._envs[0].reward_range

    # This data structure stores the history of each env.
    #
    # NOTE: Even if the env is a NN and can step in all batches concurrently, it
    # is still valuable to store the trajectories separately.
    self._trajectories = trajectory.BatchTrajectory(batch_size=batch_size)

  def seed(self, seed=None):
    if not self._envs:
      tf.logging.info("`seed` called on non-existent envs, doing nothing.")
      return None

    if not isinstance(self._envs, list):
      tf.logging.warning("`seed` called on non-list envs, doing nothing.")
      return None

    tf.logging.warning(
        "Called `seed` on EnvProblem, calling seed on the underlying envs.")
    for env in self._envs:
      env.seed(seed)

    return super(GymEnvProblem, self).seed(seed=seed)

  def close(self):
    if not self._envs:
      tf.logging.info("`close` called on non-existent envs, doing nothing.")
      return

    if not isinstance(self._envs, list):
      tf.logging.warning("`close` called on non-list envs, doing nothing.")
      return

    # Call close on all the envs one by one.
    for env in self._envs:
      env.close()

  def _reset(self, indices):
    """Resets environments at indices shouldn't pre-process or record.

    Args:
      indices: list of indices of underlying envs to call reset on.

    Returns:
      np.ndarray of stacked observations from the reset-ed envs.
    """
    # This returns a numpy array with first dimension `len(indices)` and the
    # rest being the dimensionality of the observation.
    return np.stack([self._envs[index].reset() for index in indices])

  def _step(self, actions):
    """Takes a step in all environments, shouldn't pre-process or record.

    Args:
      actions: (np.ndarray) with first dimension equal to the batch size.

    Returns:
      a tuple of stacked raw observations, raw rewards, dones and infos.
    """
    assert len(actions) == len(self._envs)

    observations = [None] * self.batch_size
    rewards = [None] * self.batch_size
    dones = [None] * self.batch_size
    infos = [{} for _ in range(self.batch_size)]

    def apply_step(i):
      t1 = time.time()
      observations[i], rewards[i], dones[i], infos[i] = self._envs[i].step(
          actions[i])
      t2 = time.time()
      infos[i]["__bare_env_run_time__"] = t2 - t1

    if self._parallelism > 1:
      self._pool.map(apply_step, range(self.batch_size))
    else:
      for i in range(self.batch_size):
        apply_step(i)

    # Convert each list (observations, rewards, ...) into np.array and return a
    # tuple.
    return tuple(map(np.stack, [observations, rewards, dones, infos]))
