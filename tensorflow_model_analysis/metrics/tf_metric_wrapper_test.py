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
"""Tests for TF metric wrapper."""

from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

import os.path

from absl.testing import parameterized
import apache_beam as beam
from apache_beam.testing import util
import numpy as np
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import model_util
from tensorflow_model_analysis import types
from tensorflow_model_analysis.eval_saved_model import testutil
from tensorflow_model_analysis.metrics import metric_types
from tensorflow_model_analysis.metrics import metric_util
from tensorflow_model_analysis.metrics import tf_metric_wrapper


class _CustomMetric(tf.keras.metrics.Mean):

  def __init__(self, name='custom', dtype=None):
    super(_CustomMetric, self).__init__(name=name, dtype=dtype)

  def update_state(self, y_true, y_pred, sample_weight):
    return super(_CustomMetric, self).update_state(
        y_pred, sample_weight=sample_weight)


class ConfusionMatrixMetricsTest(testutil.TensorflowModelAnalysisTest,
                                 parameterized.TestCase):

  #  This is needed because of pickling errors when using
  #  parameterized.named_parameters with TF metric types.
  def _tf_metric_by_name(self, metric_name):
    """Returns instance of tf.keras.metric with default args given name."""
    if metric_name == 'auc':
      return tf.keras.metrics.AUC(name='auc')
    elif metric_name == 'auc_pr':
      return tf.keras.metrics.AUC(name='auc_pr', curve='PR')
    elif metric_name == 'precision':
      return tf.keras.metrics.Precision(name='precision')
    elif metric_name == 'precision@2':
      return tf.keras.metrics.Precision(name='precision@2', top_k=2)
    elif metric_name == 'precision@3':
      return tf.keras.metrics.Precision(name='precision@3', top_k=3)
    elif metric_name == 'recall':
      return tf.keras.metrics.Recall(name='recall')
    elif metric_name == 'recall@2':
      return tf.keras.metrics.Recall(name='recall@2', top_k=2)
    elif metric_name == 'recall@3':
      return tf.keras.metrics.Recall(name='recall@3', top_k=3)
    elif metric_name == 'true_positives':
      return tf.keras.metrics.TruePositives(name='true_positives')
    elif metric_name == 'false_positives':
      return tf.keras.metrics.FalsePositives(name='false_positives')
    elif metric_name == 'true_negatives':
      return tf.keras.metrics.TrueNegatives(name='true_negatives')
    elif metric_name == 'false_negatives':
      return tf.keras.metrics.FalseNegatives(name='false_negatives')
    elif metric_name == 'specificity_at_sensitivity':
      return tf.keras.metrics.SpecificityAtSensitivity(
          0.5, name='specificity_at_sensitivity')
    elif metric_name == 'sensitivity_at_specificity':
      return tf.keras.metrics.SensitivityAtSpecificity(
          0.5, name='sensitivity_at_specificity')

  @parameterized.named_parameters(
      ('auc', 'auc', 0.75),
      ('auc_pr', 'auc_pr', 0.79727),
      ('precision', 'precision', 1.0),
      ('recall', 'recall', 0.5),
      ('true_positives', 'true_positives', 1.0),
      ('false_positives', 'false_positives', 0.0),
      ('true_negatives', 'true_negatives', 2.0),
      ('false_negatives', 'false_negatives', 1.0),
      ('specificity_at_sensitivity', 'specificity_at_sensitivity', 0.5),
      ('sensitivity_at_specificity', 'sensitivity_at_specificity', 1.0),
  )
  def testMetricsWithoutWeights(self, metric_name, expected_value):
    computations = tf_metric_wrapper.tf_metric_computations(
        [self._tf_metric_by_name(metric_name)], config.EvalConfig())
    histogram = computations[0]
    matrix = computations[1]
    metric = computations[2]

    example1 = {
        'labels': np.array([0.0]),
        'predictions': np.array([0.0]),
        'example_weights': np.array([1.0]),
    }
    example2 = {
        'labels': np.array([0.0]),
        'predictions': np.array([0.5]),
        'example_weights': np.array([1.0]),
    }
    example3 = {
        'labels': np.array([1.0]),
        'predictions': np.array([0.3]),
        'example_weights': np.array([1.0]),
    }
    example4 = {
        'labels': np.array([1.0]),
        'predictions': np.array([0.9]),
        'example_weights': np.array([1.0]),
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3, example4])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'ComputeHistogram' >> beam.CombinePerKey(histogram.combiner)
          | 'ComputeConfusionMatrix' >> beam.Map(
              lambda x: (x[0], matrix.result(x[1])))  # pyformat: disable
          | 'ComputeMetric' >> beam.Map(
              lambda x: (x[0], metric.result(x[1]))))  # pyformat: disable

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          key = metric_types.MetricKey(name=metric_name)
          self.assertDictElementsAlmostEqual(
              got_metrics, {key: expected_value}, places=5)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  @parameterized.named_parameters(
      ('auc', 'auc', 0.64286),
      ('auc_pr', 'auc_pr', 0.37467),
      ('precision', 'precision', 0.5833333),
      ('recall', 'recall', 1.0),
      ('true_positives', 'true_positives', 0.7),
      ('false_positives', 'false_positives', 0.5),
      ('true_negatives', 'true_negatives', 0.9),
      ('false_negatives', 'false_negatives', 0.0),
      ('specificity_at_sensitivity', 'specificity_at_sensitivity', 0.0),
      ('sensitivity_at_specificity', 'sensitivity_at_specificity', 1.0),
  )
  def testMetricsWithWeights(self, metric_name, expected_value):
    computations = tf_metric_wrapper.tf_metric_computations(
        [self._tf_metric_by_name(metric_name)], config.EvalConfig())
    histogram = computations[0]
    matrix = computations[1]
    metric = computations[2]

    example1 = {
        'labels': np.array([0.0]),
        'predictions': np.array([1.0]),
        'example_weights': np.array([0.5]),
    }
    example2 = {
        'labels': np.array([1.0]),
        'predictions': np.array([0.7]),
        'example_weights': np.array([0.7]),
    }
    example3 = {
        'labels': np.array([0.0]),
        'predictions': np.array([0.5]),
        'example_weights': np.array([0.9]),
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'ComputeHistogram' >> beam.CombinePerKey(histogram.combiner)
          | 'ComputeConfusionMatrix' >> beam.Map(
              lambda x: (x[0], matrix.result(x[1])))  # pyformat: disable
          | 'ComputeMetric' >> beam.Map(
              lambda x: (x[0], metric.result(x[1]))))  # pyformat: disable

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          key = metric_types.MetricKey(name=metric_name)
          self.assertDictElementsAlmostEqual(
              got_metrics, {key: expected_value}, places=5)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  @parameterized.named_parameters(
      ('precision@2', 'precision@2', 0.33333),
      ('recall@2', 'recall@2', 0.5),
      ('precision@3', 'precision@3', 0.22222),
      ('recall@3', 'recall@3', 0.5),
  )
  def testMultiClassMetrics(self, metric_name, expected_value):
    computations = tf_metric_wrapper.tf_metric_computations(
        [self._tf_metric_by_name(metric_name)], config.EvalConfig())
    histogram = computations[0]
    matrix = computations[1]
    metric = computations[2]

    example1 = {
        'labels': np.array([2]),
        'predictions': np.array([0.1, 0.2, 0.1, 0.25, 0.35]),
        'example_weights': np.array([0.5]),
    }
    example2 = {
        'labels': np.array([1]),
        'predictions': np.array([0.2, 0.3, 0.05, 0.15, 0.3]),
        'example_weights': np.array([0.7]),
    }
    example3 = {
        'labels': np.array([3]),
        'predictions': np.array([0.01, 0.2, 0.09, 0.5, 0.2]),
        'example_weights': np.array([0.9]),
    }
    example4 = {
        'labels': np.array([4]),
        'predictions': np.array([0.3, 0.2, 0.05, 0.4, 0.05]),
        'example_weights': np.array([0.3]),
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3, example4])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'ComputeHistogram' >> beam.CombinePerKey(histogram.combiner)
          | 'ComputeConfusionMatrix' >> beam.Map(
              lambda x: (x[0], matrix.result(x[1])))  # pyformat: disable
          | 'ComputeMetric' >> beam.Map(
              lambda x: (x[0], metric.result(x[1]))))  # pyformat: disable

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          top_k = int(metric_name.split('@')[1])
          key = metric_types.MetricKey(
              name=metric_name, sub_key=metric_types.SubKey(top_k=top_k))
          self.assertDictElementsAlmostEqual(
              got_metrics, {key: expected_value}, places=5)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')


