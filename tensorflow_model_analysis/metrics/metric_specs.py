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
"""Specifications for common metrics."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import importlib
import json
import re
from typing import Any, Dict, List, Optional, Text, Type, Union
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import types
from tensorflow_model_analysis.metrics import aggregation
from tensorflow_model_analysis.metrics import calibration
from tensorflow_model_analysis.metrics import calibration_plot
from tensorflow_model_analysis.metrics import confusion_matrix_plot
from tensorflow_model_analysis.metrics import example_count
from tensorflow_model_analysis.metrics import metric_types
from tensorflow_model_analysis.metrics import multi_class_confusion_matrix_plot
from tensorflow_model_analysis.metrics import tf_metric_wrapper
from tensorflow_model_analysis.metrics import weighted_example_count

_TFOrTFMAMetric = Union[tf.keras.metrics.Metric, metric_types.Metric]


def specs_from_metrics(
    metrics: Union[List[_TFOrTFMAMetric], Dict[Text, List[_TFOrTFMAMetric]]],
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    binarize: Optional[config.BinarizationOptions] = None,
    aggregate: Optional[config.AggregationOptions] = None,
    query_key: Optional[Text] = None,
    include_example_count: Optional[bool] = None,
    include_weighted_example_count: Optional[bool] = None
) -> List[config.MetricsSpec]:
  """Returns specs from tf.keras.metrics.Metric or tfma.metrics.Metric classes.

  Examples:

    metrics_specs = specs_from_metrics([
        tf.keras.metrics.BinaryAccuracy(),
        tf.keras.metrics.AUC(),
        tf.keras.metrics.Precision(),
        tf.keras.metrics.Recall(),
        tfma.metrics.MeanLabel(),
        tfma.metrics.MeanPrediction()
        ...
    ])

    metrics_specs = specs_from_metrics({
      'output1': [
          tf.keras.metrics.BinaryAccuracy(),
          tf.keras.metrics.AUC(),
          tfma.metrics.MeanLabel(),
          tfma.metrics.MeanPrediction()
          ...
      ],
      'output2': [
          tf.keras.metrics.Precision(),
          tf.keras.metrics.Recall(),
      ]
    })

  Args:
    metrics: List of tf.keras.metrics.Metric or tfma.metrics.Metric. For
      multi-output models a dict of dicts may be passed where the first dict is
      indexed by the output_name.
    model_names: Optional model names (if multi-model evaluation).
    output_names: Optional output names (if multi-output models). If the metrics
      are a dict this should not be set.
    binarize: Optional settings for binarizing multi-class/multi-label metrics.
    aggregate: Optional settings for aggregating multi-class/multi-label
      metrics.
    query_key: Optional query key for query/ranking based metrics.
    include_example_count: True to add example_count metric. Default is True.
    include_weighted_example_count: True to add weighted_example_count metric.
      Default is True. A weighted example count will be added per output for
      multi-output models.
  """
  if isinstance(metrics, dict) and output_names:
    raise ValueError('metrics cannot be a dict when output_names is used: '
                     'metrics={}, output_names={}'.format(
                         metrics, output_names))
  if isinstance(metrics, dict):
    specs = []
    for output_name in sorted(metrics.keys()):
      specs.extend(
          specs_from_metrics(
              metrics[output_name],
              model_names=model_names,
              output_names=[output_name],
              binarize=binarize,
              aggregate=aggregate,
              include_example_count=include_example_count,
              include_weighted_example_count=include_weighted_example_count))
      include_example_count = False
    return specs

  if include_example_count is None:
    include_example_count = True
  if include_weighted_example_count is None:
    include_weighted_example_count = True

  # Add the computations for the example counts and weights since they are
  # independent of the model and class ID.
  specs = example_count_specs(
      model_names=model_names,
      output_names=output_names,
      include_example_count=include_example_count,
      include_weighted_example_count=include_weighted_example_count)

  metric_configs = []
  for metric in metrics:
    if isinstance(metric, tf.keras.metrics.Metric):
      metric_configs.append(_serialize_tf_metric(metric))
    else:
      metric_configs.append(_serialize_tfma_metric(metric))
  specs.append(
      config.MetricsSpec(
          metrics=metric_configs,
          model_names=model_names,
          output_names=output_names,
          binarize=binarize,
          aggregate=aggregate,
          query_key=query_key))

  return specs


def example_count_specs(
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    include_example_count: bool = True,
    include_weighted_example_count: bool = True) -> List[config.MetricsSpec]:
  """Returns metric specs for example count and weighted example counts.

  Args:
    model_names: Optional list of model names (if multi-model evaluation).
    output_names: Optional list of output names (if multi-output model).
    include_example_count: True to add example_count metric.
    include_weighted_example_count: True to add weighted_example_count metric. A
      weighted example count will be added per output for multi-output models.
  """
  specs = []
  if include_example_count:
    metric_config = _serialize_tfma_metric(example_count.ExampleCount())
    specs.append(config.MetricsSpec(metrics=[metric_config]))
  if include_weighted_example_count:
    metric_config = _serialize_tfma_metric(
        weighted_example_count.WeightedExampleCount())
    specs.append(
        config.MetricsSpec(
            metrics=[metric_config],
            model_names=model_names,
            output_names=output_names))
  return specs


def default_regression_specs(
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    loss_functions: Optional[List[tf.keras.metrics.Metric]] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None) -> List[config.MetricsSpec]:
  """Returns default metric specs for for regression problems.

  Args:
    model_names: Optional model names (if multi-model evaluation).
    output_names: Optional list of output names (if multi-output model).
    loss_functions: Loss functions to use (if None MSE is used).
    min_value: Min value for calibration plot (if None no plot will be created).
    max_value: Max value for calibration plot (if None no plot will be created).
  """

  if loss_functions is None:
    loss_functions = [tf.keras.metrics.MeanSquaredError(name='mse')]

  metrics = [
      tf.keras.metrics.Accuracy(name='accuracy'),
      calibration.MeanLabel(name='mean_label'),
      calibration.MeanPrediction(name='mean_prediction'),
      calibration.Calibration(name='calibration'),
  ]
  for fn in loss_functions:
    metrics.append(fn)
  if min_value is not None and max_value is not None:
    metrics.append(
        calibration_plot.CalibrationPlot(
            name='calibration_plot', left=min_value, right=max_value))

  return specs_from_metrics(
      metrics, model_names=model_names, output_names=output_names)


def default_binary_classification_specs(
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    binarize: Optional[config.BinarizationOptions] = None,
    aggregate: Optional[config.AggregationOptions] = None,
    include_loss: bool = True) -> List[config.MetricsSpec]:
  """Returns default metric specs for binary classification problems.

  Args:
    model_names: Optional model names (if multi-model evaluation).
    output_names: Optional list of output names (if multi-output model).
    binarize: Optional settings for binarizing multi-class/multi-label metrics.
    aggregate: Optional settings for aggregating multi-class/multi-label
      metrics.
    include_loss: True to include loss.
  """

  metrics = [
      tf.keras.metrics.BinaryAccuracy(name='accuracy'),
      tf.keras.metrics.AUC(name='auc'),
      tf.keras.metrics.AUC(name='auc_pr', curve='PR'),
      tf.keras.metrics.Precision(name='precision'),
      tf.keras.metrics.Recall(name='recall'),
      calibration.MeanLabel(name='mean_label'),
      calibration.MeanPrediction(name='mean_prediction'),
      calibration.Calibration(name='calibration'),
      confusion_matrix_plot.AUCPlot(name='confusion_matrix_plot'),
      calibration_plot.CalibrationPlot(name='calibration_plot')
  ]
  if include_loss:
    metrics.append(tf.keras.metrics.BinaryCrossentropy(name='loss'))

  return specs_from_metrics(
      metrics,
      model_names=model_names,
      output_names=output_names,
      binarize=binarize,
      aggregate=aggregate)


def default_multi_class_classification_specs(
    model_names: Optional[List[Text]] = None,
    output_names: Optional[List[Text]] = None,
    binarize: Optional[config.BinarizationOptions] = None,
    aggregate: Optional[config.AggregationOptions] = None,
    sparse: bool = True) -> config.MetricsSpec:
  """Returns default metric specs for multi-class classification problems.

  Args:
    model_names: Optional model names if multi-model evaluation.
    output_names: Optional list of output names (if multi-output model).
    binarize: Optional settings for binarizing multi-class/multi-label metrics.
    aggregate: Optional settings for aggregating multi-class/multi-label
      metrics.
    sparse: True if the labels are sparse.
  """

  if sparse:
    metrics = [
        tf.keras.metrics.SparseCategoricalCrossentropy(name='loss'),
        tf.keras.metrics.SparseCategoricalAccuracy(name='accuracy')
    ]
  else:
    metrics = [
        tf.keras.metrics.CategoricalCrossentropy(name='loss'),
        tf.keras.metrics.CategoricalAccuracy(name='accuracy')
    ]
  metrics.append(
      multi_class_confusion_matrix_plot.MultiClassConfusionMatrixPlot())
  if binarize is not None:
    for top_k in binarize.top_k_list:
      metrics.extend([
          tf.keras.metrics.Precision(name='precision', top_k=top_k),
          tf.keras.metrics.Recall(name='recall', top_k=top_k)
      ])
    binarize = config.BinarizationOptions().CopyFrom(binarize)
    binarize.ClearField('top_k')
  multi_class_metrics = specs_from_metrics(
      metrics, model_names=model_names, output_names=output_names)
  if aggregate is None:
    aggregate = config.AggregationOptions(micro_average=True)
  multi_class_metrics.extend(
      default_binary_classification_specs(
          model_names=model_names,
          output_names=output_names,
          binarize=binarize,
          aggregate=aggregate))
  return multi_class_metrics


def to_computations(
    metrics_specs: List[config.MetricsSpec],
    eval_config: Optional[config.EvalConfig] = None,
    model_loaders: Optional[Dict[Text, types.ModelLoader]] = None
) -> metric_types.MetricComputations:
  """Returns computations associated with given metrics specs."""
  computations = []

  #
  # Split into TF metrics and TFMA metrics
  #

  # Dict[Text, Type[tf.keras.metrics.Metric]]
  tf_metric_classes = {}  # class_name -> class
  # List[metric_types.MetricsSpec]
  tf_metrics_specs = []
  # Dict[Text, Type[metric_types.Metric]]
  tfma_metric_classes = metric_types.registered_metrics()  # class_name -> class
  # List[metric_types.MetricsSpec]
  tfma_metrics_specs = []
  #
  # Note: Lists are used instead of Dicts for the following items because
  # protos are are no hashable.
  #
  # List[List[_TFOrTFMAMetric]] (offsets align with metrics_specs).
  per_spec_metric_instances = []
  # List[List[tf.keras.metrics.Metric]] (offsets align with tf_metrics_specs).
  per_tf_spec_metric_instances = []
  # List[List[metric_types.Metric]]] (offsets align with tfma_metrics_specs).
  per_tfma_spec_metric_instances = []
  for spec in metrics_specs:
    tf_spec = config.MetricsSpec()
    tf_spec.CopyFrom(spec)
    del tf_spec.metrics[:]
    tfma_spec = config.MetricsSpec()
    tfma_spec.CopyFrom(spec)
    del tfma_spec.metrics[:]
    for metric in spec.metrics:
      if metric.class_name in tfma_metric_classes:
        tfma_spec.metrics.append(metric)
      elif not metric.module:
        tf_spec.metrics.append(metric)
      else:
        cls = getattr(importlib.import_module(metric.module), metric.class_name)
        if issubclass(cls, tf.keras.metrics.Metric):
          tf_metric_classes[metric.class_name] = cls
          tf_spec.metrics.append(metric)
        else:
          tfma_metric_classes[metric.class_name] = cls
          tfma_spec.metrics.append(metric)

    metric_instances = []
    if tf_spec.metrics:
      tf_metrics_specs.append(tf_spec)
      tf_metric_instances = [
          _deserialize_tf_metric(m, tf_metric_classes) for m in tf_spec.metrics
      ]
      per_tf_spec_metric_instances.append(tf_metric_instances)
      metric_instances.extend(tf_metric_instances)
    if tfma_spec.metrics:
      tfma_metrics_specs.append(tfma_spec)
      tfma_metric_instances = [
          _deserialize_tfma_metric(m, tfma_metric_classes)
          for m in tfma_spec.metrics
      ]
      per_tfma_spec_metric_instances.append(tfma_metric_instances)
      metric_instances.extend(tfma_metric_instances)
    per_spec_metric_instances.append(metric_instances)

  #
  # Group TF metrics by the subkeys, models and outputs. This is done in reverse
  # because model and subkey processing is done outside of TF and so each unique
  # sub key combination needs to be run through a separate model instance. Note
  # that output_names are handled by the tf_metric_computation since all the
  # outputs are batch calculated in a single model evaluation call.
  #

  # Dict[metric_types.SubKey, Dict[Text, List[int]]
  tf_spec_indices_by_subkey = {}  # SubKey -> model_name -> [index(MetricSpec)]
  for i, spec in enumerate(tf_metrics_specs):
    sub_keys = _create_sub_keys(spec)
    if not sub_keys:
      sub_keys = [None]
    for sub_key in sub_keys:
      if sub_key not in tf_spec_indices_by_subkey:
        tf_spec_indices_by_subkey[sub_key] = {}
      # Dict[Text, List[config.MetricSpec]]
      tf_spec_indices_by_model = (tf_spec_indices_by_subkey[sub_key]
                                 )  # name -> [ModelSpec]
      model_names = spec.model_names
      if not model_names:
        model_names = ['']  # '' is name used when only one model is used
      for model_name in model_names:
        if model_name not in tf_spec_indices_by_model:
          tf_spec_indices_by_model[model_name] = []
        tf_spec_indices_by_model[model_name].append(i)
  for sub_key, spec_indices_by_model in tf_spec_indices_by_subkey.items():
    for model_name, indices in spec_indices_by_model.items():
      metrics_by_output = {}
      for i in indices:
        metrics = per_tf_spec_metric_instances[i]
        if tf_metrics_specs[i].output_names:
          for output_name in tf_metrics_specs[i].output_names:
            if output_name not in metrics_by_output:
              metrics_by_output[output_name] = []
            metrics_by_output[output_name].extend(metrics)
        else:
          if '' not in metrics_by_output:
            metrics_by_output[''] = []  # '' is name used when only one output
          metrics_by_output[''].extend(metrics)
      model_loader = None
      if model_loaders and model_name in model_loaders:
        model_loader = model_loaders[model_name]
      class_weights = None
      if tf_metrics_specs[i].HasField('aggregate'):
        class_weights = dict(tf_metrics_specs[i].aggregate.class_weights)
      computations.extend(
          tf_metric_wrapper.tf_metric_computations(
              metrics_by_output,
              eval_config=eval_config,
              model_name=model_name,
              sub_key=sub_key,
              class_weights=class_weights,
              model_loader=model_loader))

  #
  # Group TFMA metric specs by the metric classes
  #

  # Dict[bytes, List[config.MetricSpec]]
  tfma_specs_by_metric_config = {}  # hash(MetricConfig) -> [MetricSpec]
  # Dict[bytes, metric_types.Metric]
  hashed_metrics = {}  # hash(MetricConfig) -> Metric
  for i, spec in enumerate(tfma_metrics_specs):
    for metric_config, metric in zip(spec.metrics,
                                     per_tfma_spec_metric_instances[i]):
      # Note that hashing by SerializeToString() is only safe if used within the
      # same process.
      config_hash = metric_config.SerializeToString()
      if config_hash not in tfma_specs_by_metric_config:
        hashed_metrics[config_hash] = metric
        tfma_specs_by_metric_config[config_hash] = []
      tfma_specs_by_metric_config[config_hash].append(spec)
  for config_hash, specs in tfma_specs_by_metric_config.items():
    metric = hashed_metrics[config_hash]
    for spec in specs:
      sub_keys = _create_sub_keys(spec)
      class_weights = None
      if spec.HasField('aggregate'):
        class_weights = dict(spec.aggregate.class_weights)
      computations.extend(
          metric.computations(
              eval_config=eval_config,
              model_names=spec.model_names if spec.model_names else [''],
              output_names=spec.output_names if spec.output_names else [''],
              sub_keys=sub_keys,
              class_weights=class_weights,
              query_key=spec.query_key))

  #
  # Create macro averaging metrics
  #

  for i, spec in enumerate(metrics_specs):
    if spec.aggregate.macro_average or spec.aggregate.weighted_macro_average:
      sub_keys = _create_sub_keys(spec)
      if sub_keys is None:
        raise ValueError(
            'binarize settings are required when aggregate.macro_average or '
            'aggregate.weighted_macro_average is used: spec={}'.format(spec))
      for model_name in spec.model_names or ['']:
        for output_name in spec.output_names or ['']:
          for metric in per_spec_metric_instances[i]:
            if spec.aggregate.macro_average:
              computations.extend(
                  aggregation.macro_average(
                      metric.get_config()['name'],
                      eval_config=eval_config,
                      model_name=model_name,
                      output_name=output_name,
                      sub_keys=sub_keys,
                      class_weights=dict(spec.aggregate.class_weights)))
            elif spec.aggregate.weighted_macro_average:
              computations.extend(
                  aggregation.weighted_macro_average(
                      metric.get_config()['name'],
                      eval_config=eval_config,
                      model_name=model_name,
                      output_name=output_name,
                      sub_keys=sub_keys,
                      class_weights=dict(spec.aggregate.class_weights)))

  return computations


def _create_sub_keys(
    spec: config.MetricsSpec) -> Optional[List[metric_types.SubKey]]:
  """Creates subkeys associated with spec."""
  sub_keys = None
  if spec.HasField('binarize'):
    sub_keys = []
    if spec.binarize.class_ids:
      for v in spec.binarize.class_ids:
        sub_keys.append(metric_types.SubKey(class_id=v))
    if spec.binarize.k_list:
      for v in spec.binarize.k_list:
        sub_keys.append(metric_types.SubKey(k=v))
    if spec.binarize.top_k_list:
      for v in spec.binarize.top_k_list:
        sub_keys.append(metric_types.SubKey(top_k=v))
    if spec.aggregate.micro_average:
      # Micro averaging is performed by flattening the labels and predictions
      # and treating them as independent pairs. This is done by default by most
      # metrics whenever binarization is not used. If micro-averaging and
      # binarization are used, then we need to create an empty subkey to ensure
      # the overall aggregate key is still computed. Note that the class_weights
      # should always be passed to all metric calculations to ensure they are
      # taken into account when flattening is required.
      sub_keys.append(None)
  return sub_keys


def _metric_config(cfg: Text) -> Dict[Text, Any]:
  """Returns deserializable metric config from JSON string."""
  if not cfg:
    json_cfg = '{}'
  elif cfg[0] != '{':
    json_cfg = '{' + cfg + '}'
  else:
    json_cfg = cfg
  return json.loads(json_cfg)


def _maybe_add_name_to_config(cfg: Dict[Text, Any],
                              class_name: Text) -> Dict[Text, Any]:
  """Adds default name field to metric config if not present."""
  if 'name' not in cfg:
    # Use snake_case version of class name as default name.
    intermediate = re.sub('(.)([A-Z][a-z0-9]+)', r'\1_\2', class_name)
    cfg['name'] = re.sub('([a-z])([A-Z])', r'\1_\2', intermediate).lower()
  return cfg


def _serialize_tf_metric(
    metric: tf.keras.metrics.Metric) -> config.MetricConfig:
  """Serializes TF metric."""
  cfg = tf.keras.metrics.serialize(metric)
  return config.MetricConfig(
      class_name=cfg['class_name'],
      config=json.dumps(cfg['config'], sort_keys=True))


def _deserialize_tf_metric(
    metric_config: config.MetricConfig,
    custom_objects: Dict[Text, Type[tf.keras.metrics.Metric]]
) -> tf.keras.metrics.Metric:
  """Deserializes a tf.keras.metrics metric."""
  # The same metric type may be used for different keys when mult-class metrics
  # are used (e.g. AUC for class0, # class1, etc). TF tries to generate unique
  # metirc names even though these metrics are already unique within a
  # MetricKey. To workaroudn this issue, if a name is not set, then add a
  # default name ourselves.
  with tf.keras.utils.custom_object_scope(custom_objects):
    return tf.keras.metrics.deserialize({
        'class_name':
            metric_config.class_name,
        'config':
            _maybe_add_name_to_config(
                _metric_config(metric_config.config), metric_config.class_name)
    })


def _serialize_tfma_metric(metric: metric_types.Metric) -> config.MetricConfig:
  """Serializes TFMA metric."""
  # This implementation is identical to _serialize_tf_metric, but keeping two
  # implementations for symmetry with deserialize where separate implementations
  # are required (and to be consistent with the keras implementation).
  cfg = tf.keras.utils.serialize_keras_object(metric)
  return config.MetricConfig(
      class_name=cfg['class_name'],
      config=json.dumps(cfg['config'], sort_keys=True))


def _deserialize_tfma_metric(
    metric_config: config.MetricConfig,
    custom_objects: Dict[Text,
                         Type[metric_types.Metric]]) -> metric_types.Metric:
  """Deserializes a tfma.metrics metric."""
  with tf.keras.utils.custom_object_scope(custom_objects):
    return tf.keras.utils.deserialize_keras_object({
        'class_name': metric_config.class_name,
        'config': _metric_config(metric_config.config)
    })
