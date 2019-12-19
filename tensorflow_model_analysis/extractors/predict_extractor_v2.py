# Copyright 2019 Google LLC
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
"""Predict extractor."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import copy

import apache_beam as beam
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import constants
from tensorflow_model_analysis import model_util
from tensorflow_model_analysis import types
from tensorflow_model_analysis.extractors import extractor
from typing import List, Sequence

PREDICT_EXTRACTOR_STAGE_NAME = 'ExtractPredictions'

PREDICT_SIGNATURE_DEF_KEY = 'predict'


def PredictExtractor(
    eval_config: config.EvalConfig,
    eval_shared_models: List[types.EvalSharedModel]) -> extractor.Extractor:
  """Creates an extractor for performing predictions.

  The extractor's PTransform loads and runs the serving saved_model(s) against
  every extract yielding a copy of the incoming extracts with an additional
  extract added for the predictions keyed by tfma.PREDICTIONS_KEY. The model
  inputs are searched for under tfma.FEATURES_KEY (keras only) or tfma.INPUT_KEY
  (if tfma.FEATURES_KEY is not set or the model is non-keras). If multiple
  models are used the predictions will be stored in a dict keyed by model name.

  Args:
    eval_config: Eval config.
    eval_shared_models: Shared model parameters.

  Returns:
    Extractor for extracting predictions.
  """
  # pylint: disable=no-value-for-parameter
  return extractor.Extractor(
      stage_name=PREDICT_EXTRACTOR_STAGE_NAME,
      ptransform=_ExtractPredictions(
          eval_config=eval_config, eval_shared_models=eval_shared_models))


@beam.typehints.with_input_types(beam.typehints.List[types.Extracts])
@beam.typehints.with_output_types(types.Extracts)
class _PredictionDoFn(model_util.BatchReducibleDoFnWithModels):
  """A DoFn that loads the models and predicts."""

  def __init__(self, eval_config: config.EvalConfig,
               eval_shared_models: List[types.EvalSharedModel]) -> None:
    super(_PredictionDoFn, self).__init__(
        {m.model_path: m.model_loader for m in eval_shared_models})
    self._eval_config = eval_config

  def _batch_reducible_process(
      self,
      batch_of_extracts: List[types.Extracts]) -> Sequence[types.Extracts]:
    result = copy.deepcopy(batch_of_extracts)
    for spec in self._eval_config.model_specs:
      if spec.location not in self._loaded_models:
        raise ValueError('loaded model for location {} not found: '
                         'locations={}, eval_config={}'.format(
                             spec.location, self._loaded_models.keys(),
                             self._eval_config))
      loaded_model = self._loaded_models[spec.location]
      signatures = None
      if loaded_model.keras_model:
        signatures = loaded_model.keras_model.signatures
      elif loaded_model.saved_model:
        signatures = loaded_model.saved_model.signatures
      if not signatures:
        raise ValueError(
            'PredictExtractor V2 requires a keras model or a serving model. '
            'If using EvalSavedModel then you must use PredictExtractor V1.')

      signature_key = spec.signature_name
      if (not signature_key and spec.signature_names and
          spec.model_name in spec.signature_names):
        signature_key = spec.signature_names[spec.model_name]
      if not signature_key:
        # First try 'predict' then try 'serving_default'. The estimator output
        # for the 'serving_default' key does not include all the heads in a
        # multi-head model. However, keras only uses the 'serving_default' for
        # its outputs. Note that the 'predict' key only exists for estimators
        # for multi-head models, for single-head models only 'serving_default'
        # is used.
        signature_key = tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY
        if PREDICT_SIGNATURE_DEF_KEY in signatures:
          signature_key = PREDICT_SIGNATURE_DEF_KEY
      if signature_key not in signatures:
        raise ValueError('{} not found in model signatures: {}'.format(
            signature_key, signatures))
      signature = signatures[signature_key]

      # If input names exist then filter the inputs by these names (unlike
      # estimators, keras does not accept unknown inputs).
      input_names = None
      input_specs = None
      # First arg of structured_input_signature tuple is shape, second is dtype
      # (we currently only support named params passed as a dict)
      if (signature.structured_input_signature and
          len(signature.structured_input_signature) == 2 and
          isinstance(signature.structured_input_signature[1], dict)):
        input_names = [name for name in signature.structured_input_signature[1]]
        input_specs = signature.structured_input_signature[1]
      elif loaded_model.keras_model is not None:
        # Calling keras_model.input_names does not work properly in TF 1.15.0.
        # As a work around, make sure the signature.structured_input_signature
        # check is before this check (see b/142807137).
        input_names = loaded_model.keras_model.input_names
      inputs = None
      if input_names is not None:
        inputs = model_util.rebatch_by_input_names(batch_of_extracts,
                                                   input_names, input_specs)
      if not inputs and (input_names is None or len(input_names) <= 1):
        # Assume serialized examples
        inputs = [extract[constants.INPUT_KEY] for extract in batch_of_extracts]

      if isinstance(inputs, dict):
        outputs = signature(**{k: tf.constant(v) for k, v in inputs.items()})
      else:
        outputs = signature(tf.constant(inputs, dtype=tf.string))

      for i in range(len(result)):
        output = {k: v[i].numpy() for k, v in outputs.items()}
        # Keras and regression serving models return a dict of predictions even
        # for single-outputs. Convert these to a single tensor for compatibility
        # with the labels (and model.predict API).
        if len(output) == 1:
          output = list(output.values())[0]
        # If only one model, the predictions are stored without using a dict
        if len(self._eval_config.model_specs) == 1:
          result[i][constants.PREDICTIONS_KEY] = output
        else:
          if constants.PREDICTIONS_KEY not in result[i]:
            result[i][constants.PREDICTIONS_KEY] = {}
          result[i][constants.PREDICTIONS_KEY][spec.name] = output
    return result


@beam.ptransform_fn
@beam.typehints.with_input_types(types.Extracts)
@beam.typehints.with_output_types(types.Extracts)
def _ExtractPredictions(  # pylint: disable=invalid-name
    extracts: beam.pvalue.PCollection, eval_config: config.EvalConfig,
    eval_shared_models: List[types.EvalSharedModel]) -> beam.pvalue.PCollection:
  """A PTransform that adds predictions and possibly other tensors to extracts.

  Args:
    extracts: PCollection of extracts containing model inputs keyed by
      tfma.FEATURES_KEY (if model inputs are named) or tfma.INPUTS_KEY (if model
      takes raw tf.Examples as input).
    eval_config: Eval config.
    eval_shared_models: Shared model parameters.

  Returns:
    PCollection of Extracts updated with the predictions.
  """
  batch_args = {}
  # TODO(b/143484017): Consider removing this option if autotuning is better
  # able to handle batch size selection.
  if eval_config.options.HasField('desired_batch_size'):
    batch_args = dict(
        min_batch_size=eval_config.options.desired_batch_size.value,
        max_batch_size=eval_config.options.desired_batch_size.value)

  return (
      extracts
      | 'Batch' >> beam.BatchElements(**batch_args)
      | 'Predict' >> beam.ParDo(
          _PredictionDoFn(
              eval_config=eval_config, eval_shared_models=eval_shared_models)))
