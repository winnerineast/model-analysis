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
"""Weighted example count metric."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import apache_beam as beam
import numpy as np
from tensorflow_model_analysis import util
from tensorflow_model_analysis.metrics import metric_types
from typing import Dict, List, Optional, Text

WEIGHTED_EXAMPLE_COUNT_NAME = 'weighted_example_count'


class WeightedExampleCount(metric_types.Metric):
  """Weighted example count."""

  def __init__(self, name: Text = WEIGHTED_EXAMPLE_COUNT_NAME):
    """Initializes weighted example count.

    Args:
      name: Metric name.
    """
    super(WeightedExampleCount, self).__init__(
        _weighted_example_count, name=name)


metric_types.register_metric(WeightedExampleCount)


def _weighted_example_count(
    name: Text = WEIGHTED_EXAMPLE_COUNT_NAME,
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None
) -> metric_types.MetricComputations:
  """Returns metric computations for weighted example count."""
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

      # Note: This cannot be implemented based on the weight stored in
      # calibration because weighted example count is used with multi-class, etc
      # models that do not use calibration metrics.
      computations.append(
          metric_types.MetricComputation(
              keys=[key],
              preprocessor=None,
              combiner=_WeightedExampleCountCombiner(key)))
  return computations


class _WeightedExampleCountCombiner(beam.CombineFn):
  """Computes weighted example count."""

  def __init__(self, key: metric_types.MetricKey):
    self._key = key

  def create_accumulator(self) -> float:
    return 0.0

  def add_input(self, accumulator: float,
                element: metric_types.StandardMetricInputs) -> float:
    example_weight = element.example_weight or np.array(1.0)
    if isinstance(example_weight, dict) and self._key.model_name:
      value = util.get_by_keys(
          example_weight, [self._key.model_name], optional=True)
      if value is not None:
        example_weight = value
    if isinstance(example_weight, dict) and self._key.output_name:
      example_weight = util.get_by_keys(example_weight, [self._key.output_name],
                                        np.array(1.0))
    if isinstance(example_weight, dict):
      raise ValueError(
          'weighted_example_count cannot be calculated on a dict: {} = {}.\n\n'
          'This is most likely a configuration error (for multi-output models'
          'a separate metric is needed for each output).'.format(
              self._key, example_weight))
    return accumulator + np.sum(example_weight)

  def merge_accumulators(self, accumulators: List[float]) -> float:
    result = 0.0
    for accumulator in accumulators:
      result += accumulator
    return result

  def extract_output(self,
                     accumulator: float) -> Dict[metric_types.MetricKey, float]:
    return {self._key: accumulator}
