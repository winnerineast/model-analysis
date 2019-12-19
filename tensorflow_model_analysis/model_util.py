# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for working with models."""

# Standard __future__ imports

import collections
import datetime
import apache_beam as beam
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import constants
from tensorflow_model_analysis import types
from tensorflow_model_analysis.eval_saved_model import constants as eval_constants
from tensorflow_model_analysis.eval_saved_model import load

from typing import Any, Callable, Dict, List, Optional, Sequence, Text

KERAS_INPUT_SUFFIX = '_input'


def get_baseline_model_spec(
    eval_config: config.EvalConfig) -> Optional[config.ModelSpec]:
  """Returns baseline model spec."""
  for spec in eval_config.model_specs:
    if spec.is_baseline:
      return spec
  return None


def get_model_spec(eval_config: config.EvalConfig,
                   model_name: Text) -> Optional[config.ModelSpec]:
  """Returns model spec with given model name."""
  for spec in eval_config.model_specs:
    if spec.name == model_name:
      return spec
  return None


def rebatch_by_input_names(
    batch_of_extracts: List[types.Extracts],
    input_names: List[Text],
    input_specs: Optional[Dict[Text, tf.TypeSpec]] = None) -> Dict[Text, Any]:
  """Converts a batch of extracts into multiple batches keyed by input names.

  Args:
    batch_of_extracts: Batch of extracts (one per example).
    input_names: List of input names to search for features under.
    input_specs: Optional list of type specs associated with inputs.

  Returns:
    Dict of batch aligned features keyed by input (feature) name.
  """
  # TODO(b/138474171): Make this code more efficient.
  if input_specs is None:
    input_specs = {}
  inputs = collections.defaultdict(list)
  found = {}
  for name in input_names:
    for extract in batch_of_extracts:
      # If features key exist, use that for features, else use input_key
      if constants.FEATURES_KEY in extract:
        input_features = extract[constants.FEATURES_KEY]
      else:
        input_features = extract[constants.INPUT_KEY]
      if isinstance(input_features, dict):
        value = None
        if name in input_features:
          found[name] = True
          value = input_features[name]
        # Some keras models prepend '_input' to the names of the inputs
        # so try under '<name>_input' as well.
        elif (name.endswith(KERAS_INPUT_SUFFIX) and
              name[:-len(KERAS_INPUT_SUFFIX)] in input_features):
          found[name] = True
          value = input_features[name[:-len(KERAS_INPUT_SUFFIX)]]
        if value is not None:
          # If the expected input shape contains only the batch dimension
          # then we need to flatten the np.array. This it to handle tf_hub
          # cases where the inputs can have a single dimension.
          if name in input_specs and len(input_specs[name].shape) == 1:
            if value.size != 1:
              raise ValueError(
                  'model expects inputs with shape (?,), but shape is '
                  '{}: input_names={} input_specs={}, extract={}'.format(
                      value.shape, input_names, input_specs, extract))
            inputs[name].append(value.item())
          else:
            inputs[name].append(value)
      else:
        # Check that we have not previously added inputs before.
        if inputs:
          raise ValueError(
              'only a single input was passed, but model expects multiple: '
              'input_names = {}, extract={}'.format(input_names, extract))
        found[name] = True
        inputs[name].append(input_features)
  if len(found) != len(input_names):
    tf.compat.v1.logging.warning(
        'inputs do not match those expected by the '
        'model: input_names={}, found in extracts={}'.format(
            input_names, found))
  return inputs


def model_construct_fn(  # pylint: disable=invalid-name
    eval_saved_model_path: Optional[Text] = None,
    add_metrics_callbacks: Optional[List[types.AddMetricsCallbackType]] = None,
    include_default_metrics: Optional[bool] = None,
    additional_fetches: Optional[List[Text]] = None,
    blacklist_feature_fetches: Optional[List[Text]] = None,
    tags: Optional[List[Text]] = None):
  """Returns function for constructing shared ModelTypes."""
  if tags is None:
    tags = [eval_constants.EVAL_TAG]

  def construct_fn(model_load_seconds_callback: Callable[[int], None]):
    """Thin wrapper for the actual construct to allow for load time metrics."""

    def construct():  # pylint: disable=invalid-name
      """Function for constructing shared ModelTypes."""
      start_time = datetime.datetime.now()
      saved_model = None
      keras_model = None
      eval_saved_model = None
      # If we are evaluating on TPU, initialize the TPU.
      # TODO(b/143484017): Add model warmup for TPU.
      if tf.saved_model.TPU in tags:
        tf.tpu.experimental.initialize_tpu_system()
      if eval_constants.EVAL_TAG in tags:
        eval_saved_model = load.EvalSavedModel(
            eval_saved_model_path,
            include_default_metrics,
            additional_fetches=additional_fetches,
            blacklist_feature_fetches=blacklist_feature_fetches,
            tags=tags)
        if add_metrics_callbacks:
          eval_saved_model.register_add_metric_callbacks(add_metrics_callbacks)
        eval_saved_model.graph_finalize()
      else:
        # TODO(b/141524386, b/141566408): TPU Inference is not supported
        # for Keras saved_model yet.
        try:
          keras_model = tf.keras.models.load_model(eval_saved_model_path)
          # In some cases, tf.keras.models.load_model can successfully load a
          # saved_model but it won't actually be a keras model.
          if not isinstance(keras_model, tf.keras.models.Model):
            keras_model = None
        except Exception:  # pylint: disable=broad-except
          keras_model = None
        if keras_model is None:
          saved_model = tf.compat.v1.saved_model.load_v2(
              eval_saved_model_path, tags=tags)
      end_time = datetime.datetime.now()
      model_load_seconds_callback(int((end_time - start_time).total_seconds()))
      return types.ModelTypes(
          saved_model=saved_model,
          keras_model=keras_model,
          eval_saved_model=eval_saved_model)

    return construct

  return construct_fn