class NonConfusionMatrixMetricsTest(testutil.TensorflowModelAnalysisTest):

  def testSimpleMetric(self):
    computation = tf_metric_wrapper.tf_metric_computations(
        [tf.keras.metrics.MeanSquaredError(name='mse')], config.EvalConfig())[0]

    example = {
        'labels': [0, 0, 1, 1],
        'predictions': [0, 0.5, 0.3, 0.9],
        'example_weights': [1.0]
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          mse_key = metric_types.MetricKey(name='mse')
          self.assertDictElementsAlmostEqual(got_metrics, {mse_key: 0.1875})

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testSparseMetric(self):
    computation = tf_metric_wrapper.tf_metric_computations([
        tf.keras.metrics.SparseCategoricalCrossentropy(
            name='sparse_categorical_crossentropy')
    ], config.EvalConfig())[0]

    # Simulate a multi-class problem with 3 labels.
    example = {
        'labels': [1],
        'predictions': [0.3, 0.6, 0.1],
        'example_weights': [1.0]
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          key = metric_types.MetricKey(name='sparse_categorical_crossentropy')
          # 0*log(.3) -1*log(0.6)-0*log(.1) = 0.51
          self.assertDictElementsAlmostEqual(got_metrics, {key: 0.51083})

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testRaisesErrorForInvalidNonSparseSettings(self):
    with self.assertRaises(ValueError):
      tf_metric_wrapper.tf_metric_computations([
          tf.keras.metrics.SparseCategoricalCrossentropy(
              name='sparse_categorical_crossentropy')
      ],
                                               config.EvalConfig(),
                                               class_weights={})

  def testMetricWithClassWeights(self):
    computation = tf_metric_wrapper.tf_metric_computations(
        [tf.keras.metrics.MeanSquaredError(name='mse')],
        config.EvalConfig(),
        class_weights={
            0: 0.1,
            1: 0.2,
            2: 0.3,
            3: 0.4
        })[0]

    # Simulate a multi-class problem with 4 labels. The use of class weights
    # implies micro averaging which only makes sense for multi-class metrics.
    example = {
        'labels': [0, 0, 1, 0],
        'predictions': [0, 0.5, 0.3, 0.9],
        'example_weights': [1.0]
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          mse_key = metric_types.MetricKey(name='mse')
          # numerator = (0.1*0**2 + 0.2*0.5**2 + 0.3*0.7**2 + 0.4*0.9**2)
          # denominator = (.1 + .2 + 0.3 + 0.4)
          # numerator / denominator = 0.521
          self.assertDictElementsAlmostEqual(got_metrics, {mse_key: 0.521})

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testCustomTFMetric(self):
    metric = tf_metric_wrapper.tf_metric_computations([_CustomMetric()],
                                                      config.EvalConfig())[0]

    example1 = {'labels': [0.0], 'predictions': [0.2], 'example_weights': [1.0]}
    example2 = {'labels': [0.0], 'predictions': [0.8], 'example_weights': [1.0]}
    example3 = {'labels': [0.0], 'predictions': [0.5], 'example_weights': [2.0]}

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(metric.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())

          custom_key = metric_types.MetricKey(name='custom')
          self.assertDictElementsAlmostEqual(
              got_metrics,
              {custom_key: (0.2 + 0.8 + 2 * 0.5) / (1.0 + 1.0 + 2.0)})

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testMultiOutputTFMetric(self):
    computation = tf_metric_wrapper.tf_metric_computations(
        {
            'output_name': [tf.keras.metrics.MeanSquaredError(name='mse')],
        }, config.EvalConfig())[0]

    extracts = {
        'labels': {
            'output_name': [0, 0, 1, 1],
        },
        'predictions': {
            'output_name': [0, 0.5, 0.3, 0.9],
        },
        'example_weights': {
            'output_name': [1.0]
        }
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([extracts])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          mse_key = metric_types.MetricKey(
              name='mse', output_name='output_name')
          self.assertDictElementsAlmostEqual(got_metrics, {
              mse_key: 0.1875,
          })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testTFMetricWithClassID(self):
    computation = tf_metric_wrapper.tf_metric_computations(
        [tf.keras.metrics.MeanSquaredError(name='mse')],
        config.EvalConfig(),
        sub_key=metric_types.SubKey(class_id=1))[0]

    example1 = {
        'labels': [2],
        'predictions': [0.5, 0.0, 0.5],
        'example_weights': [1.0]
    }
    example2 = {
        'labels': [0],
        'predictions': [0.2, 0.5, 0.3],
        'example_weights': [1.0]
    }
    example3 = {
        'labels': [1],
        'predictions': [0.2, 0.3, 0.5],
        'example_weights': [1.0]
    }
    example4 = {
        'labels': [1],
        'predictions': [0.0, 0.9, 0.1],
        'example_weights': [1.0]
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3, example4])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          mse_key = metric_types.MetricKey(
              name='mse', sub_key=metric_types.SubKey(class_id=1))
          self.assertDictElementsAlmostEqual(got_metrics, {
              mse_key: 0.1875,
          })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testBatching(self):
    options = config.Options()
    options.desired_batch_size.value = 2
    computation = tf_metric_wrapper.tf_metric_computations(
        [_CustomMetric(),
         tf.keras.metrics.MeanSquaredError(name='mse')],
        config.EvalConfig(options=options))[0]

    example1 = {'labels': [0.0], 'predictions': [0.0], 'example_weights': [1.0]}
    example2 = {'labels': [0.0], 'predictions': [0.5], 'example_weights': [1.0]}
    example3 = {'labels': [1.0], 'predictions': [0.3], 'example_weights': [1.0]}
    example4 = {'labels': [1.0], 'predictions': [0.9], 'example_weights': [1.0]}
    example5 = {'labels': [1.0], 'predictions': [0.5], 'example_weights': [0.0]}

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      result = (
          pipeline
          | 'Create' >> beam.Create(
              [example1, example2, example3, example4, example5])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x))
          | 'Combine' >> beam.CombinePerKey(computation.combiner))

      # pylint: enable=no-value-for-parameter

      def check_result(got):
        try:
          self.assertEqual(1, len(got), 'got: %s' % got)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())

          custom_key = metric_types.MetricKey(name='custom')
          mse_key = metric_types.MetricKey(name='mse')
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  custom_key: (0.0 + 0.5 + 0.3 + 0.9 + 0.0) /
                              (1.0 + 1.0 + 1.0 + 1.0 + 0.0),
                  mse_key:
                      0.1875,
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(result, check_result, label='result')

  def testMergeAccumulators(self):
    options = config.Options()
    options.desired_batch_size.value = 2
    computation = tf_metric_wrapper.tf_metric_computations(
        [tf.keras.metrics.MeanSquaredError(name='mse')],
        config.EvalConfig(options=options))[0]

    example1 = {'labels': [0.0], 'predictions': [0.0], 'example_weights': [1.0]}
    example2 = {'labels': [0.0], 'predictions': [0.5], 'example_weights': [1.0]}
    example3 = {'labels': [1.0], 'predictions': [0.3], 'example_weights': [1.0]}
    example4 = {'labels': [1.0], 'predictions': [0.9], 'example_weights': [1.0]}
    example5 = {'labels': [1.0], 'predictions': [0.5], 'example_weights': [0.0]}

    combiner_inputs = []
    for e in (example1, example2, example3, example4, example5):
      combiner_inputs.append(metric_util.to_standard_metric_inputs(e))
    acc1 = computation.combiner.create_accumulator()
    acc1 = computation.combiner.add_input(acc1, combiner_inputs[0])
    acc1 = computation.combiner.add_input(acc1, combiner_inputs[1])
    acc1 = computation.combiner.add_input(acc1, combiner_inputs[2])
    acc2 = computation.combiner.create_accumulator()
    acc2 = computation.combiner.add_input(acc2, combiner_inputs[3])
    acc2 = computation.combiner.add_input(acc2, combiner_inputs[4])
    acc = computation.combiner.merge_accumulators([acc1, acc2])

    got_metrics = computation.combiner.extract_output(acc)
    mse_key = metric_types.MetricKey(name='mse')
    self.assertDictElementsAlmostEqual(got_metrics, {mse_key: 0.1875})


