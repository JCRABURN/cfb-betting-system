"""
run_feature_test_rest.py
Entry point: tests feature_rest.py (EPA + days-of-rest differential + bye-
week-flag differential) against the EPA-only baseline via
run_feature_test.py's pre-registered acceptance criteria -- same three
criteria, same bar, tested independently (not stacked on the rejected
success-rate or havoc features).

Usage: python models/run_feature_test_rest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import feature_rest
from run_feature_test import run_feature_test

if __name__ == "__main__":
    run_feature_test(
        challenger_feature_fn=feature_rest.features,
        challenger_predict_fn=feature_rest.predict_margin,
        num_new_features=2,
        challenger_label="EPA + rest/schedule (days-rest diff + bye-week diff)",
    )
