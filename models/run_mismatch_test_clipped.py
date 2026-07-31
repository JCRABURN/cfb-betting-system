"""
run_mismatch_test_clipped.py
Entry point: tests variant_clipped_input.py against the EPA-only baseline
via run_mismatch_variant_test.py's pre-registered bucket-focused acceptance
criteria.

Usage: python models/run_mismatch_test_clipped.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import variant_clipped_input
from run_mismatch_variant_test import run_variant_test

if __name__ == "__main__":
    run_variant_test(
        challenger_feature_fn=variant_clipped_input.epa_differential,
        challenger_predict_fn=variant_clipped_input.predict_margin,
        challenger_label="EPA, clipped input (cap=0.40)",
    )
