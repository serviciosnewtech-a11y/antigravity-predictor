"""Predictor source tree.

The trading-model implementation lives in the ``lgbm_poc`` package
(``src/lgbm_poc/``). Entry-point scripts in this directory add ``src/`` to
``sys.path`` via ``_bootstrap`` and import only from ``lgbm_poc.*``.
"""
