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
"""Utils for performing poisson bootstrapping."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

# Standard Imports
import apache_beam as beam
import numpy as np

from tensorflow_model_analysis import types
from tensorflow_model_analysis.slicer import slicer_lib as slicer
from typing import Any, Dict, Generator, Iterable, List, Optional, Text, Tuple, Type, Union

DEFAULT_NUM_BOOTSTRAP_SAMPLES = 20


@beam.ptransform_fn
@beam.typehints.with_input_types(Tuple[slicer.SliceKeyType, types.Extracts])
@beam.typehints.with_output_types(Tuple[slicer.SliceKeyType, Dict[Text, Any]])
def ComputeWithConfidenceIntervals(  # pylint: disable=invalid-name
    sliced_extracts: beam.pvalue.PCollection,
    compute_per_slice_metrics_cls: Type[beam.PTransform],
    num_bootstrap_samples: Optional[int] = DEFAULT_NUM_BOOTSTRAP_SAMPLES,
    random_seed_for_testing: Optional[int] = None,
    **kwargs) -> beam.pvalue.PCollection:
  """PTransform for computing metrics using T-Distribution values.

  Args:
    sliced_extracts: Incoming PCollection consisting of slice key and extracts.
    compute_per_slice_metrics_cls: PTransform class that takes a PCollection of
      (slice key, extracts) as input and returns (slice key, dict of metrics) as
      output. The class will be instantiated multiple times to compute metrics
      both with and without sampling. The class will be initialized using kwargs
      'compute_with_sampling' and 'random_seed_for_testing' along with any
      kwargs passed in **kwargs.
    num_bootstrap_samples: Number of replicas to use in calculating uncertainty
      using bootstrapping. If 1 is provided (default), aggregate metrics will be
      calculated with no uncertainty. If num_bootstrap_samples is > 0, multiple
      samples of each slice will be calculated using the Poisson bootstrap
      method. To calculate standard errors, num_bootstrap_samples should be 20
      or more in order to provide useful data. More is better, but you pay a
      performance cost.
    random_seed_for_testing: Seed to use for unit testing, because
      nondeterministic tests stink. Each partition will use this value + i.
    **kwargs: Additional args to pass to compute_per_slice_metrics_cls init.

  Returns:
    PCollection of (slice key, dict of metrics)
  """
  if not num_bootstrap_samples:
    num_bootstrap_samples = 1
  # TODO(ckuhn): Cap the number of bootstrap samples at 20.
  if num_bootstrap_samples < 1:
    raise ValueError('num_bootstrap_samples should be > 0, got %d' %
                     num_bootstrap_samples)

  output_results = (
      sliced_extracts
      | 'ComputeUnsampledMetrics' >> compute_per_slice_metrics_cls(
          compute_with_sampling=False, random_seed_for_testing=None, **kwargs))

  if num_bootstrap_samples > 1:
    multicombine = []
    for i in range(num_bootstrap_samples):
      seed = (None if random_seed_for_testing is None else
              random_seed_for_testing + i)
      multicombine.append(
          sliced_extracts
          | 'ComputeSampledMetrics%d' % i >> compute_per_slice_metrics_cls(
              compute_with_sampling=True,
              random_seed_for_testing=seed,
              **kwargs))
    output_results = (
        multicombine
        | 'FlattenBootstrapPartitions' >> beam.Flatten()
        | 'GroupBySlice' >> beam.GroupByKey()
        | 'MergeBootstrap' >> beam.ParDo(_MergeBootstrap(),
                                         beam.pvalue.AsDict(output_results)))
  return output_results


class _MergeBootstrap(beam.DoFn):
  """Merge the bootstrap values and fit a T-distribution to get confidence."""

  def process(
      self, element: Tuple[slicer.SliceKeyType, Iterable[Dict[Text, Any]]],
      unsampled_results: Dict[slicer.SliceKeyType, Dict[Text, Any]]
  ) -> Generator[Tuple[slicer.SliceKeyType, Dict[Text, Any]], None, None]:
    """Merge the bootstrap values.

    Args:
      element: The element is the tuple that contains slice key and a list of
        the metrics dict. It's the output of the GroupByKey step. All the
        metrics that under the same slice key are generated by
        poisson-bootstrap.
      unsampled_results: The unsampled_results is passed in as a side input.
        It's a tuple that contains the slice key and the metrics dict from a run
        of the slice with no sampling (ie, all examples in the set are
        represented exactly once.) This should be identical to the values
        obtained without sampling.

    Yields:
      A tuple of slice key and the metrics dict which contains the unsampled
      value, as well as parameters about t distribution.

    Raises:
      ValueError if the key of metrics inside element does not equal to the
      key of metrics in unsampled_results.
    """
    slice_key, metrics = element
    # metrics should be a list of dicts, but the dataflow runner has a quirk
    # that requires specific casting.
    metrics = list(metrics)
    if len(metrics) == 1:
      yield slice_key, metrics[0]
      return

    # Group the same metrics into one list.
    metrics_dict = {}
    for metric in metrics:
      for metrics_name in metric:
        if metrics_name not in metrics_dict:
          metrics_dict[metrics_name] = []
        metrics_dict[metrics_name].append(metric[metrics_name])

    unsampled_metrics_dict = unsampled_results.get(slice_key, {})

    # The key set of the two metrics dicts must be identical.
    if set(metrics_dict.keys()) != set(unsampled_metrics_dict.keys()):
      raise ValueError('Keys of two metrics do not match: sampled_metrics: %s. '
                       'unsampled_metrics: %s' %
                       (metrics_dict.keys(), unsampled_metrics_dict.keys()))

    metrics_with_confidence = {}
    for metrics_name in metrics_dict:
      metrics_with_confidence[metrics_name] = _calculate_t_distribution(
          metrics_dict[metrics_name], unsampled_metrics_dict[metrics_name])

    yield slice_key, metrics_with_confidence


def _calculate_t_distribution(  # pylint: disable=invalid-name
    sampling_data_list: List[Union[int, float, np.ndarray]],
    unsampled_data: Union[int, float, np.ndarray]):
  """Calculate the confidence interval of the data.

  Args:
    sampling_data_list: A list of number or np.ndarray.
    unsampled_data: Individual number or np.ndarray. The format of the
      unsampled_data should match the format of the element inside
      sampling_data_list.

  Returns:
    Confidence Interval value stored inside
    types.ValueWithTDistribution.
  """
  if isinstance(sampling_data_list[0], (np.ndarray, list)):
    merged_data = sampling_data_list[0][:]
    if isinstance(sampling_data_list[0], np.ndarray):
      merged_data = merged_data.astype(object)
    for index in range(len(merged_data)):
      merged_data[index] = _calculate_t_distribution(
          [data[index] for data in sampling_data_list], unsampled_data[index])
    return merged_data
  else:
    # Data has to be numeric. That means throw out nan values.
    sampling_data_list = [
        data for data in sampling_data_list if not np.isnan(data)
    ]
    n_samples = len(sampling_data_list)
    if n_samples:
      sample_mean = np.mean(sampling_data_list)
      sample_std = np.std(sampling_data_list, ddof=1)
      return types.ValueWithTDistribution(sample_mean, sample_std,
                                          n_samples - 1, unsampled_data)
    else:
      return types.ValueWithTDistribution(
          float('nan'), float('nan'), -1, float('nan'))