class DoFnWithModels(beam.DoFn):
  """Abstract class for DoFns that need the shared models."""

  def __init__(self, model_loaders: Dict[Text, types.ModelLoader]):
    """Initializes DoFn using dict of model loaders keyed by model location."""
    self._model_loaders = model_loaders
    self._loaded_models = None  # types.ModelTypes
    self._model_load_seconds = None
    self._model_load_seconds_distribution = beam.metrics.Metrics.distribution(
        constants.METRICS_NAMESPACE, 'model_load_seconds')

  def _set_model_load_seconds(self, model_load_seconds):
    self._model_load_seconds = model_load_seconds

  def setup(self):
    self._loaded_models = {}
    for model_path, model_loader in self._model_loaders.items():
      self._loaded_models[model_path] = model_loader.shared_handle.acquire(
          model_loader.construct_fn(self._set_model_load_seconds))

  def process(self, elem):
    raise NotImplementedError('Subclasses are expected to override this.')

  def finish_bundle(self):
    # Must update distribution in finish_bundle instead of setup
    # because Beam metrics are not supported in setup.
    if self._model_load_seconds is not None:
      self._model_load_seconds_distribution.update(self._model_load_seconds)
      self._model_load_seconds = None


@beam.typehints.with_input_types(beam.typehints.List[types.Extracts])
@beam.typehints.with_output_types(types.Extracts)
class BatchReducibleDoFnWithModels(DoFnWithModels):
  """Abstract class for DoFns that need the shared models.

  This DoFn will try to use large batch size at first. If a functional failure
  is caught, an attempt will be made to process the elements serially
  at batch size 1.
  """

  def __init__(self, model_loaders: Dict[Text, types.ModelLoader]):
    super(BatchReducibleDoFnWithModels, self).__init__(model_loaders)
    self._batch_size = (
        beam.metrics.Metrics.distribution(constants.METRICS_NAMESPACE,
                                          'batch_size'))
    self._batch_size_failed = (
        beam.metrics.Metrics.distribution(constants.METRICS_NAMESPACE,
                                          'batch_size_failed'))
    self._num_instances = beam.metrics.Metrics.counter(
        constants.METRICS_NAMESPACE, 'num_instances')

  def _batch_reducible_process(
      self, elements: List[types.Extracts]) -> Sequence[types.Extracts]:
    raise NotImplementedError('Subclasses are expected to override this.')

  def process(self, elements: List[types.Extracts]) -> Sequence[types.Extracts]:
    batch_size = len(elements)
    try:
      result = self._batch_reducible_process(elements)
      self._batch_size.update(batch_size)
      self._num_instances.inc(batch_size)
      return result
    except (ValueError, tf.errors.InvalidArgumentError) as e:
      tf.compat.v1.logging.warning(
          'Large batch_size %s failed with error %s. '
          'Attempting to run batch through serially.', batch_size, e)
      self._batch_size_failed.update(batch_size)
      result = []
      for element in elements:
        self._batch_size.update(1)
        result.extend(self._batch_reducible_process([element]))
      self._num_instances.inc(len(result))
      return result


class CombineFnWithModels(beam.CombineFn):
  """Abstract class for CombineFns that need the shared models.

  Until BEAM-3736 (Add SetUp() and TearDown() for CombineFns) is implemented
  users of this class are responsible for calling _setup_if_needed manually.
  """

  def __init__(self, model_loaders: Dict[Text, types.ModelLoader]):
    """Initializes CombineFn using dict of loaders keyed by model location."""
    self._model_loaders = model_loaders
    self._loaded_models = None  # types.ModelTypes
    self._model_load_seconds = None
    self._model_load_seconds_distribution = beam.metrics.Metrics.distribution(
        constants.METRICS_NAMESPACE, 'model_load_seconds')

  def _set_model_load_seconds(self, model_load_seconds):
    self._model_load_seconds = model_load_seconds

  # TODO(yifanmai): Merge _setup_if_needed into setup
  # There's no initialisation method for CombineFns.
  # See BEAM-3736: Add SetUp() and TearDown() for CombineFns.
  def _setup_if_needed(self) -> None:
    if self._loaded_models is None:
      self._loaded_models = {}
      for model_path, model_loader in self._model_loaders.items():
        self._loaded_models[model_path] = model_loader.shared_handle.acquire(
            model_loader.construct_fn(self._set_model_load_seconds))
      if self._model_load_seconds is not None:
        self._model_load_seconds_distribution.update(self._model_load_seconds)
        self._model_load_seconds = None
