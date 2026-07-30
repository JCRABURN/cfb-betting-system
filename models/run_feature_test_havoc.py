"""
run_feature_test_havoc.py
Entry point: tests feature_havoc.py (EPA + point-in-time havoc rate) against
the EPA-only baseline via run_feature_test.py's pre-registered acceptance
criteria -- same three criteria, same bar, tested independently (not stacked
on the rejected success-rate feature).

Usage: python models/run_feature_test_havoc.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import feature_havoc
from run_feature_test import run_feature_test

if __name__ == "__main__":
    run_feature_test(
        challenger_feature_fn=feature_havoc.features,
        challenger_predict_fn=feature_havoc.predict_margin,
        num_new_features=1,
        challenger_label="EPA + havoc rate",
    )
