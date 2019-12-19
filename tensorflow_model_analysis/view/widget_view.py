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
"""View API for Tensorflow Model Analysis."""
from __future__ import absolute_import
from __future__ import division
# Standard __future__ imports
from __future__ import print_function

from tensorflow_model_analysis import constants
from tensorflow_model_analysis.api import model_eval_lib
import tensorflow_model_analysis.notebook.visualization as visualization
from tensorflow_model_analysis.slicer import slicer_lib as slicer
from tensorflow_model_analysis.view import util
from typing import Callable, Dict, Optional, Text, Union


def render_slicing_metrics(
    result: model_eval_lib.EvalResult,
    slicing_column: Optional[Text] = None,
    slicing_spec: Optional[slicer.SingleSliceSpec] = None,
    weighted_example_column: Text = None,
    event_handlers: Optional[Callable[[Dict[Text, Union[Text, float]]],
                                      None]] = None,
) -> Optional[visualization.SlicingMetricsViewer]:  # pytype: disable=invalid-annotation
  """Renders the slicing metrics view as widget.

  Args:
    result: An tfma.EvalResult.
    slicing_column: The column to slice on.
    slicing_spec: The slicing spec to filter results. If neither column nor spec
      is set, show overall.
    weighted_example_column: Override for the weighted example column. This can
      be used when different weights are applied in different aprts of the model
      (eg: multi-head).
    event_handlers: The event handlers

  Returns:
    A SlicingMetricsViewer object if in Jupyter notebook; None if in Colab.
  """
  data = util.get_slicing_metrics(result.slicing_metrics, slicing_column,
                                  slicing_spec)
  config = util.get_slicing_config(result.config, weighted_example_column)

  return visualization.render_slicing_metrics(
      data, config, event_handlers=event_handlers)


def render_time_series(
    results: model_eval_lib.EvalResults,
    slice_spec: Optional[slicer.SingleSliceSpec] = None,
    display_full_path: bool = False
) -> Optional[visualization.TimeSeriesViewer]:  # pytype: disable=invalid-annotation
  """Renders the time series view as widget.

  Args:
    results: An tfma.EvalResults.
    slice_spec: A slicing spec determining the slice to show time series on.
      Show overall if not set.
    display_full_path: Whether to display the full path to model / data in the
      visualization or just show file name.

  Returns:
    A TimeSeriesViewer object if in Jupyter notebook; None if in Colab.
  """
  slice_spec_to_use = slice_spec if slice_spec else slicer.SingleSliceSpec()
  data = util.get_time_series(results, slice_spec_to_use, display_full_path)
  config = {
      'isModelCentric': results.get_mode() == constants.MODEL_CENTRIC_MODE
  }

  return visualization.render_time_series(data, config)


def render_plot(
    result: model_eval_lib.EvalResult,
    slicing_spec: Optional[slicer.SingleSliceSpec] = None,
    output_name: Optional[Text] = None,
    class_id: Optional[int] = None,
    top_k: Optional[int] = None,
    k: Optional[int] = None,
    label: Optional[Text] = None,
) -> Optional[visualization.PlotViewer]:  # pytype: disable=invalid-annotation
  """Renders the plot view as widget.

  Args:
    result: An tfma.EvalResult.
    slicing_spec: The slicing spec to identify the slice. Show overall if unset.
    output_name: A string representing the output name.
    class_id: A number representing the class id if multi class.
    top_k: The k used to compute prediction in the top k position.
    k: The k used to compute prediciton at the kth position.
    label: A partial label used to match a set of plots in the results.

  Returns:
    A PlotViewer object if in Jupyter notebook; None if in Colab.
  """
  slice_spec_to_use = slicing_spec if slicing_spec else slicer.SingleSliceSpec()
  data, config = util.get_plot_data_and_config(result.plots, slice_spec_to_use,
                                               output_name, class_id, top_k, k,
                                               label)
  return visualization.render_plot(data, config)
