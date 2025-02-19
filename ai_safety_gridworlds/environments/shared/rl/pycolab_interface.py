# Copyright 2018 The AI Safety Gridworlds Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Python environment hooks for pycolab."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from ai_safety_gridworlds.environments.shared.rl import array_spec as specs
from ai_safety_gridworlds.environments.shared.rl import environment

import numpy as np
import six
from six.moves import zip


class Environment(object):
  """A generic Python interface for pycolab games."""

  def __init__(self, game_factory, discrete_actions, default_reward,
               observation_distiller, continuous_actions=None,
               max_iterations=float('inf')):
    """Construct a `Base` adapter that wraps a pycolab game.

    For each episode, a new pycolab game is supplied by the `game_factory`
    argument. The structure of games' rewards is restricted to scalar values,
    while actions passed to the games are either scalar values as well or
    concatenated flat lists of scalar values. The structure of the
    `discrete_actions` and `continuous_actions` determines the structure of the
    actions received by the game as follows:

    | `discrete_actions` is  | `continuous_actions` is | action is             |
    |------------------------|-------------------------|-----------------------|
    | a `(min, max)` 2-tuple | `None`                  | a scalar              |
    | `None`                 | a `(min, max)` 2-tuple  | a scalar              |
    |------------------------|-------------------------|-----------------------|
    | a list of N 2-tuples   | `None`                  | a list of N scalars   |
    | `None`                 | a list of N 2-tuples    | a list of N scalars   |
    |------------------------|-------------------------|-----------------------|
    | a list of N 2-tuples   | a `(min, max)` 2-tuple  | a list of N+1 scalars |
    | a `(min, max)` 2-tuple | a list of N 2-tuples    | a list of N+1 scalars |
    |------------------------|-------------------------|-----------------------|
    | a `(min, max)` 2-tuple | a `(min, max)` 2-tuple  | a list of 2 scalars   |
    | a list of N 2-tuples   | a list of M 2-tuples    | a list of N+M scalars |

    Here, a scalar action may be an int or float as appropriate, or a numpy
    array with a single element.

    Whenever there are arrays containing both discrete and continuous actions,
    the discrete actions always precede the continuous ones.

    The format of your observations depends on the value returned by your
    `observation_distiller`. If a numpy array, then the observations will be a
    dict whose single entry, `'board'`, is that array. Otherwise, your distiller
    should return a dict mapping string names to numpy arrays whose dimensions
    and contents are of your choosing.

    If a game ever terminates, the episode is considered terminated. The game
    underway will be discarded and a new game built by the `game_factory`.

    Args:
      game_factory: a callable that returns a fully-constructed pycolab
          game engine. The `its_showtime` method should not have been called yet
          on the returned games. For most predictable results, this callable
          should be stateless.
      discrete_actions: a `(min, max)` tuple or a list of such tuples, or `None`
          if the game does not use discrete actions. See discussion above.
      default_reward: a reward to return to clients of this `environment.Base`
          adapter when (or if) the game issues a reward of None. Should probably
          be a scalar (0.0 is a typical choice); should definitely have the same
          dimensions and type as the non-None rewards returned by `game_factory`
          games.
      observation_distiller: a callable that takes the `rendering.Observation`s
          generated by `game_factory`-returned game engines and converts them
          into numpy arrays (or dicts of numpy arrays). The `Distiller` class
          in this module documents further requirements for this argument and
          provides a common idiom that may be adequate for many use cases.
      continuous_actions: a `(min, max)` tuple or a list of such tuples, or
          `None` if the game does not use continuous actions. See discussion
          above.
      max_iterations: the maximum number of game iterations that an episode may
          last before it gets terminated. By default, this is unlimited, but if
          specified it prevents games from going on forever.

    Raises:
      TypeError: the game returned by `game_factory` appears to have a reward
          type that doesn't match the type of the `default_reward` value. This
          check is not particularly rigorous (it won't descend into lists,
          and can't do the check if the game returns a reward of `None` on the
          `its_showtime` call).
      ValueError: `discrete_actions` and `continuous_actions` were both `None`
          or empty lists.
    """
    # Save important constructor arguments.
    self._game_factory = game_factory
    self._default_reward = default_reward
    self._observation_distiller = observation_distiller
    self._max_iterations = max_iterations

    # These slots comprise an Environment's internal state. They are:
    self._state = None              # Current Environment game step state.
    self._current_game = None       # Current pycolab game instance.
    self._game_over = None          # Whether the instance's game has ended.
    self._last_observations = None  # Last observation received from the game.
    self._last_reward = None        # Last reward, if any, or default reward.
    self._last_discount = None      # Last discount factor from the game.

    # Attempt to distill our action spec.
    self._valid_actions, self._action_size = self._compute_action_spec(
        discrete_actions, continuous_actions)

    # With this, we're ready to compute our own observation spec. This is done
    # by starting a new episode, inspecting the observations returned in the
    # first step, then closing the episode and resetting internal variables
    # to a default value.
    self._observation_spec = self._compute_observation_spec()

  def reset(self, game_art=None):
    """Start a new episode."""
    # Build a new game and retrieve its first set of state/reward/discount.
    self._current_game = self._game_factory(game_art=game_art)
    self._state = environment.StepType.FIRST
    # Collect environment returns from starting the game and update state.
    observations, reward, discount = self._current_game.its_showtime()
    self._update_for_game_step(observations, reward, discount)
    return environment.TimeStep(
        step_type=self._state,
        reward=None,
        discount=None,
        observation=self.last_observations)

  def step(self, action):
    """Apply action, step the world forward, and return observations."""

    if self._action_size == 1:
      # Handle a float or single-element arrays of any dimensionality. Strictly
      # speaking, a single-element list will also work, but it's best not to
      # confuse matters in the docstring with this option.
      all_actions = [np.asarray(action).item()]
    else:
      all_actions = [np.asarray(a).item() for a in action]

    if len(all_actions) != self._action_size:
      raise RuntimeError("A pycolab Environment adapter's step method "
                         'was called with actions that were not compatible '
                         'with what the pycolab game expects.')

    # Clear episode internals and start a new episode, if episode ended or if
    # the game was not already underway.
    if self._state == environment.StepType.LAST:
      self._drop_last_episode()
    if self._current_game is None:
      return self.reset()

    # Execute the action in pycolab.
    action = all_actions[0] if self._action_size == 1 else all_actions
    observations, reward, discount = self._current_game.play(action)
    self._update_for_game_step(observations, reward, discount)

    # Check the current status of the game.
    if self._game_over:
      self._state = environment.StepType.LAST
    else:
      self._state = environment.StepType.MID

    return environment.TimeStep(
        step_type=self._state,
        reward=self._last_reward,
        discount=self._last_discount,
        observation=self.last_observations)

  def observation_spec(self):
    return self._observation_spec

  def action_spec(self):
    return self._valid_actions

  @property
  def last_observations(self):
    """Distill and return the last observation."""
    # A "bare" numpy array will be placed in a dict under the key "board".
    if isinstance(self._last_observations, dict):
      observation = self._last_observations
    else:
      observation = {'board': self._last_observations}

    return observation

  ### Various helpers. ###

  def _compute_action_spec(self, discrete_actions, continuous_actions):
    """Helper for `__init__`: compute our environment's action spec."""
    valid_actions = []

    # First discrete actions:
    if discrete_actions is not None:
      try:
        # Get an array of upper and lower bounds for each discrete action.
        min_, max_ = list(zip(*discrete_actions))
        # Total number of discrete actions provided on each time step.
        shape = (len(discrete_actions),)
      except TypeError:
        min_, max_ = discrete_actions  # Enforces 2-tuple.
        shape = (1,)
      spec = specs.BoundedArraySpec(shape=shape,
                                    dtype='int32',
                                    minimum=min_,
                                    maximum=max_,
                                    name='discrete')
      valid_actions.append(spec)

    # Then continuous actions:
    if continuous_actions is not None:
      try:
        # Get an array of upper and lower bounds for each continuous action.
        min_, max_ = list(zip(*continuous_actions))
        # Total number of continuous actions provided on each time step.
        shape = (len(continuous_actions),)
      except TypeError:
        min_, max_ = continuous_actions  # Enforces 2-tuple
        shape = (1,)
      spec = specs.BoundedArraySpec(shape=shape,
                                    dtype='float32',
                                    minimum=min_,
                                    maximum=max_,
                                    name='continuous')
      valid_actions.append(spec)

    # And in total we have this many actions.
    action_size = sum(value.shape[0] for value in valid_actions)

    if action_size <= 0:
      raise ValueError('A pycolab Environment adapter was initialised '
                       'without any discrete or continuous actions specified.')

    # Use arrays directly if we only have one.
    if len(valid_actions) == 1:
      valid_actions = valid_actions[0]

    return valid_actions, action_size

  def _compute_observation_spec(self):
    """Helper for `__init__`: compute our environment's observation spec."""
    # Start an environment, examine the values it gives to us, and reset things
    # back to default.
    timestep = self.reset()
    observation_spec = {k: specs.ArraySpec(v.shape, v.dtype, name=k)
                        for k, v in six.iteritems(timestep.observation)}
    # As long as we've got environment result data, we try checking to make sure
    # that the reward types can be added together---a very weak way of measuring
    # whether they are compatible.
    if timestep.reward is not None:
      try:
        _ = timestep.reward + self._default_reward
      except TypeError:
        raise TypeError(
            'A pycolab game wrapped by an Environment adapter returned '
            'a first reward whose type is incompatible with the default reward '
            "given to the adapter's `__init__`.")

    self._drop_last_episode()
    return observation_spec

  def _update_for_game_step(self, observations, reward, discount):
    """Update internal state with data from an environment interaction."""
    # Save interaction data in slots for self.observations() et al.
    self._last_observations = self._observation_distiller(observations)
    self._last_reward = reward if reward is not None else self._default_reward
    self._last_discount = discount
    self._game_over = self._current_game.game_over

    # If we've reached the maximum number of game iterations, terminate the
    # current game.
    if self._current_game.the_plot.frame >= self._max_iterations:
      self._game_over = True

  def _drop_last_episode(self):
    """Clear all the internal information about the game."""
    self._state = None
    self._current_game = None
    self._game_over = None
    self._last_observations = None
    self._last_reward = None
    self._last_discount = None


