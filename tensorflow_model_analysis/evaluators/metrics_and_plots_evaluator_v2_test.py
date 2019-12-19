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
"""Test for MetricsAndPlotsEvaluator."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

import apache_beam as beam
from apache_beam.testing import util
import tensorflow as tf
from tensorflow_model_analysis import config
from tensorflow_model_analysis import constants
from tensorflow_model_analysis.api import model_eval_lib
from tensorflow_model_analysis.eval_saved_model import testutil
from tensorflow_model_analysis.eval_saved_model.example_trainers import dnn_classifier
from tensorflow_model_analysis.eval_saved_model.example_trainers import fixed_prediction_estimator_extra_fields
from tensorflow_model_analysis.eval_saved_model.example_trainers import multi_head
from tensorflow_model_analysis.evaluators import metrics_and_plots_evaluator_v2
from tensorflow_model_analysis.extractors import input_extractor
from tensorflow_model_analysis.extractors import predict_extractor
from tensorflow_model_analysis.extractors import predict_extractor_v2
from tensorflow_model_analysis.extractors import slice_key_extractor
from tensorflow_model_analysis.metrics import calibration
from tensorflow_model_analysis.metrics import calibration_plot
from tensorflow_model_analysis.metrics import metric_specs
from tensorflow_model_analysis.metrics import metric_types
from tensorflow_model_analysis.metrics import ndcg
from tensorflow_model_analysis.slicer import slicer_lib as slicer


class MetricsAndPlotsEvaluatorTest(testutil.TensorflowModelAnalysisTest):

  def _getExportDir(self):
    return os.path.join(self._getTempDir(), 'export_dir')

  def testEvaluateWithSlicing(self):
    temp_export_dir = self._getExportDir()
    _, export_dir = (
        fixed_prediction_estimator_extra_fields
        .simple_fixed_prediction_estimator_extra_fields(None, temp_export_dir))
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='fixed_float')
        ],
        slicing_specs=[
            config.SlicingSpec(),
            config.SlicingSpec(feature_keys=['fixed_string']),
        ],
        metrics_specs=metric_specs.specs_from_metrics([
            calibration.MeanLabel('mean_label'),
            calibration.MeanPrediction('mean_prediction')
        ]))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir)
    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        predict_extractor.PredictExtractor(eval_shared_model=eval_shared_model),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    # fixed_float used as example_weight key
    examples = [
        self._makeExample(
            prediction=0.2,
            label=1.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.8,
            label=0.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.5,
            label=0.0,
            fixed_int=2,
            fixed_float=2.0,
            fixed_string='fixed_string2')
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 3)
          slices = {}
          for slice_key, value in got:
            slices[slice_key] = value
          overall_slice = ()
          fixed_string1_slice = (('fixed_string', b'fixed_string1'),)
          fixed_string2_slice = (('fixed_string', b'fixed_string2'),)
          self.asssertCountEqual(
              list(slices.keys()),
              [overall_slice, fixed_string1_slice, fixed_string2_slice])
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key = metric_types.MetricKey(name='mean_label')
          pred_key = metric_types.MetricKey(name='mean_prediction')
          self.assertDictElementsAlmostEqual(
              slices[overall_slice], {
                  example_count_key: 3,
                  weighted_example_count_key: 4.0,
                  label_key: (1.0 + 0.0 + 2 * 0.0) / (1.0 + 1.0 + 2.0),
                  pred_key: (0.2 + 0.8 + 2 * 0.5) / (1.0 + 1.0 + 2.0),
              })
          self.assertDictElementsAlmostEqual(
              slices[fixed_string1_slice], {
                  example_count_key: 2,
                  weighted_example_count_key: 2.0,
                  label_key: (1.0 + 0.0) / (1.0 + 1.0),
                  pred_key: (0.2 + 0.8) / (1.0 + 1.0),
              })
          self.assertDictElementsAlmostEqual(
              slices[fixed_string2_slice], {
                  example_count_key: 1,
                  weighted_example_count_key: 2.0,
                  label_key: (2 * 0.0) / 2.0,
                  pred_key: (2 * 0.5) / 2.0,
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

        util.assert_that(
            metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithConfidenceIntervals(self):
    # NOTE: This test does not actually test that confidence intervals are
    #   accurate it only tests that the proto output by the test is well formed.
    #   This test would pass if the confidence interval implementation did
    #   nothing at all except compute the unsampled value.
    temp_export_dir = self._getExportDir()
    _, export_dir = (
        fixed_prediction_estimator_extra_fields
        .simple_fixed_prediction_estimator_extra_fields(None, temp_export_dir))
    options = config.Options()
    options.compute_confidence_intervals.value = True
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='fixed_float')
        ],
        slicing_specs=[
            config.SlicingSpec(),
            config.SlicingSpec(feature_keys=['fixed_string']),
        ],
        metrics_specs=metric_specs.specs_from_metrics([
            calibration.MeanLabel('mean_label'),
            calibration.MeanPrediction('mean_prediction')
        ]),
        options=options)
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    # fixed_float used as example_weight key
    examples = [
        self._makeExample(
            prediction=0.2,
            label=1.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.8,
            label=0.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.5,
            label=0.0,
            fixed_int=2,
            fixed_float=2.0,
            fixed_string='fixed_string2')
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 3)
          slices = {}
          for slice_key, value in got:
            slices[slice_key] = value
          overall_slice = ()
          fixed_string1_slice = (('fixed_string', b'fixed_string1'),)
          fixed_string2_slice = (('fixed_string', b'fixed_string2'),)
          self.assertCountEqual(
              list(slices.keys()),
              [overall_slice, fixed_string1_slice, fixed_string2_slice])
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key = metric_types.MetricKey(name='mean_label')
          pred_key = metric_types.MetricKey(name='mean_prediction')
          self.assertDictElementsWithTDistributionAlmostEqual(
              slices[overall_slice], {
                  example_count_key: 3,
                  weighted_example_count_key: 4.0,
                  label_key: (1.0 + 0.0 + 2 * 0.0) / (1.0 + 1.0 + 2.0),
                  pred_key: (0.2 + 0.8 + 2 * 0.5) / (1.0 + 1.0 + 2.0),
              })
          self.assertDictElementsWithTDistributionAlmostEqual(
              slices[fixed_string1_slice], {
                  example_count_key: 2,
                  weighted_example_count_key: 2.0,
                  label_key: (1.0 + 0.0) / (1.0 + 1.0),
                  pred_key: (0.2 + 0.8) / (1.0 + 1.0),
              })
          self.assertDictElementsWithTDistributionAlmostEqual(
              slices[fixed_string2_slice], {
                  example_count_key: 1,
                  weighted_example_count_key: 2.0,
                  label_key: (2 * 0.0) / 2.0,
                  pred_key: (2 * 0.5) / 2.0,
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithRegressionModel(self):
    temp_export_dir = self._getExportDir()
    _, export_dir = (
        fixed_prediction_estimator_extra_fields
        .simple_fixed_prediction_estimator_extra_fields(None, temp_export_dir))
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='fixed_float')
        ],
        slicing_specs=[config.SlicingSpec()],
        metrics_specs=metric_specs.specs_from_metrics([
            calibration.MeanLabel('mean_label'),
            calibration.MeanPrediction('mean_prediction')
        ]))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    # fixed_float used as example_weight key
    examples = [
        self._makeExample(
            prediction=0.2,
            label=1.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.8,
            label=0.0,
            fixed_int=1,
            fixed_float=1.0,
            fixed_string='fixed_string1'),
        self._makeExample(
            prediction=0.5,
            label=0.0,
            fixed_int=2,
            fixed_float=2.0,
            fixed_string='fixed_string2')
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key = metric_types.MetricKey(name='mean_label')
          pred_key = metric_types.MetricKey(name='mean_prediction')
          self.assertEqual(got_slice_key, ())
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  example_count_key: 3,
                  weighted_example_count_key: 4.0,
                  label_key: (1.0 + 0.0 + 2 * 0.0) / (1.0 + 1.0 + 2.0),
                  pred_key: (0.2 + 0.8 + 2 * 0.5) / (1.0 + 1.0 + 2.0),
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithBinaryClassificationModel(self):
    n_classes = 2
    temp_export_dir = self._getExportDir()
    _, export_dir = dnn_classifier.simple_dnn_classifier(
        None, temp_export_dir, n_classes=n_classes)

    # Add mean_label, example_count, weighted_example_count, calibration_plot
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='age')
        ],
        slicing_specs=[config.SlicingSpec()],
        metrics_specs=metric_specs.specs_from_metrics([
            calibration.MeanLabel('mean_label'),
            calibration_plot.CalibrationPlot(
                name='calibration_plot', num_buckets=10)
        ]))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    examples = [
        self._makeExample(age=1.0, language='english', label=0.0),
        self._makeExample(age=2.0, language='chinese', label=1.0),
        self._makeExample(age=3.0, language='chinese', label=0.0),
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics_and_plots = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key = metric_types.MetricKey(name='mean_label')
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  example_count_key: 3,
                  weighted_example_count_key: (1.0 + 2.0 + 3.0),
                  label_key: (0 * 1.0 + 1 * 2.0 + 0 * 3.0) / (1.0 + 2.0 + 3.0),
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      def check_plots(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_plots = got[0]
          self.assertEqual(got_slice_key, ())
          plot_key = metric_types.PlotKey('calibration_plot')
          self.assertIn(plot_key, got_plots)
          # 10 buckets + 2 for edge cases
          self.assertLen(got_plots[plot_key].buckets, 12)

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics_and_plots[constants.METRICS_KEY],
          check_metrics,
          label='metrics')
      util.assert_that(
          metrics_and_plots[constants.PLOTS_KEY], check_plots, label='plots')

  def testEvaluateWithMultiClassModel(self):
    n_classes = 3
    temp_export_dir = self._getExportDir()
    _, export_dir = dnn_classifier.simple_dnn_classifier(
        None, temp_export_dir, n_classes=n_classes)

    # Add example_count and weighted_example_count
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='age')
        ],
        slicing_specs=[config.SlicingSpec()],
        metrics_specs=metric_specs.specs_from_metrics(
            [calibration.MeanLabel('mean_label')],
            binarize=config.BinarizationOptions(class_ids=range(n_classes))))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    examples = [
        self._makeExample(age=1.0, language='english', label=0),
        self._makeExample(age=2.0, language='chinese', label=1),
        self._makeExample(age=3.0, language='english', label=2),
        self._makeExample(age=4.0, language='chinese', label=1),
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key_class_0 = metric_types.MetricKey(
              name='mean_label', sub_key=metric_types.SubKey(class_id=0))
          label_key_class_1 = metric_types.MetricKey(
              name='mean_label', sub_key=metric_types.SubKey(class_id=1))
          label_key_class_2 = metric_types.MetricKey(
              name='mean_label', sub_key=metric_types.SubKey(class_id=2))
          self.assertEqual(got_slice_key, ())
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  example_count_key:
                      4,
                  weighted_example_count_key: (1.0 + 2.0 + 3.0 + 4.0),
                  label_key_class_0: (1 * 1.0 + 0 * 2.0 + 0 * 3.0 + 0 * 4.0) /
                                     (1.0 + 2.0 + 3.0 + 4.0),
                  label_key_class_1: (0 * 1.0 + 1 * 2.0 + 0 * 3.0 + 1 * 4.0) /
                                     (1.0 + 2.0 + 3.0 + 4.0),
                  label_key_class_2: (0 * 1.0 + 0 * 2.0 + 1 * 3.0 + 0 * 4.0) /
                                     (1.0 + 2.0 + 3.0 + 4.0)
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithMultiOutputModel(self):
    temp_export_dir = self._getExportDir()
    _, export_dir = multi_head.simple_multi_head(None, temp_export_dir)

    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_keys={
                    'chinese_head': 'chinese_label',
                    'english_head': 'english_label',
                    'other_head': 'other_label'
                },
                example_weight_keys={
                    'chinese_head': 'age',
                    'english_head': 'age',
                    'other_head': 'age'
                })
        ],
        slicing_specs=[config.SlicingSpec()],
        metrics_specs=metric_specs.specs_from_metrics({
            'chinese_head': [calibration.MeanLabel('mean_label')],
            'english_head': [calibration.MeanLabel('mean_label')],
            'other_head': [calibration.MeanLabel('mean_label')],
        }))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    examples = [
        self._makeExample(
            age=1.0,
            language='english',
            english_label=1.0,
            chinese_label=0.0,
            other_label=0.0),
        self._makeExample(
            age=1.0,
            language='chinese',
            english_label=0.0,
            chinese_label=1.0,
            other_label=0.0),
        self._makeExample(
            age=2.0,
            language='english',
            english_label=1.0,
            chinese_label=0.0,
            other_label=0.0),
        self._makeExample(
            age=2.0,
            language='other',
            english_label=0.0,
            chinese_label=1.0,
            other_label=1.0),
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          example_count_key = metric_types.MetricKey(name='example_count')
          chinese_weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count', output_name='chinese_head')
          chinese_label_key = metric_types.MetricKey(
              name='mean_label', output_name='chinese_head')
          english_weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count', output_name='english_head')
          english_label_key = metric_types.MetricKey(
              name='mean_label', output_name='english_head')
          other_weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count', output_name='other_head')
          other_label_key = metric_types.MetricKey(
              name='mean_label', output_name='other_head')
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  example_count_key:
                      4,
                  chinese_label_key:
                      (0.0 + 1.0 + 2 * 0.0 + 2 * 1.0) / (1.0 + 1.0 + 2.0 + 2.0),
                  chinese_weighted_example_count_key: (1.0 + 1.0 + 2.0 + 2.0),
                  english_label_key:
                      (1.0 + 0.0 + 2 * 1.0 + 2 * 0.0) / (1.0 + 1.0 + 2.0 + 2.0),
                  english_weighted_example_count_key: (1.0 + 1.0 + 2.0 + 2.0),
                  other_label_key:
                      (0.0 + 0.0 + 2 * 0.0 + 2 * 1.0) / (1.0 + 1.0 + 2.0 + 2.0),
                  other_weighted_example_count_key: (1.0 + 1.0 + 2.0 + 2.0)
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithKerasModel(self):
    input1 = tf.keras.layers.Input(shape=(1,), name='input1')
    input2 = tf.keras.layers.Input(shape=(1,), name='input2')
    inputs = [input1, input2]
    input_layer = tf.keras.layers.concatenate(inputs)
    output_layer = tf.keras.layers.Dense(
        1, activation=tf.nn.sigmoid, name='output')(
            input_layer)
    model = tf.keras.models.Model(inputs, output_layer)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(lr=.001),
        loss=tf.keras.losses.binary_crossentropy,
        metrics=['accuracy'])

    features = {'input1': [[0.0], [1.0]], 'input2': [[1.0], [0.0]]}
    labels = [[1], [0]]
    example_weights = [1.0, 0.5]
    dataset = tf.data.Dataset.from_tensor_slices(
        (features, labels, example_weights))
    dataset = dataset.shuffle(buffer_size=1).repeat().batch(2)
    model.fit(dataset, steps_per_epoch=1)

    export_dir = self._getExportDir()
    model.save(export_dir, save_format='tf')

    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='example_weight')
        ],
        slicing_specs=[config.SlicingSpec()],
        metrics_specs=metric_specs.specs_from_metrics(
            [calibration.MeanLabel('mean_label')]))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])

    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    examples = [
        self._makeExample(
            input1=0.0,
            input2=1.0,
            label=1.0,
            example_weight=1.0,
            extra_feature='non_model_feature'),
        self._makeExample(
            input1=1.0,
            input2=0.0,
            label=0.0,
            example_weight=0.5,
            extra_feature='non_model_feature'),
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 1)
          got_slice_key, got_metrics = got[0]
          self.assertEqual(got_slice_key, ())
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          label_key = metric_types.MetricKey(name='mean_label')
          self.assertDictElementsAlmostEqual(
              got_metrics, {
                  example_count_key: 2,
                  weighted_example_count_key: (1.0 + 0.5),
                  label_key: (1.0 * 1.0 + 0.0 * 0.5) / (1.0 + 0.5),
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')

  def testEvaluateWithQueryBasedMetrics(self):
    temp_export_dir = self._getExportDir()
    _, export_dir = (
        fixed_prediction_estimator_extra_fields
        .simple_fixed_prediction_estimator_extra_fields(None, temp_export_dir))
    eval_config = config.EvalConfig(
        model_specs=[
            config.ModelSpec(
                location=export_dir,
                label_key='label',
                example_weight_key='fixed_int')
        ],
        slicing_specs=[
            config.SlicingSpec(),
            config.SlicingSpec(feature_keys=['fixed_string']),
        ],
        metrics_specs=metric_specs.specs_from_metrics(
            [ndcg.NDCG(gain_key='fixed_float', name='ndcg')],
            binarize=config.BinarizationOptions(top_k_list=[1, 2]),
            query_key='fixed_string'))
    eval_shared_model = self.createTestEvalSharedModel(
        eval_saved_model_path=export_dir, tags=[tf.saved_model.SERVING])
    slice_spec = [
        slicer.SingleSliceSpec(spec=s) for s in eval_config.slicing_specs
    ]
    extractors = [
        input_extractor.InputExtractor(eval_config=eval_config),
        predict_extractor_v2.PredictExtractor(
            eval_config=eval_config, eval_shared_models=[eval_shared_model]),
        slice_key_extractor.SliceKeyExtractor(slice_spec=slice_spec)
    ]
    evaluators = [
        metrics_and_plots_evaluator_v2.MetricsAndPlotsEvaluator(
            eval_config=eval_config, eval_shared_models=[eval_shared_model])
    ]

    # fixed_string used as query_key
    # fixed_float used as gain_key for NDCG
    # fixed_int used as example_weight_key for NDCG
    examples = [
        self._makeExample(
            prediction=0.2,
            label=1.0,
            fixed_float=1.0,
            fixed_string='query1',
            fixed_int=1),
        self._makeExample(
            prediction=0.8,
            label=0.0,
            fixed_float=0.5,
            fixed_string='query1',
            fixed_int=1),
        self._makeExample(
            prediction=0.5,
            label=0.0,
            fixed_float=0.5,
            fixed_string='query2',
            fixed_int=2),
        self._makeExample(
            prediction=0.9,
            label=1.0,
            fixed_float=1.0,
            fixed_string='query2',
            fixed_int=2),
        self._makeExample(
            prediction=0.1,
            label=0.0,
            fixed_float=0.1,
            fixed_string='query2',
            fixed_int=2),
        self._makeExample(
            prediction=0.9,
            label=1.0,
            fixed_float=1.0,
            fixed_string='query3',
            fixed_int=3)
    ]

    with beam.Pipeline() as pipeline:
      # pylint: disable=no-value-for-parameter
      metrics = (
          pipeline
          | 'Create' >> beam.Create([e.SerializeToString() for e in examples])
          | 'InputsToExtracts' >> model_eval_lib.InputsToExtracts()
          | 'ExtractAndEvaluate' >> model_eval_lib.ExtractAndEvaluate(
              extractors=extractors, evaluators=evaluators))

      # pylint: enable=no-value-for-parameter

      def check_metrics(got):
        try:
          self.assertLen(got, 4)
          slices = {}
          for slice_key, value in got:
            slices[slice_key] = value
          overall_slice = ()
          query1_slice = (('fixed_string', b'query1'),)
          query2_slice = (('fixed_string', b'query2'),)
          query3_slice = (('fixed_string', b'query3'),)
          self.assertCountEqual(
              list(slices.keys()),
              [overall_slice, query1_slice, query2_slice, query3_slice])
          example_count_key = metric_types.MetricKey(name='example_count')
          weighted_example_count_key = metric_types.MetricKey(
              name='weighted_example_count')
          ndcg1_key = metric_types.MetricKey(
              name='ndcg', sub_key=metric_types.SubKey(top_k=1))
          ndcg2_key = metric_types.MetricKey(
              name='ndcg', sub_key=metric_types.SubKey(top_k=2))
          # Query1 (weight=1): (p=0.8, g=0.5) (p=0.2, g=1.0)
          # Query2 (weight=2): (p=0.9, g=1.0) (p=0.5, g=0.5) (p=0.1, g=0.1)
          # Query3 (weight=3): (p=0.9, g=1.0)
          #
          # DCG@1:  0.5, 1.0, 1.0
          # NDCG@1: 0.5, 1.0, 1.0
          # Average NDCG@1: (1 * 0.5 + 2 * 1.0 + 3 * 1.0) / (1 + 2 + 3) ~ 0.92
          #
          # DCG@2: (0.5 + 1.0/log(3) ~ 0.630930
          #        (1.0 + 0.5/log(3) ~ 1.315465
          #        1.0
          # NDCG@2: (0.5 + 1.0/log(3)) / (1.0 + 0.5/log(3)) ~ 0.85972
          #         (1.0 + 0.5/log(3)) / (1.0 + 0.5/log(3)) = 1.0
          #         1.0
          # Average NDCG@2: (1 * 0.860 + 2 * 1.0 + 3 * 1.0) / (1 + 2 + 3) ~ 0.97
          self.assertDictElementsAlmostEqual(
              slices[overall_slice], {
                  example_count_key: 6,
                  weighted_example_count_key: 11.0,
                  ndcg1_key: 0.9166667,
                  ndcg2_key: 0.9766198
              })
          self.assertDictElementsAlmostEqual(
              slices[query1_slice], {
                  example_count_key: 2,
                  weighted_example_count_key: 2.0,
                  ndcg1_key: 0.5,
                  ndcg2_key: 0.85972
              })
          self.assertDictElementsAlmostEqual(
              slices[query2_slice], {
                  example_count_key: 3,
                  weighted_example_count_key: 6.0,
                  ndcg1_key: 1.0,
                  ndcg2_key: 1.0
              })
          self.assertDictElementsAlmostEqual(
              slices[query3_slice], {
                  example_count_key: 1,
                  weighted_example_count_key: 3.0,
                  ndcg1_key: 1.0,
                  ndcg2_key: 1.0
              })

        except AssertionError as err:
          raise util.BeamAssertException(err)

      util.assert_that(
          metrics[constants.METRICS_KEY], check_metrics, label='metrics')


if __name__ == '__main__':
  tf.compat.v1.enable_v2_behavior()
  tf.test.main()
