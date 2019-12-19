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
"""TF metric wrapper."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import importlib

from typing import Any, Dict, List, Optional, Text, Type, Tuple, Union

import apache_beam as beam
import numpy as np
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import model_util
from tensorflow_model_analysis import types
from tensorflow_model_analysis.metrics import binary_confusion_matrices
from tensorflow_model_analysis.metrics import metric_types
from tensorflow_model_analysis.metrics import metric_util

_DEFAULT_BATCH_SIZE = 1000

_CONFIG_KEY = 'config'
_NUM_THRESHOLDS_KEY = 'num_thresholds'
_THRESHOLDS_KEY = 'thresholds'
_CLASS_ID_KEY = 'class_id'
_TOP_K_KEY = 'top_k'
_DEFAULT_NUM_THRESHOLDS_IN_KERAS = 200

_TFMetricOrLoss = Union[tf.keras.metrics.Metric, tf.keras.losses.Loss]


def tf_metric_computations(
    metrics: Union[List[_TFMetricOrLoss], Dict[Text, List[_TFMetricOrLoss]]],
    eval_config: Optional[config.EvalConfig] = None,
    model_name: Text = '',
    sub_key: Optional[metric_types.SubKey] = None,
    class_weights: Optional[Dict[int, float]] = None,
    model_loader: Optional[types.ModelLoader] = None
) -> metric_types.MetricComputations:
  """Returns metric computations for the given TF metrics.

  Note that there is no requirement that a one to one mapping exist between the
  input metrics and the output metric computations. The implementation may
  combine multiple metrics into a single computation for efficency.

  Args:
    metrics: Dict from metric name to tf.keras.metrics.Metric or
      tf.keras.metrics.Loss. For multi-output models a dict of dicts may be
      passed where the first dict is indexed by the output_name. If the
      include_default_metrics option is enabled in the eval_config then the
      model's metrics will be merged with these metrics with the metrics passed
      here taking precendence.
    eval_config: Eval config.
    model_name: Optional model name (if multi-model evaluation).
    sub_key: Optional sub key.
    class_weights: Optional class weights to apply to multi-class / multi-label
      labels and predictions. This should only be used when micro averaging is
      being used.
    model_loader: Optional model loader. Only the non-compilable metrics will be
      evaluated using the model, all other metrics will be calculated directly
      in eager mode. However, the model is also used to add default metrics.

  Returns:
    Metric computations.
  """
  if eval_config and eval_config.options.HasField('desired_batch_size'):
    desired_batch_size = eval_config.options.desired_batch_size.value
  else:
    desired_batch_size = _DEFAULT_BATCH_SIZE

  if (model_loader is not None and eval_config and
      (not eval_config.options.HasField('include_default_metrics') or
       eval_config.options.include_default_metrics.value)):
    metrics = _combine_with_default_metrics(
        metrics,
        model_loader.construct_fn(lambda x: None)().keras_model)
  elif not isinstance(metrics, dict):
    metrics = {'': metrics}

  if class_weights is not None:
    sparse_metrics = _sparse_metrics(metrics)
    if sparse_metrics:
      raise ValueError(
          'sparse metrics cannot be used with aggregation options. Either '
          'disable aggregation settings or replace the sparse metrics with'
          'non-sparse versions: {}'.format(sparse_metrics))

  computations = []

  # For efficency, metrics are separated into compilable vs non-compilable
  # with the compilable metrics being further separated into confusion matrix
  # based vs non-confusion matrix based metrics. Non-compilable metrics are
  # calculated by calling model.evaluate() with the raw inputs expected by the
  # model's input layer. Since the confusion matrix based metrics can all be
  # calculated from the calibration histogram, these metrics are computed
  # separately as derived metrics. The remaining non-confusion matrix metrics
  # are calculated using batches of predictions/labels in eager mode (possibly
  # with additional pre-processing of the values to perform binarization, etc).
  #
  # Note that in theory if a model was provided, all the metrics could be
  # calculated by calling model.evaluate(). However, this call is inefficient
  # for confusion matrix based metrics given the large number of weights that
  # need to be calculated and the overlapping computations between the metrics.
  # In addition, some metrics and plots are only defined in TFMA so a separate
  # evaluation step would still be required. Lastly, if the metrics have any
  # binarization, etc applied the inputs and outputs will not match those
  # expected by the model. For these reasons, a separate implementation is used
  # for each specific use case. It also allows evaluations that are not
  # associated with a model (i.e. raw predictions are passed as input) to share
  # the same code path as model based evaluations where possible.
  compilable_metrics, non_compilable_metrics = _separate_compilable_metrics(
      metrics)
  confusion_matrix_metrics, non_confusion_matrix_metrics = (
      _separate_confusion_matrix_metrics(compilable_metrics))

  for output_name, metrics in confusion_matrix_metrics.items():
    for metric in metrics:
      computations.extend(
          _wrap_confusion_matrix_metric(metric, model_name, output_name,
                                        sub_key, class_weights))

  if non_confusion_matrix_metrics:
    custom_objects = _custom_objects(non_confusion_matrix_metrics)
    metric_keys, metric_configs, loss_configs = _metric_keys_and_configs(
        non_confusion_matrix_metrics, model_name, sub_key)
    computations.append(
        metric_types.MetricComputation(
            keys=metric_keys,
            preprocessor=None,
            combiner=_CompilableMetricsCombiner(metric_configs, loss_configs,
                                                custom_objects, sub_key,
                                                class_weights,
                                                desired_batch_size)))

  if non_compilable_metrics:
    computations.append(
        metric_types.MetricComputation(
            keys=metric_keys,
            preprocessor=None,
            combiner=_NonCompilableMetricsCombiner(model_loader, sub_key,
                                                   desired_batch_size)))

  return computations


def _sparse_metrics(
    metrics: Dict[Text, List[tf.keras.metrics.Metric]]
) -> Dict[Text, List[tf.keras.metrics.Metric]]:
  """Returns input metrics filtered to contain only the sparse metrics."""
  results = {}
  for k, v in metrics.items():
    for m in v:
      if m.__class__.__name__.startswith('Sparse'):
        if k not in results:
          results[k] = []
        results[k].append(m)
  return results


def _combine_with_default_metrics(
    metrics: Union[List[_TFMetricOrLoss], Dict[Text, List[_TFMetricOrLoss]]],
    model: Optional[tf.keras.models.Model]
) -> Dict[Optional[Text], List[_TFMetricOrLoss]]:
  """Combines metrics with default metrics provided by model."""
  if model is None:
    if not isinstance(metrics, dict):
      return {'': metrics}
    else:
      return metrics

  def metrics_for_output(output_name: Text) -> List[_TFMetricOrLoss]:
    if isinstance(metrics, dict):
      if output_name not in metrics:
        raise ValueError('output_name "{}" not found in metrics: '
                         'metrics={}'.format(output_name, metrics))
      return metrics[output_name]
    else:
      return metrics

  def default_metrics_for_output(output_name: Text) -> List[_TFMetricOrLoss]:
    """Returns default metrics for given output name."""
    output_metrics = []
    if isinstance(model.metrics, dict):
      if output_name not in model.metrics:
        raise ValueError('output_name "{}" not found in model.metrics: '
                         'model.metrics={}'.format(output_name, model.metrics))
      output_metrics.extend(model.metrics[output_name])
    else:
      output_metrics.extend(model.metrics)
    if isinstance(model.loss_functions, dict):
      if output_name not in model.metrics:
        raise ValueError('output_name "{}" not found in model.loss_functions: '
                         'model.loss_functions={}'.format(
                             output_name, model.loss_functions))
      output_metrics.extend(model.loss_functions[output_name])
    else:
      output_metrics.extend(model.loss_functions)
    return output_metrics

  def merge(metrics: List[_TFMetricOrLoss],
            default_metrics: List[_TFMetricOrLoss]) -> List[_TFMetricOrLoss]:
    merged = metrics[:]
    exists = {m.__class__.__name__: m for m in metrics}
    for m in default_metrics:
      if m.__class__.__name__ not in exists:
        merged.append(m)
    return merged

  output_names = []
  if isinstance(metrics, dict):
    output_names = list(metrics.keys())
  if isinstance(model.metrics, dict):
    output_names = output_names.extend(model.metrics.keys())
  output_names = list(set(output_names))
  if not output_names:
    output_names = ['']
  combined = {}
  for output_name in output_names:
    combined[output_name] = merge(
        metrics_for_output(output_name),
        default_metrics_for_output(output_name))
  return combined


def _separate_compilable_metrics(
    metrics: Dict[Optional[Text], List[_TFMetricOrLoss]]
) -> Tuple[Dict[Optional[Text], List[_TFMetricOrLoss]], Dict[
    Optional[Text], List[_TFMetricOrLoss]]]:
  """Separates the compilable metrics from non-compilable metrics."""
  # TODO(mdreves): Add support once compilable flag added to keras metrics
  return metrics, {}


def _separate_confusion_matrix_metrics(
    metrics: Dict[Optional[Text], List[_TFMetricOrLoss]]
) -> Tuple[Dict[Optional[Text], List[tf.keras.metrics.Metric]], Dict[
    Optional[Text], List[_TFMetricOrLoss]]]:
  """Separates the confusion matrix metrics from the other metrics."""
  confusion_matrix_metrics = {}
  non_confusion_matrix_metrics = {}
  for output_name, metrics in metrics.items():
    for metric in metrics:
      if (isinstance(metric, tf.keras.metrics.AUC) or
          isinstance(metric, tf.keras.metrics.SpecificityAtSensitivity) or
          isinstance(metric, tf.keras.metrics.SensitivityAtSpecificity) or
          isinstance(metric, tf.keras.metrics.TruePositives) or
          isinstance(metric, tf.keras.metrics.FalsePositives) or
          isinstance(metric, tf.keras.metrics.TrueNegatives) or
          isinstance(metric, tf.keras.metrics.FalseNegatives)):
        if output_name not in confusion_matrix_metrics:
          confusion_matrix_metrics[output_name] = []
        confusion_matrix_metrics[output_name].append(metric)
      elif (isinstance(metric, tf.keras.metrics.Precision) or
            isinstance(metric, tf.keras.metrics.Recall)):
        if output_name not in confusion_matrix_metrics:
          confusion_matrix_metrics[output_name] = []
        confusion_matrix_metrics[output_name].append(metric)
      else:
        if output_name not in non_confusion_matrix_metrics:
          non_confusion_matrix_metrics[output_name] = []
        non_confusion_matrix_metrics[output_name].append(metric)
  return confusion_matrix_metrics, non_confusion_matrix_metrics


def _verify_and_update_sub_key(model_name: Text, output_name: Text,
                               sub_key: metric_types.SubKey,
                               metric: _TFMetricOrLoss):
  """Verifies the multi-class metric key matches settings used by the metric."""
  if hasattr(metric, _CLASS_ID_KEY) and metric.class_id is not None:
    if sub_key and sub_key.class_id != metric.class_id:
      raise ValueError(
          '{} tf.keras.metric has class_id = {}, but the metric is being added '
          'using sub_key = {}: model_name={}, output_name={}'.format(
              metric.name, metric.class_id, sub_key, model_name, output_name))
    return metric_types.SubKey(class_id=metric.class_id)
  elif hasattr(metric, _TOP_K_KEY) and metric.top_k is not None:
    if sub_key and sub_key.top_k != metric.top_k:
      raise ValueError(
          '{} tf.keras.metric has top_k = {}, but the metric is being added '
          'using sub_key = {}: model_name={}, output_name={}'.format(
              metric.name, metric.top_k, sub_key, model_name, output_name))
    return metric_types.SubKey(top_k=metric.top_k)
  else:
    return sub_key


def _metric_keys_and_configs(
    metrics: Dict[Text, List[_TFMetricOrLoss]], model_name: Text,
    sub_key: Optional[metric_types.SubKey]
) -> Tuple[List[metric_types.MetricKey], Dict[Text, List[Dict[Text, Any]]],
           Dict[Text, List[Dict[Text, Any]]]]:
  """Returns the metric keys, metric configs, and loss configs for metrics."""
  metric_keys = []
  metric_configs = {}
  loss_configs = {}
  for output_name, metrics_list in metrics.items():
    metric_config_list = []
    loss_config_list = []
    for metric in metrics_list:
      sub_key = _verify_and_update_sub_key(model_name, output_name, sub_key,
                                           metric)
      metric_keys.append(
          metric_types.MetricKey(
              name=metric.name,
              model_name=model_name,
              output_name=output_name,
              sub_key=sub_key))
      if isinstance(metric, tf.keras.metrics.Metric):
        metric_config_list.append(tf.keras.metrics.serialize(metric))
      elif isinstance(metric, tf.keras.losses.Loss):
        loss_config_list.append(tf.keras.losses.serialize(metric))

    metric_configs[output_name] = metric_config_list
    loss_configs[output_name] = loss_config_list
  return metric_keys, metric_configs, loss_configs


def _deserialize_metrics(
    metric_configs: List[Dict[Text, Any]]) -> List[tf.keras.metrics.Metric]:
  return [tf.keras.metrics.deserialize(c) for c in metric_configs]


def _deserialize_losses(
    loss_configs: List[Dict[Text, Any]]) -> List[tf.keras.losses.Loss]:
  return [tf.keras.losses.deserialize(c) for c in loss_configs]


def _custom_objects(
    metrics: Dict[Text, List[tf.keras.metrics.Metric]]) -> Dict[Text, Any]:
  custom_objects = {}
  for metric_list in metrics.values():
    for metric in metric_list:
      if (not metric.__class__.__module__.endswith('keras.metrics') and
          not metric.__class__.__module__.endswith('keras.losses')):
        custom_objects[metric.__class__.__module__] = metric.__class__.__name__
  return custom_objects


def _load_custom_objects(
    custom_objects: Dict[Text, Text]) -> Dict[Text, Type[Any]]:
  """Loads custom metric options."""
  loaded_custom_objects = {}
  for module_name, class_name in custom_objects.items():
    module = importlib.import_module(module_name)
    loaded_custom_objects[class_name] = getattr(module, class_name)
  return loaded_custom_objects


def _wrap_confusion_matrix_metric(
    metric: tf.keras.metrics.Metric, model_name: Text, output_name: Text,
    sub_key: Optional[metric_types.SubKey],
    class_weights: Optional[Dict[int,
                                 float]]) -> metric_types.MetricComputations:
  """Returns confusion matrix metric wrapped in a more efficient computation."""

  # Special handling for AUC metric which supports aggregation inherently via
  # multi_label flag.
  if (isinstance(metric, tf.keras.metrics.AUC) and
      hasattr(metric, 'label_weights')):
    if metric.label_weights:
      if class_weights:
        raise ValueError(
            'class weights are configured in two different places: (1) via the '
            'tf.keras.metrics.AUC class (using "label_weights") and (2) via '
            'the MetricsSpecs (using "aggregate.class_weights"). Either remove '
            'the label_weights settings in the AUC class or remove the '
            'class_weights from the AggregationOptions: metric={}, '
            'class_weights={}'.format(metric, class_weights))
      class_weights = {i: v for i, v in enumerate(metric.label_weights)}
    if metric.multi_label:
      raise NotImplementedError('AUC.multi_label=True is not implemented yet.')

  sub_key = _verify_and_update_sub_key(model_name, output_name, sub_key, metric)
  key = metric_types.MetricKey(
      name=metric.name,
      model_name=model_name,
      output_name=output_name,
      sub_key=sub_key)

  metric_config = tf.keras.metrics.serialize(metric)

  # By default use separate compuations for the confusion matrices since the
  # metrics might be using different thresholds (note, the underlying histogram
  # the confusion matrices are based on will still only be calculated once).
  name = '_{}{}'.format(
      metric.name, binary_confusion_matrices.BINARY_CONFUSION_MATRICES_NAME)
  thresholds = None
  if hasattr(metric, _THRESHOLDS_KEY):
    thresholds = metric.thresholds
  num_thresholds = None
  if hasattr(metric, _NUM_THRESHOLDS_KEY):
    num_thresholds = metric.num_thresholds
  # Increase the default number of thresholds if keras defaults were used (this
  # also allows us to share the computation with other confusion based metrics).
  if (num_thresholds == _DEFAULT_NUM_THRESHOLDS_IN_KERAS and
      _CONFIG_KEY in metric_config and
      _NUM_THRESHOLDS_KEY in metric_config[_CONFIG_KEY]):
    name = binary_confusion_matrices.BINARY_CONFUSION_MATRICES_NAME
    num_thresholds = binary_confusion_matrices.DEFAULT_NUM_THRESHOLDS
    metric_config[_CONFIG_KEY][_NUM_THRESHOLDS_KEY] = num_thresholds
    thresholds = None
    if _THRESHOLDS_KEY in metric_config[_CONFIG_KEY]:
      metric_config[_CONFIG_KEY][_THRESHOLDS_KEY] = None
  # Only one of either thresholds or num_thresholds should be used. Keras AUC
  # allows both but thresholds has more precedence.
  if thresholds is not None and num_thresholds is not None:
    num_thresholds = None

  # Make sure matrices are calculated. Note that the use of class_weights here
  # implies that micro averaging is being performed.
  computations = binary_confusion_matrices.binary_confusion_matrices(
      num_thresholds=num_thresholds,
      thresholds=thresholds,
      name=name,
      model_name=model_name,
      output_name=output_name,
      sub_key=sub_key,
      class_weights=class_weights)
  matrices_key = computations[-1].keys[-1]

  def result(
      metrics: Dict[metric_types.MetricKey, Any]
  ) -> Dict[metric_types.MetricKey, Any]:
    """Returns AUC derived from binary confustion matrices."""
    matrices = metrics[matrices_key]

    metric = tf.keras.metrics.deserialize(metric_config)
    if (isinstance(metric, tf.keras.metrics.AUC) or
        isinstance(metric, tf.keras.metrics.SpecificityAtSensitivity) or
        isinstance(metric, tf.keras.metrics.SensitivityAtSpecificity)):
      metric.true_positives.assign(np.array(matrices.tp))
      metric.true_negatives.assign(np.array(matrices.tn))
      metric.false_positives.assign(np.array(matrices.fp))
      metric.false_negatives.assign(np.array(matrices.fn))
    elif isinstance(metric, tf.keras.metrics.Precision):
      metric.true_positives.assign(np.array(matrices.tp))
      metric.false_positives.assign(np.array(matrices.fp))
    elif isinstance(metric, tf.keras.metrics.Recall):
      metric.true_positives.assign(np.array(matrices.tp))
      metric.false_negatives.assign(np.array(matrices.fn))
    elif isinstance(metric, tf.keras.metrics.TruePositives):
      metric.accumulator.assign(np.array(matrices.tp))
    elif isinstance(metric, tf.keras.metrics.FalsePositives):
      metric.accumulator.assign(np.array(matrices.fp))
    elif isinstance(metric, tf.keras.metrics.TrueNegatives):
      metric.accumulator.assign(np.array(matrices.tn))
    elif isinstance(metric, tf.keras.metrics.FalseNegatives):
      metric.accumulator.assign(np.array(matrices.fn))
    return {key: metric.result().numpy()}

  derived_computation = metric_types.DerivedMetricComputation(
      keys=[key], result=result)
  computations.append(derived_computation)
  return computations


class _LossMetric(tf.keras.metrics.Mean):
  """Converts a loss function into a metric."""

  def __init__(self, loss, name=None, dtype=None):
    if name is None:
      name = loss.name
    super(_LossMetric, self).__init__(name=name, dtype=dtype)
    self.loss = loss

  def update_state(self, y_true, y_pred, sample_weight):
    return super(_LossMetric, self).update_state(
        self.loss(y_true, y_pred), sample_weight=sample_weight)


class _CompilableMetricsAccumulator(object):
  """Accumulator for compilable metrics.

  Attributes:
    inputs: Accumulated batch of inputs (max size of desired_batch_size). The
      inputs are stored in a multi-dimensional list. The first dimension is used
      to index the associated output (for single-output models this will only
      have one item). The second dimension is used to store the args passed to
      update_state (i.e. (y_true, y_pred, example_weight)). Batching is done on
      the last dimension.
    weights: Accumulated weights. The weights are stored in a multi-dimensional
      list where the first dimension is used to index the associated output (for
      single-output models this will only have one item). The second dimension
      is used to store the accumulated weights for each metric associated with
      the output dimension.
  """
  __slots__ = ['inputs', 'weights']

  def __init__(self, metric_counts: List[int]):
    """Initializes accumulator using a list of metric counts per output."""
    # Inputs have shape (num_outputs, num_metrics, num_accumulated_inputs)
    self.inputs = [
    ]  # type: List[Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray]]]
    # Weights have shape (num_outputs, num_metrics)
    self.weights = []  # type: List[List[Optional[np.ndarray]]]
    for output_metric_count in metric_counts:
      self.inputs.append(([], [], []))
      self.weights.append([None] * output_metric_count)

  def len_inputs(self):
    return len(self.inputs[0][0])

  def add_input(self, output_index: int, label: np.ndarray,
                prediction: np.ndarray, example_weight: np.ndarray):
    for i, v in enumerate((label, prediction, example_weight)):
      self.inputs[output_index][i].append(v)

  def get_inputs(
      self, output_index: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (np.array(self.inputs[output_index][0]),
            np.array(self.inputs[output_index][1]),
            np.array(self.inputs[output_index][2]))

  def clear_inputs(self):
    for output_index in range(len(self.inputs)):
      for i in (0, 1, 2):
        del self.inputs[output_index][i][:]

  def add_weights(self, output_index: int, metric_index: int,
                  weights: np.ndarray):
    cur_weights = self.weights[output_index][metric_index]
    if cur_weights is None:
      self.weights[output_index][metric_index] = weights
    else:
      self.weights[output_index][metric_index] = np.add(cur_weights, weights)

  def get_weights(self, output_index: int,
                  metric_index: int) -> Optional[np.ndarray]:
    return self.weights[output_index][metric_index]


class _CompilableMetricsCombiner(beam.CombineFn):
  """Combines compilable metric weights and computes result."""

  def __init__(self, metric_configs: Dict[Text, List[Dict[Text, Any]]],
               loss_configs: Dict[Text, List[Dict[Text, Any]]],
               custom_objects: Dict[Text, Type[Any]],
               sub_key: Optional[metric_types.SubKey],
               class_weights: Dict[int, float], batch_size: int):
    # Use parallel lists to store output_names and configs to guarantee
    # consistent ordering and for natural alignment with the accumulator where
    # lists are used instead of dicts for efficency.
    self._output_names = sorted(metric_configs.keys())
    self._metric_configs = [metric_configs[n] for n in self._output_names]
    self._loss_configs = [loss_configs[n] for n in self._output_names]
    self._custom_objects = custom_objects
    self._sub_key = sub_key
    self._class_weights = class_weights
    self._batch_size = batch_size
    self._metrics = None  # type: Dict[Text, List[tf.keras.metrics.Metric]]

  def _setup_if_needed(self):
    if self._metrics is None:
      self._metrics = {}
      with tf.keras.utils.custom_object_scope(
          _load_custom_objects(self._custom_objects)):
        for i, output_name in enumerate(self._output_names):
          self._metrics[output_name] = (
              _deserialize_metrics(self._metric_configs[i]))
          for loss in _deserialize_losses(self._loss_configs[i]):
            self._metrics[output_name].append(_LossMetric(loss))

  def _process_batch(self, accumulator: _CompilableMetricsAccumulator):
    self._setup_if_needed()
    if accumulator.len_inputs() == 0:
      return
    for output_index, output_name in enumerate(self._output_names):
      inputs = accumulator.get_inputs(output_index)
      for metric_index, metric in enumerate(self._metrics[output_name]):
        metric.reset_states()
        metric.update_state(*inputs)
        accumulator.add_weights(output_index, metric_index,
                                metric.get_weights())
    accumulator.clear_inputs()

  def create_accumulator(self) -> _CompilableMetricsAccumulator:
    configs = zip(self._metric_configs, self._loss_configs)
    return _CompilableMetricsAccumulator([len(m) + len(l) for m, l in configs])

  def add_input(
      self, accumulator: _CompilableMetricsAccumulator,
      element: metric_types.StandardMetricInputs
  ) -> _CompilableMetricsAccumulator:
    for i, output_name in enumerate(self._output_names):
      # The use of class_weights means that micro averaging is being used. When
      # micro averaging is being used, flatten should be set to True so that
      # each class is treated as though it was an independent example.
      for label, prediction, example_weight in (
          metric_util.to_label_prediction_example_weight(
              element,
              output_name=output_name,
              sub_key=self._sub_key,
              class_weights=self._class_weights,
              flatten=self._class_weights is not None)):
        accumulator.add_input(i, label, prediction, example_weight)
    if accumulator.len_inputs() >= self._batch_size:
      self._process_batch(accumulator)
    return accumulator

  def merge_accumulators(
      self, accumulators: List[_CompilableMetricsAccumulator]
  ) -> _CompilableMetricsAccumulator:
    result = self.create_accumulator()
    for accumulator in accumulators:
      # Finish processing last batch
      self._process_batch(accumulator)
      # Merge the weights
      for output_index in range(len(self._output_names)):
        for metric_index in range(len(self._metric_configs[output_index])):
          weights = accumulator.get_weights(output_index, metric_index)
          if weights is None:
            # It is possible for beam to create an accumulator but pass no
            # inputs to it resulting in in empty weights. In theory all weights
            # should be empty but we check on a per metric weights basis.
            continue
          result.add_weights(output_index, metric_index, weights)
    return result

  def extract_output(
      self, accumulator: _CompilableMetricsAccumulator
  ) -> Dict[metric_types.MetricKey, Any]:
    self._process_batch(accumulator)
    result = {}
    for output_index, output_name in enumerate(self._output_names):
      for metric_index, metric in enumerate(self._metrics[output_name]):
        key = metric_types.MetricKey(
            name=metric.name, output_name=output_name, sub_key=self._sub_key)
        weights = accumulator.get_weights(output_index, metric_index)
        if weights is not None:
          metric.set_weights(weights)
        else:
          metric.reset_states()
        result[key] = metric.result().numpy()
    return result


class _NonCompilableMetricsAccumulator(object):
  pass


class _NonCompilableMetricsCombiner(model_util.CombineFnWithModels):
  """Combines non-compilable metric weights and computes result."""

  def __init__(self, model_loader: types.ModelLoader,
               sub_key: Optional[metric_types.SubKey], batch_size: int):
    super(_NonCompilableMetricsCombiner, self).__init__({'': model_loader})
    self._sub_key = sub_key
    self._batch_size = batch_size

  def _setup_if_needed(self):
    super(_NonCompilableMetricsCombiner, self)._setup_if_needed()
    raise NotImplementedError('not implemented')

  def _process_batch(self, accumulator: _NonCompilableMetricsAccumulator):
    self._setup_if_needed()
    raise NotImplementedError('not implemented')

  def create_accumulator(self) -> _NonCompilableMetricsAccumulator:
    raise NotImplementedError('not implemented')

  def add_input(self, accumulator: _NonCompilableMetricsAccumulator,
                element: Any) -> _NonCompilableMetricsAccumulator:
    raise NotImplementedError('not implemented')

  def merge_accumulators(
      self, accumulators: List[_NonCompilableMetricsAccumulator]
  ) -> _NonCompilableMetricsAccumulator:
    raise NotImplementedError('not implemented')

  def extract_output(self,
                     accumulator: _NonCompilableMetricsAccumulator) -> Any:
    raise NotImplementedError('not implemented')
