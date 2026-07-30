"""
run_feature_test_success_rate.py
Entry point: tests feature_success_rate.py (EPA + point-in-time success rate)
against the EPA-only baseline via run_feature_test.py's pre-registered
acceptance criteria.

Usage: python models/run_feature_test_success_rate.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import feature_success_rate
from run_feature_test import run_feature_test

if __name__ == "__main__":
    run_feature_test(
        challenger_feature_fn=feature_success_rate.features,
        challenger_predict_fn=feature_success_rate.predict_margin,
        num_new_features=1,
        challenger_label="EPA + success rate",
    )
