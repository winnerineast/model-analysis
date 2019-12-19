# Lint as: python3
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
"""Min label position metric."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

from typing import Dict, List, Optional, Text
import apache_beam as beam
import numpy as np
from tensorflow_model_analysis import config
from tensorflow_model_analysis.metrics import metric_types
from tensorflow_model_analysis.metrics import metric_util

MIN_LABEL_POSITION_NAME = 'min_label_position'


class MinLabelPosition(metric_types.Metric):
  """Min label position metric.

  Calculates the least index in a query which has a positive label. The final
  returned value is the weighted average over all queries in the evaluation set
  which have at least one labeled entry. Note, ranking is indexed from one, so
  the optimal value for this metric is one. If there are no labeled rows in the
  evaluation set, the final output will be zero.

  This is a query/ranking based metric so a query_key must also be provided in
  the associated metrics spec.
  """

  def __init__(self, name=MIN_LABEL_POSITION_NAME):
    """Initializes min label position metric.

    Args:
      name: Metric name.
    """
    super(MinLabelPosition, self).__init__(_min_label_position, name=name)


metric_types.register_metric(MinLabelPosition)


def _min_label_position(
    name=MIN_LABEL_POSITION_NAME,
    eval_config: Optional[config.EvalConfig] = None,
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    query_key: Text = '') -> metric_types.MetricComputations:
  """Returns metric computations for min label position."""
  if not query_key:
    raise ValueError('a query_key is required to use MinLabelPosition metric')
  if model_names is None:
    model_names = ['']
  if output_names is None:
    output_names = ['']
  keys = []
  computations = []
  for model_name in model_names:
    for output_name in output_names:
      key = metric_types.MetricKey(
          name=name, model_name=model_name, output_name=output_name)
      keys.append(key)

      computations.append(
          metric_types.MetricComputation(
              keys=[key],
              preprocessor=None,
              combiner=_MinLabelPositionCombiner(key, eval_config)))
  return computations


class _MinLabelPositionAccumulator(object):
  """Min label position accumulator."""
  __slots__ = ['total_min_position', 'total_weighted_examples']

  def __init__(self):
    self.total_min_position = 0.0
    self.total_weighted_examples = 0.0


class _MinLabelPositionCombiner(beam.CombineFn):
  """Computes min label position metric."""

  def __init__(self, key: metric_types.MetricKey,
               eval_config: Optional[config.EvalConfig]):
    self._key = key
    self._eval_config = eval_config

  def create_accumulator(self) -> _MinLabelPositionAccumulator:
    return _MinLabelPositionAccumulator()

  def add_input(
      self, accumulator: _MinLabelPositionAccumulator,
      elements: List[metric_types.StandardMetricInputs]
  ) -> _MinLabelPositionAccumulator:
    min_label_pos = None
    example_weight = None
    for i, element in enumerate(elements):
      label, _, weight = next(
          metric_util.to_label_prediction_example_weight(
              element,
              eval_config=self._eval_config,
              model_name=self._key.model_name,
              output_name=self._key.output_name,
              flatten=False,
              allow_none=True))
      weight = float(weight)
      if example_weight is None:
        example_weight = weight
      elif example_weight != weight:
        raise ValueError(
            'all example weights for the same query value must use the '
            'same value {} != {}: StandardMetricInputs={}'.format(
                weight, example_weight, element))
      if label is not None and np.sum(label) > 0:
        min_label_pos = i + 1  # Use 1-indexed positions
        break
    if example_weight is None:
      example_weight = 1.0
    if min_label_pos:
      accumulator.total_min_position += min_label_pos
      accumulator.total_weighted_examples += example_weight
    return accumulator

  def merge_accumulators(
      self, accumulators: List[_MinLabelPositionAccumulator]
  ) -> _MinLabelPositionAccumulator:
    result = self.create_accumulator()
    for accumulator in accumulators:
      result.total_min_position += accumulator.total_min_position
      result.total_weighted_examples += accumulator.total_weighted_examples
    return result

  def extract_output(
      self, accumulator: _MinLabelPositionAccumulator
  ) -> Dict[metric_types.MetricKey, float]:
    if accumulator.total_weighted_examples > 0:
      value = (
          accumulator.total_min_position / accumulator.total_weighted_examples)
    else:
      value = float('nan')
    return {self._key: value}