class Distiller(object):
  """A convenience class for `observation_distiller` parameters.

  An "observation distiller" is any function from the `rendering.Observation`s
  generated by a pycolab game to a numpy array or a dict mapping string
  keys to numpy arrays.  While any callable performing this transformation is
  usable as the `observation_distiller` parameter to the `Environment`
  constructor, happy users tend to have these callables be stateless.

  This class is sugar for a common pattern, which is to distill `Observation`s
  first by repainting the characters that make up the observations and then to
  convert the resulting `Observation` into one or more numpy arrays for
  tendering to TensorFlow. For the former, a
  `rendering.ObservationCharacterRepainter` will probably meet your needs; for
  the latter, consider `rendering.ObservationToArray` or
  `rendering.ObservationToFeatureArray`.

  Or don't; I'm a docstring, not a cop.
  """

  def __init__(self, repainter, array_converter):
    """Construct a Distiller.

    Args:
      repainter: a callable that converts `rendering.Observation`s to different
          `rendering.Observation`s, or None if no such conversion is required.
          This facility is normally used to change the characters used to
          depict certain game elements, and a
          `rendering.ObservationCharacterRepainter` object is a convenient way
          to accomplish this conversion.
      array_converter: a callable that converts `rendering.Observation`s to
          a numpy array or a dict mapping strings to numpy arrays.
    """
    self._repainter = repainter
    self._array_converter = array_converter

  def __call__(self, observation):
    if self._repainter: observation = self._repainter(observation)
    return self._array_converter(observation)