class MixedMetricsTest(testutil.TensorflowModelAnalysisTest):

  def testWithDefaultMetricsProvidedByModel(self):
    export_dir = os.path.join(self._getTempDir(), 'export_dir')
    dummy_layer = tf.keras.layers.Input(shape=(1,))
    model = tf.keras.models.Model([dummy_layer], [dummy_layer])
    model.compile(
        loss=tf.keras.losses.BinaryCrossentropy(),
        metrics=[tf.keras.metrics.MeanSquaredError(name='mse')])
    model.save(export_dir, save_format='tf')
    model_loader = types.ModelLoader(
        tags=[tf.saved_model.SERVING],
        construct_fn=model_util.model_construct_fn(
            eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING]))

    computations = tf_metric_wrapper.tf_metric_computations(
        [tf.keras.metrics.AUC(name='auc')],
        config.EvalConfig(),
        model_loader=model_loader)

    confusion_histogram = computations[0]
    confusion_matrix = computations[1].result
    confusion_metrics = computations[2].result
    non_confusion_metrics = computations[3]

    example1 = {
        'labels': np.array([0.0]),
        'predictions': np.array([0.0]),
        'example_weights': np.array([1.0]),
    }
    example2 = {
        'labels': np.array([0.0]),
        'predictions': np.array([0.5]),
        'example_weights': np.array([1.0]),
    }
    example3 = {
        'labels': np.array([1.0]),
        'predictions': np.array([0.3]),
        'example_weights': np.array([1.0]),
    }
    example4 = {
        'labels': np.array([1.0]),
        'predictions': np.array([0.9]),
        'example_weights': np.array([1.0]),
    }

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      sliced_examples = (
          pipeline
          | 'Create' >> beam.Create([example1, example2, example3, example4])
          | 'Process' >> beam.Map(metric_util.to_standard_metric_inputs)
          | 'AddSlice' >> beam.Map(lambda x: ((), x)))

      confusion_result = (
          sliced_examples
          |
          'ComputeHistogram' >> beam.CombinePerKey(confusion_histogram.combiner)
          | 'ComputeConfusionMatrix' >> beam.Map(
              lambda x: (x[0], confusion_matrix(x[1])))  # pyformat: disable
          | 'ComputeMetric' >> beam.Map(
              lambda x: (x[0], confusion_metrics(x[1]))))  # pyformat: disable

      non_confusion_result = (
          sliced_examples
          | 'Combine' >> beam.CombinePerKey(non_confusion_metrics.combiner))

      # pylint: enable=no-value-for-parameter

      def check_confusion_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          auc_key = metric_types.MetricKey(name='auc')
          self.assertDictElementsAlmostEqual(
              got_metrics, {auc_key: 0.75}, places=5)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      def check_non_confusion_result(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          mse_key = metric_types.MetricKey(name='mse')
          binary_crossentropy_key = metric_types.MetricKey(
              name='binary_crossentropy')
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  mse_key: 0.1875,
                  binary_crossentropy_key: 0.0
              },
              places=5)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          confusion_result, check_confusion_result, label='confusion')
      util.assert_that(
          non_confusion_result,
          check_non_confusion_result,
          label='non_confusion')


if __name__ == '__main__':
  tf.compat.v1.enable_v2_behavior()
  tf.test.main()
