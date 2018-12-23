# ----------------------------------------------------------------------------
# Copyright (c) 2016-2018, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import json
import os
import pkg_resources
from distutils.dir_util import copy_tree
import subprocess
import tempfile

import qiime2
import q2templates
import pandas as pd
from skbio.stats.composition import ancom as skbio_ancom
from skbio.stats.composition import clr

from numpy import log, sqrt
from scipy.stats import f_oneway

_difference_functions = {'mean_difference': lambda x, y: x.mean() - y.mean(),
                         'f_statistic': f_oneway}

_transform_functions = {'sqrt': sqrt,
                        'log': log,
                        'clr': clr}

TEMPLATES = pkg_resources.resource_filename('q2_composition', 'assets')


def difference_functions():
    return list(_difference_functions.keys())


def transform_functions():
    return list(_transform_functions.keys())


def run_commands(cmds, verbose=True):
    if verbose:
        print("Running external command line application(s). This may print "
              "messages to stdout and/or stderr.")
        print("The command(s) being run are below. These commands cannot "
              "be manually re-run as they will depend on temporary files that "
              "no longer exist.")
    for cmd in cmds:
        if verbose:
            print("\nCommand:", end=' ')
            print(" ".join(cmd), end='\n\n')
        subprocess.run(cmd, check=True)


def ancom(output_dir: str,
          table: pd.DataFrame,
          metadata: qiime2.Metadata,
          main_variable: str,
          adjusted_formula: str='None',
          state: str='None',
          random_formula: str=None,
          multiple_test_correction: str='conservative',
          alpha: float=0.05,
          max_sparsity: float=0.9) -> None:
    # TODO: validate metadata:
    # main_variable, adjusted_formula, state, random_formula

    with tempfile.TemporaryDirectory() as temp_dir_name:
        zeroes_dist_fp = os.path.join(temp_dir_name, 'zeroes_dist.tsv')
        w_frame_fp = os.path.join(temp_dir_name, 'w_frame.tsv')
        feature_table_fp = os.path.join(temp_dir_name, 'table.tsv')
        table.to_csv(feature_table_fp, sep='\t')

        if adjusted_formula is not None:
            adjusted = 'True'
        else:
            adjusted = 'False'

        if state is not None:
            longitudinal = 'True'
        else:
            longitudinal = 'False'

        if multiple_test_correction == 'max':
            multiple_test_correction = '1'
        elif multiple_test_correction == 'mod':
            multiple_test_correction = '2'
        else: # otherwise uncorrected
            multiple_test_correction = '3'

        cmd = ['ancom.r',
               feature_table_fp,
               Vardat,
               adjusted,
               longitudinal,
               main_variable,
               adjusted_formula,
               state,
               longitudinal,
               random_formula,
               multiple_test_correction,
               alpha,
               max_sparsity,
               zeroes_dist_fp,
               w_frame_fp]
        run_commands([cmd])

        zeroes_dist = pd.read_csv(zeroes_dist_fp, sep='\t')
        w_frame = pd.read_csv(w_frame_fp, sep='\t')


def old_ancom(output_dir: str,
          table: pd.DataFrame,
          metadata: qiime2.CategoricalMetadataColumn,
          transform_function: str = 'clr',
          difference_function: str = None) -> None:
    metadata = metadata.filter_ids(table.index)
    if metadata.has_missing_values():
        missing_data_sids = metadata.get_ids(where_values_missing=True)
        missing_data_sids = ', '.join(sorted(missing_data_sids))
        raise ValueError('Metadata column is missing values for the '
                         'following samples. Values need to be added for '
                         'these samples, or the samples need to be removed '
                         'from the table: %s' % missing_data_sids)
    ancom_results = skbio_ancom(table,
                                metadata.to_series(),
                                significance_test=f_oneway)
    # scikit-bio 0.4.2 returns a single tuple from ancom, and scikit-bio 0.5.0
    # returns two tuples. We want to support both scikit-bio versions, so we
    # tuplize ancom_result to support both. Similarly, the "reject" column
    # was renamed in scikit-bio 0.5.0, so we apply a rename here (which does
    # nothing if a column called "reject" isn't found).
    ancom_results = qiime2.core.util.tuplize(ancom_results)
    ancom_results[0].sort_values(by='W', ascending=False)
    ancom_results[0].rename(columns={'reject': 'Reject null hypothesis'},
                            inplace=True)
    significant_features = ancom_results[0][
        ancom_results[0]['Reject null hypothesis']]

    context = dict()
    if not significant_features.empty:
        context['significant_features'] = q2templates.df_to_html(
            significant_features['W'].to_frame())
        context['percent_abundances'] = q2templates.df_to_html(
            ancom_results[1].loc[significant_features.index])

    metadata = metadata.to_series()
    cats = list(set(metadata))
    transform_function_name = transform_function
    transform_function = _transform_functions[transform_function]
    # broadcast is deprecated in pandas>=0.23 and should be replaced for
    # result_type='broadcast'
    transformed_table = table.apply(
        transform_function, axis=1, broadcast=True)

    if difference_function is None:
        if len(cats) == 2:
            difference_function = 'mean_difference'
        else:  # len(categories) > 2
            difference_function = 'f_statistic'

    _d_func = _difference_functions[difference_function]

    def diff_func(x):
        args = _d_func(*[x[metadata == c] for c in cats])
        if isinstance(args, tuple):
            return args[0]
        else:
            return args
    # effectively doing a groupby operation wrt to the metadata
    fold_change = transformed_table.apply(diff_func, axis=0)
    if not pd.isnull(fold_change).all():
        volcano_results = pd.DataFrame({transform_function_name: fold_change,
                                        'W': ancom_results[0].W})
        volcano_results = volcano_results.reset_index(drop=False)

        spec = {
            '$schema': 'https://vega.github.io/schema/vega/v4.json',
            'width': 300,
            'height': 300,
            'data': [
                {'name': 'values',
                 'values': volcano_results.to_dict(orient='records')}],
            'scales': [
                {'name': 'xScale',
                 'domain': {'data': 'values',
                            'field': transform_function_name},
                 'range': 'width'},
                {'name': 'yScale',
                 'domain': {'data': 'values', 'field': 'W'},
                 'range': 'height'}],
            'axes': [
                {'scale': 'xScale', 'orient': 'bottom',
                 'title': transform_function_name},
                {'scale': 'yScale', 'orient': 'left', 'title': 'W'}],
            'marks': [
              {'type': 'symbol',
               'from': {'data': 'values'},
               'encode': {
                   'hover': {
                       'fill': {'value': '#FF0000'},
                       'opacity': {'value': 1}},
                   'enter': {
                       'x': {'scale': 'xScale',
                             'field': transform_function_name},
                       'y': {'scale': 'yScale', 'field': 'W'}},
                   'update': {
                       'fill': {'value': 'black'},
                       'opacity': {'value': 0.3},
                       'tooltip': {
                           'signal': "{{'title': datum['index'], '{0}': "
                                     "datum['{0}'], 'W': datum['W']}}".format(
                                         transform_function_name)}}}}]}
        context['vega_spec'] = json.dumps(spec)

    copy_tree(os.path.join(TEMPLATES, 'ancom'), output_dir)
    ancom_results[0].to_csv(os.path.join(output_dir, 'ancom.tsv'),
                            header=True, index=True, sep='\t')
    ancom_results[1].to_csv(os.path.join(output_dir,
                                         'percent-abundances.tsv'),
                            header=True, index=True, sep='\t')
    index = os.path.join(TEMPLATES, 'ancom', 'index.html')
    q2templates.render(index, output_dir, context=context)
