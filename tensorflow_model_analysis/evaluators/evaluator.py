# Lint as: python3
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
"""Evaluator types."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

from typing import Any, Dict, List, NamedTuple, Text
import apache_beam as beam
from tensorflow_model_analysis.extractors import extractor

# An evaluator is a PTransform that takes Extracts as input and produces an
# Evaluation as output. A typical example of an evaluator is the
# MetricsAndPlotsEvaluator that takes the 'features', 'labels', and
# 'predictions' extracts from the PredictExtractor and evaluates them using post
# export metrics to produce serialized metrics and plots.
Evaluator = NamedTuple(  # pylint: disable=invalid-name
    'Evaluator',
    [
        ('stage_name', Text),
        # Extractor.stage_name. If None then evaluation is run before any
        # extractors are run. If LAST_EXTRACTOR_STAGE_NAME then evaluation is
        # run after the last extractor has run.
        ('run_after', Text),
        # PTransform Extracts -> Evaluation
        ('ptransform', beam.PTransform)
    ])

# An Evaluation represents the output from evaluating the Extracts at a
# particular point in the pipeline. The evaluation outputs are keyed by their
# associated output type. For example, the serialized protos from evaluating
# metrics and plots might be stored under "metrics" and "plots" respectively.
Evaluation = Dict[Text, beam.pvalue.PCollection]


def verify_evaluator(evaluator: Evaluator,
                     extractors: List[extractor.Extractor]):
  """Verifies evaluator is matched with an extractor.

  Args:
    evaluator: Evaluator to verify.
    extractors: Extractors to use in verification.

  Raises:
    ValueError: If an Extractor cannot be found for the Evaluator.
  """
  if (evaluator.run_after and
      evaluator.run_after != extractor.LAST_EXTRACTOR_STAGE_NAME and
      not any(evaluator.run_after == x.stage_name for x in extractors)):
    raise ValueError(
        'Extractor matching run_after=%s for Evaluator %s not found' %
        (evaluator.run_after, evaluator.stage_name))


class _CombineEvaluationDictionariesFn(beam.CombineFn):
  """CombineFn to combine dictionaries generated by different evaluators."""

  def create_accumulator(self) -> Dict[Text, Any]:
    return {}

  def _merge(self, accumulator: Dict[Text, Any],
             output_dict: Dict[Text, Any]) -> None:
    intersection = set(accumulator) & set(output_dict)
    if intersection:
      raise ValueError(
          'Dictionaries generated by different evaluators should have '
          'different keys, but keys %s appeared in the output of multiple '
          'evaluators' % intersection)
    accumulator.update(output_dict)

  def add_input(self, accumulator: Dict[Text, Any],
                output_dict: Dict[Text, Any]) -> Dict[Text, Any]:
    if not isinstance(output_dict, dict):
      raise TypeError(
          'for outputs written to by multiple evaluators, the outputs must all '
          'be dictionaries, but got output of type %s, value %s' %
          (type(output_dict), str(output_dict)))
    self._merge(accumulator, output_dict)
    return accumulator

  def merge_accumulators(
      self, accumulators: List[Dict[Text, Any]]) -> Dict[Text, Any]:
    result = self.create_accumulator()
    for acc in accumulators:
      self._merge(result, acc)
    return result

  def extract_output(self, accumulator: Dict[Text, Any]) -> Dict[Text, Any]:
    return accumulator


def combine_dict_based_evaluations(
    evaluations: Dict[Text, List[beam.pvalue.PCollection]]) -> Evaluation:
  """Combines multiple evaluation outputs together when the outputs are dicts.

  Note that the dict here refers to the output in the PCollection. The
  evaluations themselves are dicts of PCollections keyed by category ('metrics',
  'plots', 'analysis', etc). This util is used to group the outputs of one or
  more of these evaluations where the PCollections themselves must be dicts. For
  example, a 'metrics' evaluation might store its output in PCollection of dicts
  containing metric keys and metric values. This util would be used to group the
  outputs from running two or more independent metrics evaluations together into
  a single PCollection.

  Args:
    evaluations: Dict of lists of PCollections of outputs from different
      evaluators keyed by type of output ('metrics', 'plots', 'analysis', etc).

  Returns:
    Dict of consolidated PCollections of outputs keyed by type of output.
  """
  result = {}
  for k, v in evaluations.items():
    if len(v) == 1:
      result[k] = v[0]
      continue

    result[k] = (
        v
        | 'FlattenEvaluationOutput(%s)' % k >> beam.Flatten()
        | 'CombineEvaluationOutput(%s)' % k >> beam.CombinePerKey(
            _CombineEvaluationDictionariesFn()))
  return result
