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
"""Init module for TensorFlow Model Analysis metrics."""

from tensorflow_model_analysis.metrics.calibration import Calibration
from tensorflow_model_analysis.metrics.calibration import MeanLabel
from tensorflow_model_analysis.metrics.calibration import MeanPrediction
from tensorflow_model_analysis.metrics.calibration_plot import CalibrationPlot
from tensorflow_model_analysis.metrics.confusion_matrix_metrics import ConfusionMatrixAtThresholds
from tensorflow_model_analysis.metrics.confusion_matrix_metrics import FallOut
from tensorflow_model_analysis.metrics.confusion_matrix_metrics import MissRate
from tensorflow_model_analysis.metrics.confusion_matrix_metrics import Specificity
from tensorflow_model_analysis.metrics.confusion_matrix_plot import ConfusionMatrixPlot
from tensorflow_model_analysis.metrics.example_count import ExampleCount
from tensorflow_model_analysis.metrics.metric_specs import default_binary_classification_specs
from tensorflow_model_analysis.metrics.metric_specs import default_multi_class_classification_specs
from tensorflow_model_analysis.metrics.metric_specs import default_regression_specs
from tensorflow_model_analysis.metrics.metric_specs import specs_from_metrics
from tensorflow_model_analysis.metrics.metric_types import DerivedMetricComputation
from tensorflow_model_analysis.metrics.metric_types import FeaturePreprocessor
from tensorflow_model_analysis.metrics.metric_types import Metric
from tensorflow_model_analysis.metrics.metric_types import MetricComputation
from tensorflow_model_analysis.metrics.metric_types import MetricComputations
from tensorflow_model_analysis.metrics.metric_types import MetricKey
from tensorflow_model_analysis.metrics.metric_types import PlotKey
from tensorflow_model_analysis.metrics.metric_types import StandardMetricInputs
from tensorflow_model_analysis.metrics.metric_types import SubKey
from tensorflow_model_analysis.metrics.metric_util import merge_per_key_computations
from tensorflow_model_analysis.metrics.metric_util import to_label_prediction_example_weight
from tensorflow_model_analysis.metrics.metric_util import to_standard_metric_inputs
from tensorflow_model_analysis.metrics.min_label_position import MinLabelPosition
from tensorflow_model_analysis.metrics.multi_class_confusion_matrix_plot import MultiClassConfusionMatrixPlot
from tensorflow_model_analysis.metrics.multi_label_confusion_matrix_plot import MultiLabelConfusionMatrixPlot
from tensorflow_model_analysis.metrics.ndcg import NDCG
from tensorflow_model_analysis.metrics.query_statistics import QueryStatistics
from tensorflow_model_analysis.metrics.squared_pearson_correlation import SquaredPearsonCorrelation
from tensorflow_model_analysis.metrics.tjur_discrimination import CoefficientOfDiscrimination
from tensorflow_model_analysis.metrics.tjur_discrimination import RelativeCoefficientOfDiscrimination
from tensorflow_model_analysis.metrics.weighted_example_count import WeightedExampleCount
